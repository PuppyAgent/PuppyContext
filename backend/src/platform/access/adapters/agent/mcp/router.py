"""
MCP runtime gateway.

Agent-specific MCP management remains under /mcp/agents/*, while /mcp/proxy
is the shared public gateway for Agent and standalone MCP endpoint keys.
"""

from __future__ import annotations

import asyncio
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from starlette.requests import ClientDisconnect
import httpx

from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser
from src.common_schemas import ApiResponse
from src.config import settings
from src.exceptions import NotFoundException, ErrorCode
from src.platform.access.adapters.agent.config.dependencies import (
    get_credential_agent,
    get_verified_agent,
    get_writable_agent,
)
from src.platform.access.adapters.agent.config.models import Agent

from .dependencies import McpRuntimePrincipal, get_mcp_v3_service, get_mcp_runtime_principal
from .service import McpV3Service
from .schemas import (
    McpBoundToolOut,
    McpStatusOut,
    BindToolsRequest,
    UpdateToolBindingRequest,
)


router = APIRouter(prefix="/mcp", tags=["mcp"])


_MCP_PROXY_FORWARD_HEADERS = {
    "accept",
    "content-type",
    "mcp-protocol-version",
    "mcp-session-id",
    "last-event-id",
    "cache-control",
}


def _build_mcp_proxy_headers(request: Request, api_key: str) -> dict[str, str]:
    """Forward only MCP protocol headers plus the internal runtime key."""
    headers: dict[str, str] = {}
    for name, value in request.headers.items():
        lower_name = name.lower()
        if lower_name in _MCP_PROXY_FORWARD_HEADERS or lower_name.startswith("mcp-"):
            headers[name] = value
    headers["X-API-KEY"] = api_key
    return headers


def _validate_mcp_proxy_origin(request: Request) -> None:
    """Validate browser Origin for the public MCP streamable HTTP gateway."""
    origin = (request.headers.get("origin") or "").rstrip("/")
    if not origin:
        return

    allowed_hosts = settings.ALLOWED_HOSTS or []
    if "*" in allowed_hosts:
        return

    allowed_origins = {
        host.rstrip("/")
        for host in allowed_hosts
        if host and host != "*"
    }
    if origin not in allowed_origins:
        raise HTTPException(
            status_code=403,
            detail="Origin is not allowed for MCP proxy",
        )


# ============================================
# Agent MCP Configuration Management
# ============================================


@router.get(
    "/agents/{agent_id}/status",
    response_model=ApiResponse[McpStatusOut],
    summary="Get MCP status for an Agent",
    status_code=status.HTTP_200_OK,
)
def get_mcp_status(
    agent_id: str,
    _authorized_agent: Agent = Depends(get_verified_agent),
    svc: McpV3Service = Depends(get_mcp_v3_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    data = svc.get_mcp_status(agent_id, current_user.user_id)
    return ApiResponse.success(data=data, message="MCP status retrieved successfully")


@router.post(
    "/agents/{agent_id}/regenerate-key",
    response_model=ApiResponse[dict],
    summary="Regenerate MCP API Key for an Agent",
    status_code=status.HTTP_200_OK,
)
def regenerate_mcp_key(
    agent_id: str,
    _authorized_agent: Agent = Depends(get_credential_agent),
    svc: McpV3Service = Depends(get_mcp_v3_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    new_key = svc.regenerate_mcp_key(agent_id, current_user.user_id)
    return ApiResponse.success(
        data={"mcp_api_key": new_key},
        message="MCP API Key regenerated successfully",
    )


# ============================================
# Tool Binding Management
# ============================================


@router.get(
    "/agents/{agent_id}/tools",
    response_model=ApiResponse[List[McpBoundToolOut]],
    summary="Get Tools bound to an Agent",
    status_code=status.HTTP_200_OK,
)
def list_bound_tools(
    agent_id: str,
    mcp_exposed_only: bool = Query(
        default=False, description="Whether to return only MCP-exposed tools"
    ),
    _authorized_agent: Agent = Depends(get_verified_agent),
    svc: McpV3Service = Depends(get_mcp_v3_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    data = svc.list_bound_tools(
        agent_id,
        current_user.user_id,
        mcp_exposed_only=mcp_exposed_only,
    )
    return ApiResponse.success(data=data, message="Bound tools retrieved successfully")


@router.post(
    "/agents/{agent_id}/tools",
    response_model=ApiResponse[None],
    summary="Batch bind Tools to an Agent",
    status_code=status.HTTP_201_CREATED,
)
def bind_tools(
    agent_id: str,
    payload: BindToolsRequest,
    _authorized_agent: Agent = Depends(get_writable_agent),
    svc: McpV3Service = Depends(get_mcp_v3_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    svc.bind_tools(agent_id, current_user.user_id, payload.bindings)
    return ApiResponse.success(data=None, message="Tools bound successfully")


@router.put(
    "/agents/{agent_id}/tools/{tool_id}",
    response_model=ApiResponse[None],
    summary="Update Tool binding status",
    status_code=status.HTTP_200_OK,
)
def update_tool_binding(
    agent_id: str,
    tool_id: str,
    payload: UpdateToolBindingRequest,
    _authorized_agent: Agent = Depends(get_writable_agent),
    svc: McpV3Service = Depends(get_mcp_v3_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    svc.update_tool_binding(
        agent_id,
        current_user.user_id,
        tool_id,
        enabled=payload.enabled,
        mcp_exposed=payload.mcp_exposed,
    )
    return ApiResponse.success(data=None, message="Tool binding updated successfully")


@router.delete(
    "/agents/{agent_id}/tools/{tool_id}",
    response_model=ApiResponse[None],
    summary="Unbind a Tool",
    status_code=status.HTTP_200_OK,
)
def unbind_tool(
    agent_id: str,
    tool_id: str,
    _authorized_agent: Agent = Depends(get_writable_agent),
    svc: McpV3Service = Depends(get_mcp_v3_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    svc.unbind_tool(agent_id, current_user.user_id, tool_id)
    return ApiResponse.success(data=None, message="Tool unbound successfully")


# ============================================
# MCP Server Proxy
# ============================================


@router.api_route(
    "/proxy",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
    summary="MCP Server proxy route",
    description="Forward requests to MCP Server. Provide the key via `Authorization: Bearer <mcp_api_key>`.",
    include_in_schema=True,
)
@router.api_route(
    "/proxy/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
    summary="MCP Server proxy route",
    description="Forward requests to MCP Server. Provide the key via `Authorization: Bearer <mcp_api_key>`.",
    include_in_schema=True,
)
async def proxy_mcp_server(
    request: Request,
    path: str = "",
    principal: McpRuntimePrincipal = Depends(get_mcp_runtime_principal),
):
    """
    Proxy requests to MCP Server.

    Public clients pass the key via `Authorization: Bearer <mcp_api_key>`.
    The proxy validates that key and forwards it to the internal MCP service
    as `X-API-KEY`.
    """
    _validate_mcp_proxy_origin(request)

    # 1. Build MCP Server URL
    mcp_server_url = (settings.MCP_SERVER_URL or "").rstrip("/")
    if not mcp_server_url:
        raise ValueError("MCP_SERVER_URL should not be empty.")

    normalized_path = (path or "").lstrip("/")
    if normalized_path in ("", "mcp"):
        downstream_path = "/mcp/"
    elif normalized_path.startswith("mcp/"):
        downstream_path = f"/{normalized_path}"
    else:
        downstream_path = f"/mcp/{normalized_path}"

    target_url = f"{mcp_server_url}{downstream_path}"

    # 2. Read request body
    body = b""
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        try:
            body = await request.body()
        except ClientDisconnect:
            return Response(status_code=204)

    # 3. Prepare forwarding request headers
    headers = _build_mcp_proxy_headers(request, principal.api_key)

    # 4. Query parameters
    query_params = dict(request.query_params)

    # 5. SSE / normal request routing
    accept = (request.headers.get("accept") or "").lower()
    wants_sse = "text/event-stream" in accept

    def _filter_response_headers(raw_headers: httpx.Headers) -> dict:
        filtered: dict[str, str] = {}
        for k, v in raw_headers.items():
            lk = k.lower()
            if lk.startswith(("mcp-", "x-")):
                filtered[lk] = v
        return filtered

    if wants_sse:
        # SSE streaming response
        timeout = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
        client = httpx.AsyncClient(timeout=timeout, trust_env=False)
        upstream_response: httpx.Response | None = None
        try:
            last_exc: Exception | None = None
            for attempt in range(5):
                try:
                    upstream_request = client.build_request(
                        method=request.method,
                        url=target_url,
                        headers=headers,
                        content=body,
                        params=query_params,
                    )
                    upstream_response = await client.send(upstream_request, stream=True)
                    last_exc = None
                    break
                except httpx.ConnectError as e:
                    last_exc = e
                    await asyncio.sleep(min(0.2 * (2**attempt), 1.0))

            if upstream_response is None and last_exc is not None:
                raise last_exc

            response_headers = _filter_response_headers(upstream_response.headers)
            media_type = upstream_response.headers.get("content-type")

            async def _close_upstream():
                try:
                    if upstream_response is not None:
                        await upstream_response.aclose()
                finally:
                    await client.aclose()

            return StreamingResponse(
                # aiter_bytes() (not aiter_raw()) so httpx transparently decodes
                # any Content-Encoding (e.g. gzip) from the upstream. _filter_
                # response_headers drops content-encoding, so forwarding the raw
                # compressed bytes here would leave clients unable to decode the
                # body — which broke tools/list and every large tool result for
                # real MCP clients (initialize is small enough to go uncompressed).
                upstream_response.aiter_bytes(),
                status_code=upstream_response.status_code,
                headers=response_headers,
                media_type=media_type,
                background=BackgroundTask(_close_upstream),
            )
        except httpx.ConnectError as e:
            if upstream_response is not None:
                await upstream_response.aclose()
            await client.aclose()
            raise NotFoundException(
                f"Cannot connect to MCP Server ({mcp_server_url}): {e!s}",
                code=ErrorCode.MCP_INSTANCE_NOT_FOUND,
            )
        except httpx.TimeoutException as e:
            if upstream_response is not None:
                await upstream_response.aclose()
            await client.aclose()
            raise NotFoundException(
                f"MCP Server request timed out ({mcp_server_url}): {e!s}",
                code=ErrorCode.MCP_INSTANCE_NOT_FOUND,
            )
        except Exception as e:
            if upstream_response is not None:
                await upstream_response.aclose()
            await client.aclose()
            raise NotFoundException(
                f"Error forwarding request to MCP Server: {e!s}",
                code=ErrorCode.MCP_INSTANCE_NOT_FOUND,
            )
    else:
        # Normal request
        timeout = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            try:
                last_exc: Exception | None = None
                upstream_response = None
                for attempt in range(5):
                    try:
                        upstream_response = await client.request(
                            method=request.method,
                            url=target_url,
                            headers=headers,
                            content=body,
                            params=query_params,
                        )
                        last_exc = None
                        break
                    except httpx.ConnectError as e:
                        last_exc = e
                        await asyncio.sleep(min(0.2 * (2**attempt), 1.0))

                if upstream_response is None and last_exc is not None:
                    raise last_exc

                response_headers = _filter_response_headers(upstream_response.headers)
                media_type = upstream_response.headers.get("content-type")

                return Response(
                    content=upstream_response.content,
                    status_code=upstream_response.status_code,
                    headers=response_headers,
                    media_type=media_type,
                )
            except httpx.ConnectError as e:
                raise NotFoundException(
                    f"Cannot connect to MCP Server ({mcp_server_url}): {e!s}",
                    code=ErrorCode.MCP_INSTANCE_NOT_FOUND,
                )
            except httpx.TimeoutException as e:
                raise NotFoundException(
                    f"MCP Server request timed out ({mcp_server_url}): {e!s}",
                    code=ErrorCode.MCP_INSTANCE_NOT_FOUND,
                )
            except Exception as e:
                raise NotFoundException(
                    f"Error forwarding request to MCP Server: {e!s}",
                    code=ErrorCode.MCP_INSTANCE_NOT_FOUND,
                )
