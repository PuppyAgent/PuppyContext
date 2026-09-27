"""
Chat REST API — all chat session/message CRUD goes through backend.
Frontend no longer reads Supabase directly.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from src.platform.access.adapters.agent.chat.dependencies import get_chat_service
from src.platform.access.adapters.agent.chat.schemas import (
    CreateSessionRequest,
    MessageResponse,
    SessionResponse,
    UpdateSessionRequest,
)
from src.platform.access.adapters.agent.chat.service import ChatService
from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser
from src.common_schemas import ApiResponse
from src.platform.access.adapters.agent.config.dependencies import get_agent_config_service
from src.platform.access.adapters.agent.config.service import AgentConfigService
from src.platform.authorization.dependencies import get_authorization_service
from src.platform.authorization.models import ProjectAction
from src.platform.authorization.service import AuthorizationService
from src.platform.project.readiness import ProjectReadinessService

_SESSION_NOT_FOUND = "Session not found"

router = APIRouter(prefix="/chat", tags=["chat"])


def _authorize_agent(
    *,
    agent_id: str | None,
    user_id: str,
    action: ProjectAction,
    agent_config: AgentConfigService,
    authorization: AuthorizationService,
) -> str:
    if not agent_id:
        raise HTTPException(status_code=404, detail="Agent not found")
    project_id = agent_config.get_agent_project_id(agent_id)
    if project_id is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    authorization.authorize(project_id, user_id, action)
    if not agent_config.is_visible_to(agent_id, user_id):
        raise HTTPException(status_code=404, detail="Agent not found")
    return project_id


def _session_to_response(s) -> SessionResponse:
    return SessionResponse(
        id=s.id,
        agent_id=s.agent_id,
        title=s.title,
        mode=s.mode,
        created_at=s.created_at,
        updated_at=s.updated_at,
    )


def _message_to_response(m) -> MessageResponse:
    return MessageResponse(
        id=m.id,
        session_id=m.session_id,
        role=m.role,
        content=m.content,
        parts=m.parts,
        created_at=m.created_at,
    )


# ── Sessions ──


@router.post("/sessions", summary="Create a chat session")
async def create_session(
    body: CreateSessionRequest,
    current_user: CurrentUser = Depends(get_current_user),
    chat_service: ChatService = Depends(get_chat_service),
    agent_config: AgentConfigService = Depends(get_agent_config_service),
    authorization: AuthorizationService = Depends(get_authorization_service),
):
    project_id = _authorize_agent(
        agent_id=body.agent_id,
        user_id=current_user.user_id,
        action=ProjectAction.AGENT_RUN,
        agent_config=agent_config,
        authorization=authorization,
    )
    readiness = ProjectReadinessService().resolve(project_id)
    if not readiness.claude_ready:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "project_agent_not_ready",
                "git_state": readiness.git_state,
                "blockers": readiness.blockers,
            },
        )
    session = chat_service.create_session(
        user_id=current_user.user_id,
        agent_id=body.agent_id,
        title=body.title,
        mode=body.mode,
    )
    return ApiResponse.success(data=_session_to_response(session))


@router.get("/sessions", summary="List chat sessions")
async def list_sessions(
    agent_id: Optional[str] = Query(None, description="Filter by agent ID"),
    limit: int = Query(50, ge=1, le=200),
    current_user: CurrentUser = Depends(get_current_user),
    chat_service: ChatService = Depends(get_chat_service),
    agent_config: AgentConfigService = Depends(get_agent_config_service),
    authorization: AuthorizationService = Depends(get_authorization_service),
):
    if agent_id:
        _authorize_agent(
            agent_id=agent_id,
            user_id=current_user.user_id,
            action=ProjectAction.AGENT_READ,
            agent_config=agent_config,
            authorization=authorization,
        )
    sessions = chat_service.list_sessions(
        user_id=current_user.user_id,
        agent_id=agent_id,
        limit=limit,
    )
    visible_sessions = []
    for session in sessions:
        project_id = (
            agent_config.get_agent_project_id(session.agent_id)
            if session.agent_id
            else None
        )
        if (
            project_id is None
            or not authorization.allows(
                project_id, current_user.user_id, ProjectAction.AGENT_READ
            )
            or not agent_config.is_visible_to(
                session.agent_id or "", current_user.user_id
            )
        ):
            continue
        visible_sessions.append(session)
    return ApiResponse.success(
        data=[_session_to_response(s) for s in visible_sessions]
    )


@router.get("/sessions/{session_id}", summary="Get a session")
async def get_session(
    session_id: str,
    current_user: CurrentUser = Depends(get_current_user),
    chat_service: ChatService = Depends(get_chat_service),
    agent_config: AgentConfigService = Depends(get_agent_config_service),
    authorization: AuthorizationService = Depends(get_authorization_service),
):
    session = chat_service.get_session(user_id=current_user.user_id, session_id=session_id)
    if not session:
        raise HTTPException(status_code=404, detail=_SESSION_NOT_FOUND)
    _authorize_agent(
        agent_id=session.agent_id,
        user_id=current_user.user_id,
        action=ProjectAction.AGENT_READ,
        agent_config=agent_config,
        authorization=authorization,
    )
    return ApiResponse.success(data=_session_to_response(session))


@router.patch("/sessions/{session_id}", summary="Update a session")
async def update_session(
    session_id: str,
    body: UpdateSessionRequest,
    current_user: CurrentUser = Depends(get_current_user),
    chat_service: ChatService = Depends(get_chat_service),
    agent_config: AgentConfigService = Depends(get_agent_config_service),
    authorization: AuthorizationService = Depends(get_authorization_service),
):
    existing = chat_service.get_session(
        user_id=current_user.user_id, session_id=session_id
    )
    if not existing:
        raise HTTPException(status_code=404, detail=_SESSION_NOT_FOUND)
    _authorize_agent(
        agent_id=existing.agent_id,
        user_id=current_user.user_id,
        action=ProjectAction.AGENT_RUN,
        agent_config=agent_config,
        authorization=authorization,
    )
    session = chat_service.update_session(
        user_id=current_user.user_id,
        session_id=session_id,
        title=body.title,
    )
    if not session:
        raise HTTPException(status_code=404, detail=_SESSION_NOT_FOUND)
    return ApiResponse.success(data=_session_to_response(session))


@router.delete("/sessions/{session_id}", summary="Delete a session")
async def delete_session(
    session_id: str,
    current_user: CurrentUser = Depends(get_current_user),
    chat_service: ChatService = Depends(get_chat_service),
    agent_config: AgentConfigService = Depends(get_agent_config_service),
    authorization: AuthorizationService = Depends(get_authorization_service),
):
    existing = chat_service.get_session(
        user_id=current_user.user_id, session_id=session_id
    )
    if not existing:
        raise HTTPException(status_code=404, detail=_SESSION_NOT_FOUND)
    _authorize_agent(
        agent_id=existing.agent_id,
        user_id=current_user.user_id,
        action=ProjectAction.AGENT_RUN,
        agent_config=agent_config,
        authorization=authorization,
    )
    ok = chat_service.delete_session(user_id=current_user.user_id, session_id=session_id)
    if not ok:
        raise HTTPException(status_code=404, detail=_SESSION_NOT_FOUND)
    return ApiResponse.success(message="Session deleted")


# ── Messages ──


@router.get("/sessions/{session_id}/messages", summary="List messages in a session")
async def list_messages(
    session_id: str,
    limit: int = Query(200, ge=1, le=500),
    current_user: CurrentUser = Depends(get_current_user),
    chat_service: ChatService = Depends(get_chat_service),
    agent_config: AgentConfigService = Depends(get_agent_config_service),
    authorization: AuthorizationService = Depends(get_authorization_service),
):
    session = chat_service.get_session(
        user_id=current_user.user_id, session_id=session_id
    )
    if not session:
        raise HTTPException(status_code=404, detail=_SESSION_NOT_FOUND)
    _authorize_agent(
        agent_id=session.agent_id,
        user_id=current_user.user_id,
        action=ProjectAction.AGENT_READ,
        agent_config=agent_config,
        authorization=authorization,
    )
    messages = chat_service.list_messages(
        user_id=current_user.user_id,
        session_id=session_id,
        limit=limit,
    )
    return ApiResponse.success(data=[_message_to_response(m) for m in messages])
