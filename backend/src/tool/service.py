from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from src.exceptions import (
    BusinessException,
    ErrorCode,
    NotFoundException,
    PermissionException,
)
from src.platform.access.adapters.mcp_cache import invalidate_mcp_surface_cache
from src.infra.supabase.dependencies import get_supabase_repository
from src.version_engine.adapters.product.operation_adapter import ProductOperationAdapter
from src.platform.authorization.models import ProjectAction
from src.platform.authorization.service import AuthorizationService
from src.tool.models import Tool
from src.tool.repository import ToolRepositoryBase
from src.tool.supabase_schemas import (
    ToolCreate as SbToolCreate,
)
from src.tool.supabase_schemas import (
    ToolUpdate as SbToolUpdate,
)


@dataclass
class ToolCreateParams:
    """Groups the parameters for creating a Tool."""
    org_id: str
    path: str | None
    json_path: str
    type: str
    name: str
    alias: str | None
    description: str | None
    input_schema: Any | None
    output_schema: Any | None
    metadata: Any | None
    created_by: str | None = None
    category: str = "builtin"
    script_type: str | None = None
    script_content: str | None = None
    project_id: str | None = None


@lru_cache(maxsize=64)
def _get_default_tool_description(tool_type: str) -> str | None:
    """
    Read the tool-owned default description for ``tool_type``.
    """
    desc_dir = Path(__file__).resolve().parent / "descriptions"
    p = desc_dir / f"{tool_type}.txt"
    if not p.exists():
        return None
    try:
        text = p.read_text(encoding="utf-8").strip()
        return text or None
    except Exception:
        return None


class ToolService:
    def __init__(
        self,
        repo: ToolRepositoryBase,
        ops: ProductOperationAdapter,
        authorization: AuthorizationService,
        supabase_repository: Any | None = None,
    ):
        self.repo = repo
        self._ops = ops
        self.authorization = authorization
        self._sb = supabase_repository

    def _get_supabase_repository(self):
        if self._sb is None:
            self._sb = get_supabase_repository()
        return self._sb

    def get_path_with_access_check(self, user_id: str, path: str):
        """Check that a path is accessible.

        Returns a simple object with project_id and type attributes.
        """
        from types import SimpleNamespace
        tool = self.repo.get_by_path(path) if hasattr(self.repo, 'get_by_path') else None
        project_id = tool.project_id if tool else None

        if not project_id:
            all_tools = self.repo.get_by_path_simple(path) if hasattr(self.repo, 'get_by_path_simple') else []
            if all_tools:
                project_id = all_tools[0].project_id

        if project_id:
            self.authorization.authorize(
                project_id, user_id, ProjectAction.CONTENT_READ
            )
            entry = self._ops.stat(project_id, path)
            if entry:
                return SimpleNamespace(
                    project_id=project_id,
                    type=entry.type,
                    name=entry.name,
                    path=entry.path,
                )

        raise NotFoundException(
            f"Node not found: {path}", code=ErrorCode.NOT_FOUND
        )

    def _invalidate_bound_agents_mcp(self, tool_id: str) -> None:
        """
        Best-effort: when a tool changes, notify all Agents bound to this tool to invalidate their MCP cache.

        Based on the connection_tool table structure:
        - Find all connection_tool records bound to this tool
        - Invalidate by access-surface id, so rotation never needs to retrieve
          a stored plaintext key.
        """
        from src.platform.access.adapters.agent.config.repository import AgentRepository

        try:
            agent_repo = AgentRepository()
            seen_surfaces = set()

            for conn_id in agent_repo.list_access_surface_ids_by_tool(tool_id):
                agent = agent_repo.get_by_id(conn_id)
                if not agent or not agent.mcp_enabled:
                    continue
                if agent.id in seen_surfaces:
                    continue
                seen_surfaces.add(agent.id)
                invalidate_mcp_surface_cache(agent.id)
        except Exception:
            pass

    def _check_sibling_name_conflict(
        self, tool_id: str, user_id: str, new_name: str, conn_id: str, agent_tools: list
    ) -> None:
        """Check whether any sibling tool in a connection already uses *new_name*."""
        for at in agent_tools:
            if at.tool_id == tool_id:
                continue
            sib = self.repo.get_by_id(at.tool_id)
            if not sib or sib.created_by != user_id:
                continue
            if sib.name == new_name:
                raise BusinessException(
                    f"Tool name conflict within connection (connection_id={conn_id}): name='{new_name}'",
                    code=ErrorCode.VALIDATION_ERROR,
                )

    def _assert_name_update_no_conflict(
        self, tool_id: str, user_id: str, new_name: str
    ) -> None:
        """
        Check if updating tool name would conflict with sibling tools
        in the same connection (Agent or MCP).
        """
        from src.platform.access.adapters.agent.config.repository import AgentRepository

        try:
            agent_repo = AgentRepository()

            for conn_id in agent_repo.list_access_surface_ids_by_tool(tool_id):
                agent_tools = agent_repo.get_tools_by_agent_id(conn_id)
                self._check_sibling_name_conflict(tool_id, user_id, new_name, conn_id, agent_tools)
        except BusinessException:
            raise
        except Exception:
            # Ignore other exceptions
            pass

    def list_org_tools(
        self, user_id: str, org_id: str, *, skip: int = 0, limit: int = 100
    ) -> list[Tool]:
        rows = self.repo.get_by_org_id(org_id, skip=skip, limit=limit)
        project_ids = [str(row.project_id) for row in rows if row.project_id]
        allowed = set(self.authorization.accessible_project_ids(project_ids, user_id))
        return [row for row in rows if not row.project_id or str(row.project_id) in allowed]

    def list_org_tools_by_path(
        self,
        user_id: str,
        org_id: str,
        *,
        path: str,
        skip: int = 0,
        limit: int = 1000,
    ) -> list[Tool]:
        self.get_path_with_access_check(user_id, path)
        return self.repo.get_by_org_id(
            org_id, skip=skip, limit=limit, path=path
        )

    def get_by_id(self, tool_id: str) -> Tool | None:
        return self.repo.get_by_id(tool_id)

    def get_by_id_with_access_check(
        self,
        tool_id: str,
        user_id: str,
        action: ProjectAction = ProjectAction.AGENT_READ,
    ) -> Tool:
        tool = self.repo.get_by_id(tool_id)
        if not tool:
            raise NotFoundException(
                f"Tool not found: {tool_id}", code=ErrorCode.NOT_FOUND
            )
        if tool.project_id:
            try:
                self.authorization.authorize(tool.project_id, user_id, action)
            except (NotFoundException, PermissionException):
                raise NotFoundException(
                    f"Tool not found: {tool_id}", code=ErrorCode.NOT_FOUND
                ) from None
        elif tool.created_by and str(tool.created_by) != str(user_id):
            raise NotFoundException(
                f"Tool not found: {tool_id}", code=ErrorCode.NOT_FOUND
            )
        return tool

    def create(self, *, params: ToolCreateParams) -> Tool:
        path = params.path
        created_by = params.created_by
        project_id = params.project_id
        description = params.description

        if path and created_by:
            node = self.get_path_with_access_check(created_by, path)
            if project_id and project_id != node.project_id:
                raise BusinessException(
                    "path does not belong to project_id",
                    code=ErrorCode.VALIDATION_ERROR,
                )
            if not project_id:
                project_id = node.project_id

        if description is None or not str(description).strip():
            description = _get_default_tool_description(str(params.type))

        if project_id and created_by:
            self.authorization.authorize(
                project_id, created_by, ProjectAction.AGENT_MANAGE
            )

        created = self.repo.create(
            SbToolCreate(
                created_by=created_by,
                org_id=params.org_id,
                project_id=project_id,
                path=path,
                json_path=params.json_path,
                type=params.type,
                name=params.name,
                alias=params.alias,
                description=description,
                input_schema=params.input_schema,
                output_schema=params.output_schema,
                metadata=params.metadata,
                category=params.category,
                script_type=params.script_type,
                script_content=params.script_content,
            )
        )
        return created

    def update(self, *, tool_id: str, user_id: str, patch: dict[str, Any]) -> Tool:
        existing = self.get_by_id_with_access_check(
            tool_id, user_id, ProjectAction.AGENT_MANAGE
        )

        # Only process fields that were passed in (router layer used exclude_unset to generate patch)
        name = patch.get("name")
        if name is not None and name != existing.name:
            self._assert_name_update_no_conflict(tool_id, user_id, name)

        path = patch.get("path")
        if path is not None:
            target = self.get_path_with_access_check(user_id, path)
            if existing.project_id and target.project_id != existing.project_id:
                raise BusinessException(
                    "path does not belong to the Tool project",
                    code=ErrorCode.VALIDATION_ERROR,
                )

        updated = self.repo.update(
            tool_id,
            SbToolUpdate(
                path=patch.get("path"),
                json_path=patch.get("json_path"),
                type=patch.get("type"),
                name=patch.get("name"),
                alias=patch.get("alias"),
                description=patch.get("description"),
                input_schema=patch.get("input_schema"),
                output_schema=patch.get("output_schema"),
                metadata=patch.get("metadata"),
                category=patch.get("category"),
                script_type=patch.get("script_type"),
                script_content=patch.get("script_content"),
            ),
        )
        if not updated:
            # Should not happen in practice (already did get_by_id_with_access_check); this is a safety net
            raise BusinessException(
                "Tool update failed", code=ErrorCode.INTERNAL_SERVER_ERROR
            )

        if updated.org_id != existing.org_id:
            raise BusinessException(
                "Tool org mismatch after update", code=ErrorCode.INTERNAL_SERVER_ERROR
            )

        # Trigger MCP cache invalidation for all Agents bound to this tool
        self._invalidate_bound_agents_mcp(tool_id)

        return updated

    def list_org_tools_by_project_id(
        self,
        user_id: str,
        org_id: str,
        *,
        project_id: str,
        limit: int = 1000,
    ) -> list[Tool]:
        grant = self.authorization.authorize(
            project_id, user_id, ProjectAction.AGENT_READ
        )
        if grant.org_id != org_id:
            raise NotFoundException(
                f"Project not found: {project_id}", code=ErrorCode.NOT_FOUND
            )
        return self.repo.get_by_org_id(
            org_id, skip=0, limit=limit, project_id=project_id
        )

    def delete(self, tool_id: str, user_id: str) -> None:
        _ = self.get_by_id_with_access_check(
            tool_id, user_id, ProjectAction.AGENT_MANAGE
        )
        ok = self.repo.delete(tool_id)
        if not ok:
            raise BusinessException(
                "Tool delete failed", code=ErrorCode.INTERNAL_SERVER_ERROR
            )
        self._invalidate_bound_agents_mcp(tool_id)
