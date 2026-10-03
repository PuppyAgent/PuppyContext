import pytest

from tests.repository_hosting.harness.s3_service import validate_s3_environment
from tests.repository_hosting.unit.test_evidence_runner import runner

pytestmark = pytest.mark.hosting_component

BASE = {
    "HOSTING_TEST_SUPABASE": "1", "HOSTING_TEST_S3": "1", "HOSTING_TEST_STACK": "puppy-baseline-abcd1234",
    "SUPABASE_URL": "http://127.0.0.1:54321", "HOSTING_TEST_ANON_KEY": "synthetic",
    "SUPABASE_SERVICE_ROLE_KEY": "synthetic", "S3_ENDPOINT_URL": "http://127.0.0.1:54321/storage/v1/s3",
    "S3_ACCESS_KEY_ID": "synthetic", "S3_SECRET_ACCESS_KEY": "synthetic",
    "S3_BUCKET_NAME": "hosting-puppy-baseline-abcd1234",
}


@pytest.mark.parametrize("key,value", [
    ("S3_ENDPOINT_URL", "https://s3.amazonaws.com"),
    ("S3_ENDPOINT_URL", "http://127.0.0.1:54322/storage/v1/s3"),
    ("S3_ENDPOINT_URL", "http://127.0.0.1:54321/storage/v1/s3?proxy=prod"),
    ("S3_BUCKET_NAME", "production"),
    ("HOSTING_TEST_S3", "0"),
    ("HOSTING_TEST_STACK", "production"),
    ("S3_SECRET_ACCESS_KEY", ""),
])
def test_s3_environment_fails_closed_before_any_io(key, value):
    with pytest.raises(RuntimeError):
        validate_s3_environment(BASE | {key: value})


def test_exact_owned_s3_environment_is_accepted_without_network_access():
    validate_s3_environment(BASE)


def test_s3_required_layer_cannot_be_satisfied_by_pg_or_component_tests():
    result = {"pytest_exit": 0, "s3": True, "target": True, "layers": {"hosting_live": {"passed": 100}}}
    assert runner.result_exit_code(result) == 1
    result["layers"]["hosting_s3"] = {"passed": 1}
    assert runner.result_exit_code(result) == 0
