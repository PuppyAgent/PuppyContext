from __future__ import annotations

from typing import Any, Optional

from src.platform.access.adapters.agent.chat.repository import ChatRepositorySupabase
from src.platform.access.adapters.agent.chat.schemas import (
    ChatMessage,
    ChatMessageCreate,
    ChatSession,
    ChatSessionCreate,
)
from src.exceptions import BusinessException
from src.platform.access.model_repository import AccessModelRepository


def _generate_session_title(first_message: str, max_len: int = 30) -> str:
    cleaned = (first_message or "").replace("\n", " ").strip()
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[:max_len] + "..."


def _enforce_agent_not_paused(agent_id: str) -> None:
    """Reject session creation if the agent access surface is paused.

    `agent_id` here is the access_surfaces row id (kind='agent') for the
    chat agent the user is talking to. Pausing that surface from the access
    page is meant to act as a hard "this agent is offline" toggle —
    without this check it would only flip a label in the UI and the
    chat would continue to work, which contradicts the user-facing
    affordance.

    We deliberately swallow lookup failures (returning silently when
    the surface can't be fetched) so transient DB issues don't lock
    every chat session out; a missing-or-unreadable surface falls
    back to the prior behaviour of "let the request through".
    """
    if not agent_id:
        return
    try:
        connector = AccessModelRepository().get(agent_id)
    except Exception:
        return
    if connector is None:
        return
    if connector.status == "paused":
        raise BusinessException(
            "This agent is currently paused. Resume it from the Access page "
            "to start chatting."
        )


class ChatService:
    def __init__(self, repo: ChatRepositorySupabase):
        self.repo = repo

    # ── Session CRUD ──

    def create_session(
        self, *, user_id: str, agent_id: str, title: Optional[str] = None, mode: str = "agent"
    ) -> ChatSession:
        _enforce_agent_not_paused(agent_id)
        return self.repo.create_session(
            ChatSessionCreate(user_id=user_id, agent_id=agent_id, title=title, mode=mode)
        )

    def get_session(self, *, user_id: str, session_id: str) -> Optional[ChatSession]:
        return self.repo.get_session(session_id=session_id, user_id=user_id)

    def list_sessions(
        self, *, user_id: str, agent_id: Optional[str] = None, limit: int = 50
    ) -> list[ChatSession]:
        return self.repo.list_sessions(user_id=user_id, agent_id=agent_id, limit=limit)

    def update_session(
        self, *, user_id: str, session_id: str, title: Optional[str] = None
    ) -> Optional[ChatSession]:
        updates: dict[str, Any] = {}
        if title is not None:
            updates["title"] = title
        if not updates:
            return self.get_session(user_id=user_id, session_id=session_id)
        return self.repo.update_session(session_id=session_id, user_id=user_id, updates=updates)

    def delete_session(self, *, user_id: str, session_id: str) -> bool:
        return self.repo.delete_session(session_id=session_id, user_id=user_id)

    # ── Message operations ──

    def list_messages(self, *, user_id: str, session_id: str, limit: int = 200) -> list[ChatMessage]:
        session = self.repo.get_session(session_id=session_id, user_id=user_id)
        if session is None:
            return []
        return self.repo.list_messages(session_id=session_id, limit=limit)

    def add_user_message(self, *, session_id: str, content: str) -> ChatMessage:
        return self.repo.create_message(
            ChatMessageCreate(session_id=session_id, role="user", content=content)
        )

    def add_assistant_message(
        self, *, session_id: str, content: str, parts: Optional[list[dict[str, Any]]] = None,
    ) -> ChatMessage:
        return self.repo.create_message(
            ChatMessageCreate(session_id=session_id, role="assistant", content=content, parts=parts)
        )

    # ── Helpers used by the agent SSE flow ──

    def ensure_session(
        self, *, user_id: str, session_id: Optional[str], agent_id: Optional[str] = None, mode: str = "agent"
    ) -> tuple[str, bool]:
        if session_id:
            existing = self.repo.get_session(session_id=session_id, user_id=user_id)
            if existing is None:
                raise PermissionError("Invalid session_id for current user")
            if agent_id and existing.agent_id and existing.agent_id != agent_id:
                raise PermissionError(
                    "session_id is bound to a different Project Agent"
                )
            # Re-check pause state on every continuation: a session opened
            # while the agent was active stays valid, but if the user
            # later pauses the agent we want subsequent sends to bounce
            # off the same gate as a fresh session creation. Use the
            # session's bound agent_id when no override is supplied.
            target_agent_id = agent_id or existing.agent_id
            _enforce_agent_not_paused(target_agent_id or "")
            return session_id, False
        created = self.create_session(user_id=user_id, agent_id=agent_id or "", mode=mode)
        return created.id, True

    def maybe_set_title_on_first_message(
        self, *, user_id: str, session_id: str, first_message: str
    ) -> None:
        title = _generate_session_title(first_message)
        self.repo.update_session(session_id=session_id, user_id=user_id, updates={"title": title})

    def load_history_for_llm(
        self, *, user_id: str, session_id: str, limit: int = 60
    ) -> list[dict[str, str]]:
        msgs = self.repo.list_messages(session_id=session_id, limit=limit)
        return [
            {"role": m.role, "content": m.content or ""}
            for m in msgs
            if m.role in ("user", "assistant") and m.content
        ]
