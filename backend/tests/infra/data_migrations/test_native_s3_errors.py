"""Hosted Supabase S3 error envelopes differ from the local Docker service."""

import importlib.util
import io
from pathlib import Path

import pytest
from botocore.exceptions import ClientError


@pytest.fixture(params=["native_repository_inventory", "repository_recovery_archive"])
def artifact(request):
    root = Path(__file__).resolve().parents[4]
    path = root / "supabase/data_migrations" / ("20261007_" + request.param) / "run.py"
    spec = importlib.util.spec_from_file_location(request.param, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def error(code, status):
    return ClientError(
        {"Error": {"Code": code, "Message": ""}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "GetObject",
    )


def test_blank_http_404_falls_back_to_existing_source_namespace(artifact):
    calls = []

    class Storage:
        def get_object(self, *, Bucket, Key):
            calls.append(Key)
            if Key.startswith("version/"):
                raise error("", 404)
            return {"ContentLength": 2, "Body": io.BytesIO(b"{}")}

    converter = getattr(artifact, "Migration", None) or artifact.ObjectConverter
    migration = converter(None, Storage(), "fixture", "project")
    assert migration.source("a" * 16) == b"{}"
    assert calls == [
        "version/project/objects/aa/" + "a" * 14,
        "mut/project/objects/aa/" + "a" * 14,
    ]


@pytest.mark.parametrize("code,status", [("AccessDenied", 403), ("", 500), ("NoSuchBucket", 404)])
def test_other_storage_failures_never_become_missing_objects(artifact, code, status):
    failure = error(code, status)

    class Storage:
        def get_object(self, **kwargs):
            raise failure

    converter = getattr(artifact, "Migration", None) or artifact.ObjectConverter
    migration = converter(None, Storage(), "fixture", "project")
    with pytest.raises(ClientError) as caught:
        migration.source("a" * 16)
    assert caught.value is failure


@pytest.mark.parametrize("artifact", ["repository_recovery_archive"], indirect=True)
def test_archive_blank_head_404_copies_and_verifies_before_recording(artifact):
    objects = {"mut/project/opaque": b"preserved bytes"}
    copied = []
    recorded = []

    class Storage:
        def get_object(self, *, Bucket, Key):
            data = objects[Key]
            return {"ContentLength": len(data), "Body": io.BytesIO(data)}

        def head_object(self, **kwargs):
            raise error("", 404)

        def copy_object(self, *, Bucket, Key, CopySource, CopySourceIfMatch):
            assert CopySourceIfMatch == '"source-etag"'
            objects[Key] = objects[CopySource["Key"]]
            copied.append(Key)

    class Database:
        def sql(self, statement):
            assert copied
            recorded.append(statement)

    archive = artifact.RecoveryArchive(Database(), Storage(), "fixture", "project", apply=True)
    row = archive.archive_bytes(
        "mut/project/opaque", {"size": len(objects["mut/project/opaque"]), "etag": '"source-etag"'}
    )
    assert objects[row["destination_key"]] == b"preserved bytes"
    assert len(copied) == len(recorded) == 1
