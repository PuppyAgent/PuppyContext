from __future__ import annotations

from typing import Any, Optional

from supabase import Client

from src.platform.access.adapters.agent.chat.schemas import (
    ChatMessage,
    ChatMessageCreate,
    ChatSession,
    ChatSessionCreate,
)
from src.infra.supabase.exceptions import handle_supabase_error


class ChatRepositorySupabase:
    def __init__(self, client: Client):
        self._client = client

    # ── Sessions ──

    def create_session(self, data: ChatSessionCreate) -> ChatSession:
        try:
            resp = (
                self._client.table("chat_sessions")
                .insert(data.model_dump(exclude_none=True))
                .execute()
            )
            return ChatSession(**resp.data[0])
        except Exception as e:
            raise handle_supabase_error(e, "create chat session")

    def get_session(self, *, session_id: str, user_id: str) -> Optional[ChatSession]:
        try:
            resp = (
                self._client.table("chat_sessions")
                .select("*")
                .eq("id", session_id)
                .eq("user_id", user_id)
                .execute()
            )
            rows = resp.data if isinstance(resp.data, list) else [resp.data] if resp.data else []
            if not rows:
                return None
            return ChatSession(**rows[0])
        except Exception as e:
            raise handle_supabase_error(e, "get chat session")

    def list_sessions(
        self, *, user_id: str, agent_id: Optional[str] = None, limit: int = 50
    ) -> list[ChatSession]:
        try:
            q = (
                self._client.table("chat_sessions")
                .select("*")
                .eq("user_id", user_id)
            )
            if agent_id is not None:
                q = q.eq("agent_id", agent_id)
            resp = q.order("updated_at", desc=True).limit(limit).execute()
            return [ChatSession(**row) for row in (resp.data or [])]
        except Exception as e:
            raise handle_supabase_error(e, "list chat sessions")

    def update_session(
        self, *, session_id: str, user_id: str, updates: dict[str, Any]
    ) -> Optional[ChatSession]:
        try:
            resp = (
                self._client.table("chat_sessions")
                .update(updates)
                .eq("id", session_id)
                .eq("user_id", user_id)
                .execute()
            )
            rows = resp.data or []
            if not rows:
                return None
            return ChatSession(**rows[0])
        except Exception as e:
            raise handle_supabase_error(e, "update chat session")

    def delete_session(self, *, session_id: str, user_id: str) -> bool:
        try:
            resp = (
                self._client.table("chat_sessions")
                .delete()
                .eq("id", session_id)
                .eq("user_id", user_id)
                .execute()
            )
            return bool(resp.data)
        except Exception as e:
            raise handle_supabase_error(e, "delete chat session")

    # ── Messages ──

    def create_message(self, data: ChatMessageCreate) -> ChatMessage:
        try:
            resp = (
                self._client.table("chat_messages")
                .insert(data.model_dump(exclude_none=True))
                .execute()
            )
            return ChatMessage(**resp.data[0])
        except Exception as e:
            raise handle_supabase_error(e, "create chat message")

    def list_messages(
        self, *, session_id: str, limit: int = 200
    ) -> list[ChatMessage]:
        try:
            resp = (
                self._client.table("chat_messages")
                .select("*")
                .eq("session_id", session_id)
                .order("created_at", desc=False)
                .limit(limit)
                .execute()
            )
            return [ChatMessage(**row) for row in (resp.data or [])]
        except Exception as e:
            raise handle_supabase_error(e, "list chat messages")
