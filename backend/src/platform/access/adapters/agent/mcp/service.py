"""
MCP V3 Service Layer

MCP service based on the Agent architecture. Main responsibilities:
1. Retrieve Agent configuration via mcp_api_key
2. Manage Agent Tool bindings (MCP exposure)
3. Provide MCP-specific views
"""

from __future__ import annotations

from typing import List, Optional

from src.platform.access.adapters.agent.config.models import Agent, AgentTool
from src.platform.access.adapters.agent.config.repository import AgentRepository
from src.tool.repository import ToolRepositoryBase, ToolRepositorySupabase
from src.infra.supabase.dependencies import get_supabase_repository
from src.platform.access.adapters.mcp_cache import invalidate_mcp_surface_cache
from src.exceptions import NotFoundException, ErrorCode, BusinessException

from .models import McpAgentInfo, McpBoundTool
from .schemas import BindToolRequest

_AGENT_NOT_FOUND = "Agent not found"


class McpV3Service:
    """MCP V3 service."""

    def __init__(
        self,
        agent_repo: AgentRepository = None,
        tool_repo: ToolRepositoryBase = None,
    ):
        self._agent_repo = agent_repo or AgentRepository()
        self._tool_repo = tool_repo or ToolRepositorySupabase(get_supabase_repository())

    def _tool_in_agent_boundary(self, agent: Agent, tool) -> bool:
        """Agent child grants never import a sibling/foreign Project tool."""

        if tool.project_id is not None:
            return str(tool.project_id) == str(agent.project_id)
        project_org_id = self._agent_repo.get_project_org_id(agent.project_id)
        return bool(project_org_id and str(tool.org_id) == str(project_org_id))

    def _get_boundable_tool(self, agent: Agent, tool_id: str):
        tool = self._tool_repo.get_by_id(tool_id)
        if not tool or not self._tool_in_agent_boundary(agent, tool):
            raise NotFoundException("Tool not found", code=ErrorCode.NOT_FOUND)
        return tool

    # ============================================
    # Agent MCP Configuration
    # ============================================

    def get_agent_mcp_info(self, agent_id: str, user_id: str) -> McpAgentInfo:
        """Get MCP configuration info for an Agent."""
        agent = self._agent_repo.get_by_id(agent_id)
        if not agent or not self._agent_repo.is_visible_to(agent_id, user_id):
            raise NotFoundException(_AGENT_NOT_FOUND, code=ErrorCode.NOT_FOUND)

        return McpAgentInfo(
            id=agent.id,
            name=agent.name,
            icon=agent.icon,
            mcp_api_key="",
            mcp_enabled=agent.mcp_enabled,
            created_at=agent.created_at,
        )

    def regenerate_mcp_key(self, agent_id: str, user_id: str) -> str:
        """Regenerate the MCP API Key for an Agent."""
        agent = self._agent_repo.get_by_id(agent_id)
        if not agent or not self._agent_repo.is_visible_to(agent_id, user_id):
            raise NotFoundException(_AGENT_NOT_FOUND, code=ErrorCode.NOT_FOUND)

        new_key = self._agent_repo.regenerate_mcp_api_key(agent_id)
        if not new_key:
            raise BusinessException(
                "Failed to regenerate MCP API key",
                code=ErrorCode.INTERNAL_SERVER_ERROR,
            )

        invalidate_mcp_surface_cache(agent_id)
        return new_key

    # ============================================
    # Tool Binding Management
    # ============================================

    def list_bound_tools(
        self,
        agent_id: str,
        user_id: str,
        *,
        mcp_exposed_only: bool = False,
    ) -> List[McpBoundTool]:
        """Get Tools bound to an Agent."""
        agent = self._agent_repo.get_by_id(agent_id)
        if not agent or not self._agent_repo.is_visible_to(agent_id, user_id):
            raise NotFoundException(_AGENT_NOT_FOUND, code=ErrorCode.NOT_FOUND)

        # Get binding relationships
        if mcp_exposed_only:
            agent_tools = self._agent_repo.get_tools_by_agent_id_for_mcp(agent_id)
        else:
            agent_tools = self._agent_repo.get_tools_by_agent_id(agent_id)

        # Get Tool details
        result: List[McpBoundTool] = []
        for at in agent_tools:
            tool = self._tool_repo.get_by_id(at.tool_id)
            if not tool or not self._tool_in_agent_boundary(agent, tool):
                continue
            result.append(
                McpBoundTool(
                    id=at.id,
                    tool_id=tool.id,
                    name=tool.name,
                    type=tool.type,
                    description=tool.description,
                    path=tool.path,
                    json_path=tool.json_path,
                    enabled=at.enabled,
                    mcp_exposed=at.mcp_exposed,
                    category=tool.category,
                )
            )

        return result

    def bind_tool(
        self,
        agent_id: str,
        user_id: str,
        tool_id: str,
        enabled: bool = True,
        mcp_exposed: bool = True,
    ) -> AgentTool:
        """Bind a Tool to an Agent."""
        # Verify Agent access (via project)
        agent = self._agent_repo.get_by_id(agent_id)
        if not agent or not self._agent_repo.is_visible_to(agent_id, user_id):
            raise NotFoundException(_AGENT_NOT_FOUND, code=ErrorCode.NOT_FOUND)

        self._get_boundable_tool(agent, tool_id)

        # Create or update binding
        binding = self._agent_repo.upsert_tool_binding(
            agent_id=agent_id,
            tool_id=tool_id,
            enabled=enabled,
            mcp_exposed=mcp_exposed,
        )

        # Invalidate MCP cache
        if agent.mcp_enabled:
            invalidate_mcp_surface_cache(agent.id)

        return binding

    def bind_tools(
        self,
        agent_id: str,
        user_id: str,
        bindings: List[BindToolRequest],
    ) -> List[AgentTool]:
        """Batch bind Tools to an Agent."""
        # Verify Agent access (via project)
        agent = self._agent_repo.get_by_id(agent_id)
        if not agent or not self._agent_repo.is_visible_to(agent_id, user_id):
            raise NotFoundException(_AGENT_NOT_FOUND, code=ErrorCode.NOT_FOUND)

        result: List[AgentTool] = []
        for b in bindings:
            try:
                self._get_boundable_tool(agent, b.tool_id)
            except NotFoundException:
                raise NotFoundException(
                    f"Tool not found: {b.tool_id}", code=ErrorCode.NOT_FOUND
                ) from None

            binding = self._agent_repo.upsert_tool_binding(
                agent_id=agent_id,
                tool_id=b.tool_id,
                enabled=b.enabled,
                mcp_exposed=b.mcp_exposed,
            )
            result.append(binding)

        # Invalidate MCP cache
        if agent.mcp_enabled:
            invalidate_mcp_surface_cache(agent.id)

        return result

    def update_tool_binding(
        self,
        agent_id: str,
        user_id: str,
        tool_id: str,
        enabled: Optional[bool] = None,
        mcp_exposed: Optional[bool] = None,
    ) -> AgentTool:
        """Update a Tool binding."""
        # Verify Agent access (via project)
        agent = self._agent_repo.get_by_id(agent_id)
        if not agent or not self._agent_repo.is_visible_to(agent_id, user_id):
            raise NotFoundException(_AGENT_NOT_FOUND, code=ErrorCode.NOT_FOUND)

        # Find existing binding
        binding = self._agent_repo.get_tool_binding_by_agent_and_tool(agent_id, tool_id)
        if not binding:
            raise NotFoundException("Tool binding not found", code=ErrorCode.NOT_FOUND)
        self._get_boundable_tool(agent, tool_id)

        # Update
        updated = self._agent_repo.update_tool_binding(
            binding.id,
            enabled=enabled,
            mcp_exposed=mcp_exposed,
        )
        if not updated:
            raise BusinessException(
                "Failed to update tool binding",
                code=ErrorCode.INTERNAL_SERVER_ERROR,
            )

        # Invalidate MCP cache
        if agent.mcp_enabled:
            invalidate_mcp_surface_cache(agent.id)

        return updated

    def unbind_tool(self, agent_id: str, user_id: str, tool_id: str) -> bool:
        """Unbind a Tool."""
        # Verify Agent access (via project)
        agent = self._agent_repo.get_by_id(agent_id)
        if not agent or not self._agent_repo.is_visible_to(agent_id, user_id):
            raise NotFoundException(_AGENT_NOT_FOUND, code=ErrorCode.NOT_FOUND)

        # Find existing binding
        binding = self._agent_repo.get_tool_binding_by_agent_and_tool(agent_id, tool_id)
        if not binding:
            raise NotFoundException("Tool binding not found", code=ErrorCode.NOT_FOUND)
        self._get_boundable_tool(agent, tool_id)

        # Delete
        ok = self._agent_repo.delete_tool_binding(binding.id)
        if not ok:
            raise BusinessException(
                "Failed to unbind tool",
                code=ErrorCode.INTERNAL_SERVER_ERROR,
            )

        # Invalidate MCP cache
        if agent.mcp_enabled:
            invalidate_mcp_surface_cache(agent.id)

        return True

    def get_mcp_status(self, agent_id: str, user_id: str) -> dict:
        """Get MCP status summary for an Agent."""
        agent = self._agent_repo.get_by_id(agent_id)
        if not agent or not self._agent_repo.is_visible_to(agent_id, user_id):
            raise NotFoundException(_AGENT_NOT_FOUND, code=ErrorCode.NOT_FOUND)

        all_tools = [
            binding
            for binding in self._agent_repo.get_tools_by_agent_id(agent_id)
            if (
                (tool := self._tool_repo.get_by_id(binding.tool_id)) is not None
                and self._tool_in_agent_boundary(agent, tool)
            )
        ]
        mcp_exposed = [t for t in all_tools if t.enabled and t.mcp_exposed]

        return {
            "agent_id": agent.id,
            "mcp_api_key": "",
            "mcp_enabled": agent.mcp_enabled,
            "tools_count": len(all_tools),
            "mcp_exposed_count": len(mcp_exposed),
        }
