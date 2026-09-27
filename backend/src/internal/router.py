"""
Internal API Router
Called by internal services (e.g., MCP Server), authenticated via SECRET
"""

import asyncio
import hmac
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request

from src.config import settings
from src.content.table.dependencies import get_table_service
from src.exceptions import AppException
from src.infra.search.dependencies import get_search_service
from src.infra.search.schemas import SearchToolQueryInput, SearchToolQueryResponse
from src.infra.supabase.dependencies import get_supabase_repository
from src.infra.turbopuffer.internal_router import router as turbopuffer_internal_router
from src.platform.authorization.service import redacted_project_ref
from src.platform.billing.facts import BillingFactsService
from src.platform.entitlements.dependencies import get_entitlement_service
from src.platform.entitlements.models import EntitlementUpsert
from src.platform.entitlements.service import EntitlementService
from src.platform.organization.repository import OrganizationRepository
from src.utils.logger import log_warning
from src.version_engine.adapters.product.commands import VersionWriteCommandService
from src.version_engine.adapters.product.operation_adapter import ProductOperationAdapter
from src.version_engine.bootstrap.dependencies import (
    get_product_operation_adapter,
)

router = APIRouter(prefix="/internal", tags=["internal"])

# Security manifest: every project-scoped internal endpoint must appear here and
# invoke one of the two actor guards before its first tenant operation. Tests
# compare this manifest to the router so additions cannot silently omit authz.
PROJECT_SCOPED_INTERNAL_ENDPOINTS = {
    "/internal/table/{table_id}",
    "/internal/tables/{table_id}/context-schema",
    "/internal/tables/{table_id}/context-data",
    "/internal/tools/{tool_id}/search",
    "/internal/nodes/resolve-path",
    "/internal/nodes/list",
    "/internal/nodes/read",
    "/internal/nodes/write",
    "/internal/nodes/create",
    "/internal/nodes/rm",
    "/internal/nodes/rename",
    "/internal/nodes/move",
}


async def verify_internal_secret(x_internal_secret: str = Header(...)) -> None:
    configured_secret = (settings.INTERNAL_API_SECRET or "").strip()
    if not configured_secret:
        raise HTTPException(
            status_code=503,
            detail="Internal API secret is not configured",
        )

    if not hmac.compare_digest(x_internal_secret, configured_secret):
        raise HTTPException(status_code=403, detail="Invalid internal secret")


def _enforce_acting_user_project_access(
    request: Request,
    project_id: str,
    action=None,
) -> str:
    """SECURITY (C-3): Internal endpoints that operate on a project must
    declare WHICH user the call is being made on behalf of (via the
    X-Acting-User-Id header), and that user must have access to project_id.

    Without this check, anyone holding the internal secret (e.g. the mcp
    service, or an attacker who exfiltrated it) could read/write the hash
    tree of ANY project by varying project_id.

    Returns:
        acting_user_id (str)

    Raises:
        400 if X-Acting-User-Id is missing
        403 if the acting user has no access to project_id
    """
    if not project_id:
        raise HTTPException(status_code=400, detail="project_id is required")

    acting_user = request.headers.get("x-acting-user-id", "").strip()
    if not acting_user:
        raise HTTPException(
            status_code=400,
            detail=(
                "Internal endpoints operating on a project must declare X-Acting-User-Id header"
            ),
        )

    try:
        from src.platform.authorization.factory import build_authorization_service
        from src.platform.authorization.models import ProjectAction

        selected_action = action or ProjectAction.CONTENT_READ
        allowed = build_authorization_service().allows(project_id, acting_user, selected_action)
    except Exception as e:
        log_warning(
            "[Internal] project access check error "
            f"project_ref={redacted_project_ref(project_id)} "
            f"error_type={type(e).__name__}"
        )
        raise HTTPException(
            status_code=503,
            detail="Project access check unavailable",
        ) from e

    if not allowed:
        log_warning(
            "[Internal] project authorization denied "
            f"project_ref={redacted_project_ref(project_id)}"
        )
        raise HTTPException(
            status_code=403,
            detail="Acting user is not a member of this project",
        )
    return acting_user


def _create_write_commands() -> VersionWriteCommandService:
    from src.platform.project.write_lease import build_leased_worker_write_commands

    return build_leased_worker_write_commands()


def _enforce_acting_user_table_access(
    request: Request, table_service, table_id: str, action=None
) -> str:
    """Resolve the project owning ``table_id`` and enforce acting-user access.

    The table context endpoints operate on project data by table_id; without
    this, any holder of the internal secret could read/write any project's
    table by varying table_id. Mirrors the node endpoints' project-access gate.
    """
    table = table_service.get_by_id(table_id)
    if not table:
        raise HTTPException(status_code=404, detail="Table not found")
    return _enforce_acting_user_project_access(request, table.project_id, action=action)


# ============================================================
# Turbopuffer internal debug endpoints
# ============================================================
router.include_router(
    turbopuffer_internal_router,
    dependencies=[Depends(verify_internal_secret)],
)


@router.post(
    "/billing/entitlements/upsert",
    summary="Upsert organization entitlement snapshot",
    dependencies=[Depends(verify_internal_secret)],
)
async def upsert_organization_entitlements(
    payload: EntitlementUpsert,
    entitlement_service: EntitlementService = Depends(get_entitlement_service),
):
    acknowledgement = entitlement_service.publish(payload)
    return {
        "ok": True,
        "data": acknowledgement.model_dump(mode="json"),
    }


@router.get(
    "/billing/organizations/{org_id}/access",
    summary="Verify user can manage organization billing",
    dependencies=[Depends(verify_internal_secret)],
)
async def verify_billing_organization_access(
    org_id: str,
    user_id: str = Query(..., description="Authenticated user id requesting billing changes"),
):
    member = OrganizationRepository().get_member(org_id, user_id)
    if not member:
        raise HTTPException(
            status_code=403,
            detail="User is not a member of this organization",
        )
    if member.role != "owner":
        raise HTTPException(
            status_code=403,
            detail="Organization owner role is required for billing changes",
        )
    return {
        "ok": True,
        "org_id": org_id,
        "user_id": user_id,
        "role": member.role,
    }


@router.get(
    "/billing/organizations/{org_id}/facts",
    summary="Read non-financial organization billing facts for reconciliation",
    dependencies=[Depends(verify_internal_secret)],
)
async def get_billing_organization_facts(org_id: str):
    facts = await asyncio.to_thread(BillingFactsService().get, org_id)
    return {"ok": True, "data": facts.model_dump(mode="json")}


@router.get(
    "/table/{table_id}",
    summary="Get table metadata",
    description="Get table metadata by table_id (excluding data content)",
    dependencies=[Depends(verify_internal_secret)],
)
async def get_table_metadata(
    table_id: str, request: Request, table_service=Depends(get_table_service)
):
    table = table_service.get_by_id(table_id)
    if not table:
        raise HTTPException(status_code=404, detail="Table not found")
    _enforce_acting_user_project_access(request, table.project_id)

    return {
        "id": table.id,
        "table_id": table.id,
        "name": table.name,
        "description": table.description,
        "project_id": table.project_id,
    }


# ============================================================
# New internal endpoints (more standardized naming, clearer parameters)
# ============================================================


@router.get(
    "/tables/{table_id}/context-schema",
    summary="Get table mount point data structure",
    description="Get structure by table_id + json_path (JSON Pointer), excluding actual values",
    dependencies=[Depends(verify_internal_secret)],
)
async def get_table_context_schema(
    table_id: str,
    request: Request,
    json_path: str = Query(default="", description="Mount point JSON Pointer path"),
    table_service=Depends(get_table_service),
):
    try:
        from src.platform.authorization.models import ProjectAction

        _enforce_acting_user_table_access(
            request, table_service, table_id, ProjectAction.CONTENT_READ
        )
        return table_service.get_context_structure(table_id=table_id, json_pointer_path=json_path)
    except HTTPException:
        raise
    except AppException as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get(
    "/tables/{table_id}/context-data",
    summary="Get table mount point data (optional JMESPath query)",
    description="Get data by table_id + json_path; if query is provided, performs JMESPath query on the data",
    dependencies=[Depends(verify_internal_secret)],
)
async def get_table_context_data(
    table_id: str,
    request: Request,
    json_path: str = Query(default="", description="Mount point JSON Pointer path"),
    query: str | None = Query(default=None, description="JMESPath query expression (optional)"),
    table_service=Depends(get_table_service),
):
    try:
        from src.platform.authorization.models import ProjectAction

        _enforce_acting_user_table_access(
            request, table_service, table_id, ProjectAction.CONTENT_READ
        )
        if query:
            return table_service.query_context_data_with_jmespath(
                table_id=table_id, json_pointer_path=json_path, query=query
            )
        return table_service.get_context_data(table_id=table_id, json_pointer_path=json_path)
    except HTTPException:
        raise
    except AppException as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post(
    "/tables/{table_id}/context-data",
    summary="Batch create elements at mount point",
    description="Create elements at mount point by table_id + json_path; writes by key for dict, appends content in order for list",
    dependencies=[Depends(verify_internal_secret)],
)
async def create_table_context_data(
    table_id: str,
    payload: dict[str, Any],
    request: Request,
    table_service=Depends(get_table_service),
):
    try:
        from src.platform.authorization.models import ProjectAction

        _enforce_acting_user_table_access(
            request, table_service, table_id, ProjectAction.CONTENT_WRITE
        )
        json_path = payload.get("json_path", "")
        elements = payload.get("elements", [])
        data = await table_service.create_context_data(
            table_id=table_id,
            mounted_json_pointer_path=json_path,
            elements=elements,
        )
        return {"message": "Created successfully", "data": data}
    except HTTPException:
        raise
    except AppException as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.put(
    "/tables/{table_id}/context-data",
    summary="Batch update elements at mount point",
    description="Update elements by table_id + json_path; replaces by key for dict, treats key as index for list",
    dependencies=[Depends(verify_internal_secret)],
)
async def update_table_context_data(
    table_id: str,
    payload: dict[str, Any],
    request: Request,
    table_service=Depends(get_table_service),
):
    try:
        from src.platform.authorization.models import ProjectAction

        _enforce_acting_user_table_access(
            request, table_service, table_id, ProjectAction.CONTENT_WRITE
        )
        json_path = payload.get("json_path", "")
        elements = payload.get("elements", [])
        data = await table_service.update_context_data(
            table_id=table_id, json_pointer_path=json_path, elements=elements
        )
        return {"message": "Updated successfully", "data": data}
    except HTTPException:
        raise
    except AppException as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.delete(
    "/tables/{table_id}/context-data",
    summary="Batch delete elements at mount point",
    description="Delete keys by table_id + json_path; deletes by key for dict, treats key as index for list",
    dependencies=[Depends(verify_internal_secret)],
)
async def delete_table_context_data(
    table_id: str,
    payload: dict[str, Any],
    request: Request,
    table_service=Depends(get_table_service),
):
    try:
        from src.platform.authorization.models import ProjectAction

        _enforce_acting_user_table_access(
            request, table_service, table_id, ProjectAction.CONTENT_WRITE
        )
        json_path = payload.get("json_path", "")
        keys = payload.get("keys", [])
        data = await table_service.delete_context_data(
            table_id=table_id, json_pointer_path=json_path, keys=keys
        )
        return {"message": "Deleted successfully", "data": data}
    except HTTPException:
        raise
    except AppException as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


# ============================================================
# Search Tool internal endpoints (called by mcp_service v2)
# ============================================================


@router.post(
    "/tools/{tool_id}/search",
    response_model=SearchToolQueryResponse,
    summary="Execute Search Tool (ANN retrieval)",
    description=(
        "Execute semantic vector retrieval (ANN) by tool_id, returning structured results.\n\n"
        "Key points for frontend/callers:\n"
        "- This is an Internal API endpoint, requiring `X-Internal-Secret` authentication;\n"
        "- Tool must be `type=search` and must have a bound `path`;\n"
        "- Returned `results[*].json_path` is **relative to tool.json_path in RFC6901 format**, for frontend scoped positioning."
    ),
    dependencies=[Depends(verify_internal_secret)],
)
async def search_tool(
    tool_id: str,
    payload: SearchToolQueryInput,
    request: Request,
    supabase_repo=Depends(get_supabase_repository),
    search_service=Depends(get_search_service),
):
    tool = supabase_repo.get_tool(tool_id)
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")

    if (tool.type or "").strip() != "search":
        raise HTTPException(status_code=400, detail="Tool is not a search tool")

    node_path = tool.path or ""
    if not node_path:
        raise HTTPException(status_code=400, detail="tool.path is missing")

    project_id = tool.project_id or ""
    if not project_id:
        raise HTTPException(status_code=400, detail="tool.project_id is missing")
    _enforce_acting_user_project_access(request, project_id)

    try:
        from src.infra.search.index_task_repository import SearchIndexTaskRepository
        from src.infra.supabase.client import SupabaseClient

        sb_client = SupabaseClient().get_client()
        task_repo = SearchIndexTaskRepository(sb_client)
        task = task_repo.get_by_tool_id(str(tool_id))

        is_folder_search = bool(task and task.folder_path)
    except Exception:
        is_folder_search = False

    try:
        if is_folder_search:
            results = await search_service.search_folder(
                project_id=project_id,
                folder_path=node_path,
                query=payload.query,
                top_k=payload.top_k,
            )
        else:
            results = await search_service.search_scope(
                project_id=project_id,
                path=node_path,
                tool_json_path=tool.json_path or "",
                query=payload.query,
                top_k=payload.top_k,
            )
        return {"query": payload.query, "results": results}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except AppException as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


# ============================================================
# ContentNode POSIX endpoints (called by mcp_service POSIX tools)
# All path-based, using ProductOperationAdapter (hash clone/push under the hood)
# ============================================================


@router.post(
    "/nodes/resolve-path",
    summary="Resolve path to node info",
    description="Resolve to specific node info by project_id + path",
    dependencies=[Depends(verify_internal_secret)],
)
async def resolve_node_path(
    payload: dict[str, Any],
    request: Request,
    ops: ProductOperationAdapter = Depends(get_product_operation_adapter),
):
    try:
        project_id = payload.get("project_id", "")
        from src.platform.authorization.models import ProjectAction

        _enforce_acting_user_project_access(request, project_id, ProjectAction.CONTENT_READ)
        path = payload.get("path", "")

        if not path or path == "/":
            return {"virtual_root": True, "path": "/"}

        path = path.strip("/")
        entry = ops.stat(project_id, path)
        if not entry:
            raise HTTPException(status_code=404, detail=f"Path not found: {path}")

        return {
            "name": entry.name,
            "type": entry.type,
            "path": entry.path,
            "size_bytes": entry.size_bytes,
            "mime_type": entry.mime_type,
        }
    except HTTPException:
        raise
    except AppException as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get(
    "/nodes/list",
    summary="List directory contents",
    description="List child entries at specified path (including metadata)",
    dependencies=[Depends(verify_internal_secret)],
)
async def list_node_children(
    request: Request,
    project_id: str = Query(..., description="Project ID"),
    path: str = Query("", description="Directory path"),
    ops: ProductOperationAdapter = Depends(get_product_operation_adapter),
):
    try:
        from src.platform.authorization.models import ProjectAction

        _enforce_acting_user_project_access(request, project_id, ProjectAction.CONTENT_READ)
        path = path.strip("/")
        entries = ops.list_dir(project_id, path)

        return {
            "path": path,
            "children": [
                {
                    "name": e.name,
                    "path": e.path,
                    "type": e.type,
                    "size_bytes": e.size_bytes,
                    "mime_type": e.mime_type,
                    "children_count": e.children_count,
                }
                for e in entries
            ],
        }
    except AppException as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get(
    "/nodes/read",
    summary="Read file content",
    description="Return JSON content / Markdown text / file metadata based on path and type",
    dependencies=[Depends(verify_internal_secret)],
)
async def read_node_content(
    request: Request,
    project_id: str = Query(..., description="Project ID"),
    path: str = Query(..., description="File path"),
    ops: ProductOperationAdapter = Depends(get_product_operation_adapter),
):
    try:
        from src.platform.authorization.models import ProjectAction

        _enforce_acting_user_project_access(request, project_id, ProjectAction.CONTENT_READ)
        path = path.strip("/")
        entry = ops.stat(project_id, path)
        if not entry:
            raise HTTPException(status_code=404, detail=f"Path not found: {path}")

        base = {
            "name": entry.name,
            "path": entry.path,
            "type": entry.type,
            "size_bytes": entry.size_bytes,
        }

        if entry.type == "folder":
            children = ops.list_dir(project_id, path)
            base["children"] = [
                {
                    "name": c.name,
                    "path": c.path,
                    "type": c.type,
                    "size_bytes": c.size_bytes,
                }
                for c in children
            ]
            return base

        content_bytes = ops.read_file(project_id, path)

        if entry.type == "json":
            import json

            try:
                base["content"] = json.loads(content_bytes.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                base["content"] = content_bytes.decode("utf-8", errors="replace")
            return base

        if entry.type == "markdown":
            base["content"] = content_bytes.decode("utf-8", errors="replace")
            return base

        base["mime_type"] = entry.mime_type
        base["content"] = content_bytes.decode("utf-8", errors="replace")
        return base

    except HTTPException:
        raise
    except AppException as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.put(
    "/nodes/write",
    summary="Write file content (via ProductOperationAdapter)",
    description="Create or update file content",
    dependencies=[Depends(verify_internal_secret)],
)
async def write_node_content(
    payload: dict[str, Any],
    request: Request,
):
    """
    Write file content via ProductOperationAdapter.

    payload:
        project_id: str
        path: str
        content: Any (JSON object or Markdown string)
        operator_id: str (optional)
    """
    try:
        project_id = payload.get("project_id", "")
        from src.platform.authorization.models import ProjectAction

        _enforce_acting_user_project_access(request, project_id, ProjectAction.CONTENT_WRITE)
        path = payload.get("path", "").strip("/")
        content = payload.get("content")
        operator_id = payload.get("operator_id", "mcp_agent")

        if not path:
            raise HTTPException(status_code=400, detail="path is required")

        if not isinstance(content, (str, dict, list, bytes)):
            raise HTTPException(
                status_code=400, detail=f"Unsupported content type: {type(content).__name__}"
            )

        commands = _create_write_commands()
        outcome = await commands.write_file(
            project_id,
            path,
            content,
            node_type="file",
            actor=operator_id,
            message=f"Write {path}",
        )
        result = outcome.result

        return {
            "path": outcome.path,
            "commit_id": result.commit_id,
            "op": "modified",
            "updated": True,
        }
    except HTTPException:
        raise
    except AppException as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post(
    "/nodes/create",
    summary="Create file or directory (via ProductOperationAdapter)",
    description="Create a new file (JSON / Markdown) or empty directory at the specified path",
    dependencies=[Depends(verify_internal_secret)],
)
async def create_node(
    payload: dict[str, Any],
    request: Request,
):
    """
    Create file/directory via ProductOperationAdapter.

    payload:
        project_id: str
        path: str
        node_type: str (json | markdown | folder)
        content: Any (optional)
        created_by: str (optional)
    """
    try:
        project_id = payload.get("project_id", "")
        from src.platform.authorization.models import ProjectAction

        _enforce_acting_user_project_access(request, project_id, ProjectAction.CONTENT_WRITE)
        path = payload.get("path", "").strip("/")
        node_type = payload.get("node_type", "")
        content = payload.get("content")
        created_by = payload.get("created_by", "mcp_agent")

        if not path:
            raise HTTPException(status_code=400, detail="path is required")

        if node_type not in ("json", "markdown", "folder"):
            raise HTTPException(
                status_code=400, detail=f"Unsupported node type for creation: {node_type}"
            )

        commands = _create_write_commands()

        if node_type == "folder":
            outcome = await commands.mkdir(
                project_id,
                path,
                actor=created_by,
                message=f"mkdir {path}",
            )
            result = outcome.result
        else:
            if content is None:
                content = {} if node_type == "json" else ""

            outcome = await commands.write_file(
                project_id,
                path,
                content,
                node_type=node_type,
                actor=created_by,
                message=f"Create {path}",
            )
            result = outcome.result

        return {
            "path": outcome.path,
            "created": True,
            "commit_id": result.commit_id,
        }
    except HTTPException:
        raise
    except AppException as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post(
    "/nodes/rm",
    summary="Delete file or directory",
    description="Remove file or directory from the version tree",
    dependencies=[Depends(verify_internal_secret)],
)
async def remove_node(
    payload: dict[str, Any],
    request: Request,
):
    """
    payload:
        project_id: str
        path: str
        user_id: str
    """
    try:
        project_id = payload.get("project_id", "")
        from src.platform.authorization.models import ProjectAction

        _enforce_acting_user_project_access(request, project_id, ProjectAction.CONTENT_WRITE)
        path = payload.get("path", "").strip("/")
        user_id = payload.get("user_id", "mcp_agent")

        if not path:
            raise HTTPException(status_code=400, detail="path is required")
        commands = _create_write_commands()
        if commands.ops.stat(project_id, path) is None:
            raise HTTPException(status_code=404, detail=f"Path not found: {path}")

        outcome = await commands.delete(
            project_id,
            [path],
            actor=user_id,
            message=f"delete {path}",
        )
        result = outcome.result

        return {"path": path, "removed": True, "commit_id": result.commit_id}
    except HTTPException:
        raise
    except AppException as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


# ============================================================
# Node rename / move (called by AGFS puppyonefs, etc.)
# ============================================================


@router.post(
    "/nodes/rename",
    summary="Rename file or directory (via ProductOperationAdapter)",
    description="Rename by moving paths",
    dependencies=[Depends(verify_internal_secret)],
)
async def rename_node(
    payload: dict[str, Any],
    request: Request,
):
    try:
        project_id = payload.get("project_id", "")
        from src.platform.authorization.models import ProjectAction

        _enforce_acting_user_project_access(request, project_id, ProjectAction.CONTENT_WRITE)
        path = payload.get("path", "").strip("/")
        new_name = payload.get("new_name", "")
        if not new_name:
            raise HTTPException(status_code=400, detail="new_name is required")
        if not path:
            raise HTTPException(status_code=400, detail="path is required")

        parent = "/".join(path.split("/")[:-1])
        new_path = f"{parent}/{new_name}" if parent else new_name

        commands = _create_write_commands()
        await commands.move(
            project_id,
            path,
            new_path,
            actor="system",
            message=f"rename {path} → {new_path}",
        )

        return {"path": new_path, "name": new_name, "renamed": True}
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except HTTPException:
        raise
    except AppException as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post(
    "/nodes/move",
    summary="Move file or directory to new path (via ProductOperationAdapter)",
    description="Move file/directory to a new parent directory",
    dependencies=[Depends(verify_internal_secret)],
)
async def move_node_internal(
    payload: dict[str, Any],
    request: Request,
):
    try:
        project_id = payload.get("project_id", "")
        from src.platform.authorization.models import ProjectAction

        _enforce_acting_user_project_access(request, project_id, ProjectAction.CONTENT_WRITE)
        path = payload.get("path", "").strip("/")
        new_parent_path = payload.get("new_parent_path", "").strip("/")

        if not path:
            raise HTTPException(status_code=400, detail="path is required")

        name = path.split("/")[-1]
        new_path = f"{new_parent_path}/{name}" if new_parent_path else name

        commands = _create_write_commands()
        await commands.move(
            project_id,
            path,
            new_path,
            actor="system",
            message=f"move {path} → {new_path}",
        )

        return {"old_path": path, "new_path": new_path, "moved": True}
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except HTTPException:
        raise
    except AppException as e:
        raise HTTPException(status_code=e.status_code, detail=e.message) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post(
    "/sandbox-endpoint-by-key",
    summary="Get standalone Sandbox endpoint configuration by access key",
    description="External consumers call this to get Sandbox endpoint mounts, runtime, and other configuration before execution",
    dependencies=[Depends(verify_internal_secret)],
)
async def get_sandbox_endpoint_by_key(access_key: str = Body(..., embed=True)):
    from src.platform.access.adapters.sandbox_endpoint.repository import SandboxEndpointRepository

    repo = SandboxEndpointRepository()
    endpoint = repo.get_by_access_key(access_key)
    if not endpoint:
        raise HTTPException(
            status_code=404, detail="Sandbox endpoint not found for this access key"
        )
    if endpoint.get("status") != "active":
        raise HTTPException(status_code=403, detail="Sandbox endpoint is not active")

    mounts_data = []
    for m in endpoint.get("mounts", []):
        entry = {
            "path": m.get("path", ""),
            "mount_path": m.get("mount_path", "/workspace"),
            "permissions": m.get("permissions", {"read": True, "write": False, "exec": False}),
            "node_name": m.get("path", ""),
            "node_type": "",
        }
        mounts_data.append(entry)

    return {
        "endpoint": {
            "id": endpoint["id"],
            "name": endpoint["name"],
            "project_id": endpoint["project_id"],
            "runtime": endpoint.get("runtime", "alpine"),
            "provider": endpoint.get("provider", "docker"),
            "timeout_seconds": endpoint.get("timeout_seconds", 30),
            "resource_limits": endpoint.get("resource_limits", {}),
        },
        "mounts": mounts_data,
    }
