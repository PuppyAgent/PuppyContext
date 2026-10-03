"""Production S3 backend, restricted to the runner-owned Supabase object service."""

import os
from contextlib import contextmanager

import boto3
from botocore.config import Config

from src.infra.s3.service import S3Service
from tests.repository_hosting.harness.supabase_api import SupabaseAPI


def validate_s3_environment(environ):
    api = SupabaseAPI(environ)
    try:
        expected = str(api.client.base_url).rstrip("/") + "/storage/v1/s3"
        if (environ.get("HOSTING_TEST_S3") != "1" or environ.get("S3_ENDPOINT_URL") != expected
                or environ.get("S3_BUCKET_NAME") != "hosting-" + environ["HOSTING_TEST_STACK"]
                or not environ.get("S3_ACCESS_KEY_ID") or not environ.get("S3_SECRET_ACCESS_KEY")):
            raise RuntimeError("S3 tests require the owned stack's exact endpoint and bucket")
    finally:
        api.close()


@contextmanager
def owned_s3():
    validate_s3_environment(os.environ)
    api = SupabaseAPI(os.environ)
    service = None
    try:
        bucket = os.environ["S3_BUCKET_NAME"]
        response = api.request("POST", "/storage/v1/bucket", json={"id": bucket, "name": bucket, "public": False})
        if response.status_code not in (200, 201):
            # Fixture can share an existing bucket only inside this owned stack.
            existing = api.request("GET", "/storage/v1/bucket/" + bucket)
            assert existing.status_code == 200, response.text
        service = S3Service()
        assert service.endpoint_url == os.environ["S3_ENDPOINT_URL"]
        assert service.bucket_name == bucket
        # The real production S3 adapter is used. Only connection budgets/proxy
        # isolation change, so developer proxy settings cannot route test data.
        service.client.close()
        service.client = boto3.client(
            "s3", endpoint_url=service.endpoint_url, region_name=service.region,
            aws_access_key_id=service.access_key_id, aws_secret_access_key=service.secret_access_key,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                          proxies={}, connect_timeout=5, read_timeout=15, retries={"max_attempts": 1}),
        )
        yield service, api
    finally:
        if service is not None:
            service.client.close()
        api.close()
