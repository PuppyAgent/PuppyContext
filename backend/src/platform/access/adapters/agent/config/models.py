"""
Agent Config Data Models
"""

from datetime import datetime
from typing import Optional, List, Literal
from pydantic import BaseModel, ConfigDict, Field


AgentType = Literal["chat", "webhook", "schedule"]
TriggerType = Literal["manual", "cron", "webhook"]


class AgentBash(BaseModel):
    """Agent Bash terminal access permission model."""

    id: str = Field(..., description="Bash permission ID")
    agent_id: str = Field(..., description="Owning Agent ID")
    path: str = Field(..., description="version path")
    readonly: bool = Field(default=True)
    created_at: datetime = Field(...)

    model_config = ConfigDict(from_attributes=True)


class AgentTool(BaseModel):
    """Agent Tool association model."""

    id: str = Field(...)
    agent_id: str = Field(...)
    tool_id: str = Field(...)
    enabled: bool = Field(default=True)
    mcp_exposed: bool = Field(default=False)
    created_at: datetime = Field(...)

    model_config = ConfigDict(from_attributes=True)


class Agent(BaseModel):
    """Agent domain model."""

    id: str = Field(...)
    project_id: str = Field(...)

    name: str = Field(...)
    icon: str = Field(default="✨")
    type: AgentType = Field(default="chat")
    description: Optional[str] = Field(None)

    is_default: bool = Field(default=False)

    mcp_api_key: Optional[str] = Field(None)
    mcp_enabled: bool = Field(default=False)
    mcp_key_last4: Optional[str] = Field(None)

    trigger_type: Optional[str] = Field(default="manual")
    trigger_config: Optional[dict] = Field(None)
    task_content: Optional[str] = Field(None)
    task_path: Optional[str] = Field(None)
    external_config: Optional[dict] = Field(None)

    llm_model: Optional[str] = Field(None)
    system_prompt: Optional[str] = Field(None)

    # Mirrors ``access_surfaces.status`` (``active`` / ``paused``). Agents
    # are stored as access-surface rows with ``kind='agent'``; pause /
    # resume is the existing surface-level toggle and is also what
    # ``chat/service._enforce_agent_not_paused`` reads to refuse
    # sessions for a paused agent.
    status: str = Field(default="active")

    created_at: datetime = Field(...)
    updated_at: datetime = Field(...)

    bash_accesses: List[AgentBash] = Field(default_factory=list)
    tools: List[AgentTool] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)
