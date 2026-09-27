"""
MCP V3 API Models

Request/response data structure definitions.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field


# ============================================
# Request models
# ============================================

class BindToolRequest(BaseModel):
    """Bind tool request."""
    tool_id: str = Field(..., description="Tool ID to bind")
    enabled: bool = Field(default=True, description="Whether enabled")
    mcp_exposed: bool = Field(default=True, description="Whether exposed via MCP")


class BindToolsRequest(BaseModel):
    """Batch bind tools request."""
    bindings: List[BindToolRequest] = Field(..., description="List of bindings")


class UpdateToolBindingRequest(BaseModel):
    """Update tool binding request."""
    enabled: Optional[bool] = Field(None, description="Whether enabled")
    mcp_exposed: Optional[bool] = Field(None, description="Whether exposed via MCP")


class RegenerateMcpKeyRequest(BaseModel):
    """Regenerate MCP API Key request."""
    # No parameters needed for now


# ============================================
# Response models
# ============================================

class McpAgentOut(BaseModel):
    """MCP Agent response."""
    id: str
    name: str
    icon: str
    mcp_api_key: str
    mcp_enabled: bool = True
    created_at: datetime


class McpBoundToolOut(BaseModel):
    """MCP bound tool response."""
    id: str  # agent_tool association ID
    tool_id: str
    name: str
    type: str
    description: Optional[str] = None
    path: Optional[str] = None
    json_path: str = ""
    enabled: bool = True
    mcp_exposed: bool = True
    category: str = "builtin"
    created_at: Optional[datetime] = None


class McpStatusOut(BaseModel):
    """MCP status response."""
    agent_id: str
    mcp_api_key: str
    mcp_enabled: bool
    tools_count: int
    mcp_exposed_count: int
