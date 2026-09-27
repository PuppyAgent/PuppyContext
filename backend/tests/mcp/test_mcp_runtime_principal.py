from __future__ import annotations

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from src.platform.access.adapters.agent.mcp import dependencies
from src.platform.access.adapters.agent.mcp import router as mcp_router


def _request_with_headers(headers: dict[str, str]) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/mcp/proxy",
            "headers": [
                (name.lower().encode("latin-1"), value.encode("latin-1"))
                for name, value in headers.items()
            ],
        }
    )


def test_runtime_principal_forwards_opaque_mcp_key_without_repository_lookup():
    principal = dependencies.get_mcp_runtime_principal(
        authorization="Bearer mcp_endpoint_key",
    )

    assert principal.api_key == "mcp_endpoint_key"


@pytest.mark.parametrize("authorization", [None, "", "mcp_key", "Basic mcp_key", "Bearer "])
def test_runtime_principal_requires_bearer_authorization(authorization):
    with pytest.raises(HTTPException) as exc:
        dependencies.get_mcp_runtime_principal(authorization=authorization)

    assert exc.value.status_code == 401


def test_runtime_principal_rejects_non_mcp_token_format():
    with pytest.raises(HTTPException) as exc:
        dependencies.get_mcp_runtime_principal(authorization="Bearer unknown")

    assert exc.value.status_code == 401


def test_proxy_headers_forward_only_mcp_protocol_headers():
    headers = mcp_router._build_mcp_proxy_headers(
        _request_with_headers(
            {
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "Mcp-Session-Id": "session-1",
                "MCP-Protocol-Version": "2025-06-18",
                "Authorization": "Bearer public-key",
                "Cookie": "session=leak",
                "Origin": "https://example.com",
            }
        ),
        "mcp_internal_key",
    )

    normalized = {name.lower(): value for name, value in headers.items()}
    assert normalized["accept"] == "application/json, text/event-stream"
    assert normalized["content-type"] == "application/json"
    assert normalized["mcp-session-id"] == "session-1"
    assert normalized["mcp-protocol-version"] == "2025-06-18"
    assert normalized["x-api-key"] == "mcp_internal_key"
    assert "authorization" not in normalized
    assert "cookie" not in normalized
    assert "origin" not in normalized


def test_proxy_origin_validation_allows_non_browser_and_allowed_origin(monkeypatch):
    monkeypatch.setattr(mcp_router.settings, "ALLOWED_HOSTS", ["https://app.example.com"])

    mcp_router._validate_mcp_proxy_origin(_request_with_headers({}))
    mcp_router._validate_mcp_proxy_origin(
        _request_with_headers({"Origin": "https://app.example.com"})
    )


def test_proxy_origin_validation_rejects_untrusted_origin(monkeypatch):
    monkeypatch.setattr(mcp_router.settings, "ALLOWED_HOSTS", ["https://app.example.com"])

    with pytest.raises(HTTPException) as exc:
        mcp_router._validate_mcp_proxy_origin(
            _request_with_headers({"Origin": "https://evil.example"})
        )

    assert exc.value.status_code == 403
