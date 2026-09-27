"""
MCP V3 Dependency Injection
"""

from __future__ import annotations

from dataclasses import dataclass
from fastapi import Header, HTTPException

from .service import McpV3Service


@dataclass(frozen=True)
class McpRuntimePrincipal:
    api_key: str


# Singleton service instance
_mcp_v3_service: McpV3Service | None = None


def get_mcp_v3_service() -> McpV3Service:
    """Get MCP V3 service singleton."""
    global _mcp_v3_service
    if _mcp_v3_service is None:
        _mcp_v3_service = McpV3Service()
    return _mcp_v3_service


def get_mcp_runtime_principal(
    authorization: str | None = Header(
        default=None,
        alias="Authorization",
        description="Bearer token for an MCP access point",
    ),
) -> McpRuntimePrincipal:
    """
    Parse an MCP runtime key for proxy routing.

    Public MCP clients authenticate with the standard HTTP Authorization
    header. Credential authentication happens exactly once in the canonical
    backend MCP runtime, after the transport forwards the opaque token.
    """
    scheme, _, token = (authorization or "").strip().partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=401,
            detail="Missing Authorization bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    api_key = token.strip()
    if not api_key.startswith("mcp_"):
        raise HTTPException(
            status_code=401,
            detail="Invalid MCP bearer token format",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return McpRuntimePrincipal(api_key=api_key)
