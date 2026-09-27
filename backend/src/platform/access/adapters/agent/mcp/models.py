"""
MCP V3 Domain Models

MCP V3 no longer has a separate MCP instance table; it reuses the Agent model.
This module defines some MCP-specific view models.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class McpAgentInfo(BaseModel):
    """Agent information from the MCP perspective."""

    id: str = Field(..., description="Agent ID")
    name: str = Field(..., description="Agent name")
    icon: str = Field(default="✨", description="Agent icon")
    mcp_api_key: str = Field(..., description="MCP API Key")
    mcp_enabled: bool = Field(default=True, description="Whether MCP is enabled")
    created_at: datetime = Field(..., description="Creation time")


class McpBoundTool(BaseModel):
    """MCP bound tool information."""

    id: str = Field(..., description="agent_tool association ID")
    tool_id: str = Field(..., description="Tool ID")
    name: str = Field(..., description="Tool name")
    type: str = Field(..., description="Tool type")
    description: Optional[str] = Field(None, description="Tool description")
    path: Optional[str] = Field(None, description="Bound version path")
    json_path: str = Field(default="", description="JSON path")
    enabled: bool = Field(default=True, description="Whether enabled")
    mcp_exposed: bool = Field(default=True, description="Whether exposed via MCP")
    category: str = Field(default="builtin", description="Tool category")
