from typing import Literal, Optional, List
from pydantic import BaseModel, Field


class ChatHistoryItem(BaseModel):
    role: Literal["user", "assistant"] = Field(
        "user", description="Message role (backward-compatible with old clients, defaults to user)"
    )
    content: str = Field(..., description="Message text content")


class AgentRequest(BaseModel):
    prompt: str = Field(..., min_length=1, description="User input content")
    session_id: Optional[str] = Field(
        None, description="Chat session ID (for server-side persistence and history loading)"
    )
    agent_id: Optional[str] = Field(
        None, description="Agent ID (for associating a session with a specific agent)"
    )
    chatHistory: Optional[List[ChatHistoryItem]] = Field(
        None, description="Historical conversation messages"
    )
    # New version: only pass tool IDs, the backend handles everything automatically
    active_tool_ids: Optional[List[str]] = Field(
        None, description="List of Tool IDs selected by the user (backend auto-fetches config from DB)"
    )


class AgentEvent(BaseModel):
    type: str
