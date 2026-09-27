"""Single backend boundary for invalidating MCP config/session caches."""

from __future__ import annotations

import httpx

from src.config import settings
from src.utils.logger import log_error


def _invalidate(payload: dict[str, str]) -> None:
    base = (settings.MCP_SERVER_URL or "").rstrip("/")
    if not base:
        return
    try:
        httpx.post(
            f"{base}/cache/invalidate",
            json=payload,
            headers={"X-Internal-Secret": settings.INTERNAL_API_SECRET or ""},
            timeout=5.0,
            trust_env=False,
        )
    except Exception as exc:
        log_error(f"Failed to invalidate MCP cache: target={payload} err={exc}")


def invalidate_mcp_cache(api_key: str) -> None:
    _invalidate({"api_key": api_key})


def invalidate_mcp_surface_cache(access_surface_id: str) -> None:
    _invalidate({"access_surface_id": access_surface_id})
