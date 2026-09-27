from unittest.mock import MagicMock

import httpx
import pytest

from src.infra import readiness


@pytest.mark.parametrize(
    "database_status,bucket_error,expected",
    [
        (200, False, []),
        (503, False, ["Database is unavailable"]),
        (200, True, ["Object storage is unavailable"]),
        (401, True, ["Database is unavailable", "Object storage is unavailable"]),
    ],
)
async def test_readiness_checks_real_dependency_results(
    monkeypatch, database_status, bucket_error, expected
):
    monkeypatch.setenv("SUPABASE_KEY", "private-test-key")
    monkeypatch.setenv("SUPABASE_URL", "http://local.invalid")
    monkeypatch.delenv("AUTH_SECURITY_REDIS_URL", raising=False)
    monkeypatch.delenv("ETL_REDIS_URL", raising=False)
    original = httpx.AsyncClient

    def handler(request):
        assert request.url.path == "/rest/v1/projects"
        assert request.headers["apikey"] == "private-test-key"
        return httpx.Response(database_status, json=[])

    monkeypatch.setattr(
        readiness.httpx,
        "AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(handler), **kw),
    )
    bucket = MagicMock(
        side_effect=RuntimeError("private-provider-details") if bucket_error else None
    )
    monkeypatch.setattr(readiness, "_probe_bucket", bucket)
    assert await readiness.core_dependency_errors() == expected
    bucket.assert_called_once()
