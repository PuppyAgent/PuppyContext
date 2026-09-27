from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# ── DB Models ──

class ChatSession(BaseModel):
    id: str
    user_id: str
    agent_id: Optional[str] = None
    title: Optional[str] = None
    mode: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class ChatMessage(BaseModel):
    id: str
    session_id: str
    role: Literal["user", "assistant"]
    content: Optional[str] = None
    parts: Optional[list[dict[str, Any]]] = None
    created_at: Optional[str] = None


# ── Internal create payloads (for repository) ──

class ChatSessionCreate(BaseModel):
    user_id: str
    agent_id: Optional[str] = None
    title: Optional[str] = None
    mode: str = "agent"


class ChatMessageCreate(BaseModel):
    session_id: str
    role: Literal["user", "assistant"]
    content: Optional[str] = None
    parts: Optional[list[dict[str, Any]]] = Field(default=None)


# ── API Request / Response schemas ──

class CreateSessionRequest(BaseModel):
    agent_id: str = Field(..., description="Agent ID (access point ID)")
    title: Optional[str] = Field(None, description="Optional initial title")
    mode: str = Field("agent", description="Session mode")


class UpdateSessionRequest(BaseModel):
    title: Optional[str] = Field(None, description="New title")


class SessionResponse(BaseModel):
    id: str
    agent_id: Optional[str] = None
    title: Optional[str] = None
    mode: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class MessageResponse(BaseModel):
    id: str
    session_id: str
    role: str
    content: Optional[str] = None
    parts: Optional[list[dict[str, Any]]] = None
    created_at: Optional[str] = None
