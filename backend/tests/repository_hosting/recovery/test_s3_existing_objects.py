"""Production S3 backend against moto's S3 emulator, never a real AWS account.

Seed persisted loose-object bytes directly, then open a fresh production store.
This is an existing-data compatibility contract, not proof of a future schema
migration or real S3 durability.
"""

import asyncio
from types import SimpleNamespace

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from src.version_engine.domain.errors import ObjectNotFoundError
from src.version_engine.storage.backends.s3 import S3StorageBackend
from src.version_engine.storage.object_store import ObjectStore
from src.version_engine.write_engine.git_object_format import encode_object, hash_object

pytestmark = pytest.mark.hosting_component


class EmulatedS3Service:
    """S3Service's async boundary using boto3 against the moto emulator."""

    def __init__(self, client):
        self.client = client
        self.bucket = "hosting-isolated-test"

    async def upload_file(self, key, data, content_type="application/octet-stream"):
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

    async def download_file(self, key):
        try:
            return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "NoSuchKey":
                raise ObjectNotFoundError(key) from exc
            raise

    async def file_exists(self, key):
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "404":
                return False
            raise

    async def delete_file(self, key):
        self.client.delete_object(Bucket=self.bucket, Key=key)

    async def list_files(self, prefix="", max_keys=1000, continuation_token=None):
        args = dict(Bucket=self.bucket, Prefix=prefix, MaxKeys=max_keys)
        if continuation_token:
            args["ContinuationToken"] = continuation_token
        page = self.client.list_objects_v2(**args)
        return (
            [
                SimpleNamespace(key=row["Key"], size=row["Size"], last_modified=row["LastModified"])
                for row in page.get("Contents", [])
            ],
            [],
            page.get("NextContinuationToken"),
            page["IsTruncated"],
        )


@pytest.fixture
def s3():
    with mock_aws():
        client = boto3.client(
            "s3",
            region_name="us-east-1",
            aws_access_key_id="testing",
            aws_secret_access_key="testing",
        )
        service = EmulatedS3Service(client)
        client.create_bucket(Bucket=service.bucket)
        yield service


def store(s3, project="old-project", *, legacy=False):
    return ObjectStore(
        None, backend=S3StorageBackend(s3, project, allow_deferred_namespace_reads=legacy)
    )


@pytest.mark.parametrize(
    "body",
    [b"", b"old user data\n", b"\0\xff\r\n", "用户原先的数据".encode(), b"x" * (1024 * 1024)],
    ids=["empty", "text", "binary", "unicode", "1MiB"],
)
def test_persisted_user_bytes_survive_fresh_reader_and_new_write(s3, body):
    oid = hash_object("blob", body)
    key = f"version/old-project/objects/{oid[:2]}/{oid[2:]}"
    asyncio.run(s3.upload_file(key, encode_object("blob", body)[1]))
    assert store(s3).get_object(oid) == ("blob", body)
    new_id = store(s3).put_blob(b"next write")
    reopened = store(s3)
    assert reopened.get_object(oid) == ("blob", body)
    assert reopened.get_object(new_id) == ("blob", b"next write")
    assert asyncio.run(s3.download_file(key)) == encode_object("blob", body)[1]


def test_same_hash_in_another_project_does_not_grant_access(s3):
    oid = store(s3, "private-a").put_blob(b"secret")
    with pytest.raises(ObjectNotFoundError):
        store(s3, "private-b").get_object(oid)
    assert store(s3, "private-a").get_object(oid) == ("blob", b"secret")


def test_corrupt_existing_key_is_never_returned_as_valid_data(s3):
    oid = store(s3).put_blob(b"original")
    key = f"version/old-project/objects/{oid[:2]}/{oid[2:]}"
    asyncio.run(s3.upload_file(key, encode_object("blob", b"wrong")[1]))
    with pytest.raises(ObjectNotFoundError):
        store(s3).get_object(oid)
    # A retry must repair corrupt dedup data, not skip because the key exists.
    assert store(s3).put_blob(b"original") == oid
    assert store(s3).get_object(oid) == ("blob", b"original")


def test_missing_object_does_not_become_empty_file(s3):
    with pytest.raises(ObjectNotFoundError):
        store(s3).get_object("a" * 40)


def test_s3_outage_is_not_misclassified_as_missing_data(s3, monkeypatch):
    oid = store(s3).put_blob(b"must stay readable after outage")
    original = s3.download_file

    async def unavailable(*args, **kwargs):
        raise TimeoutError("injected S3 timeout")

    monkeypatch.setattr(s3, "download_file", unavailable)
    with pytest.raises(TimeoutError):
        store(s3).get_object(oid)
    monkeypatch.setattr(s3, "download_file", original)
    assert store(s3).get_object(oid)[1] == b"must stay readable after outage"
