from __future__ import annotations

from src.platform.access.adapters.mcp_endpoint.repository import _row_to_endpoint


def test_row_to_endpoint_preserves_scope_id_for_runtime_resolution():
    endpoint = _row_to_endpoint(
        {
            "id": "endpoint-1",
            "project_id": "proj-1",
            "scope_id": "scope-1",
            "name": "MCP Server",
            "config": {},
            "status": "active",
            "created_at": "2026-06-16T00:00:00Z",
            "updated_at": "2026-06-16T00:00:00Z",
        },
        "docs",
    )

    assert endpoint["scope_id"] == "scope-1"
    assert endpoint["path"] == "docs"
