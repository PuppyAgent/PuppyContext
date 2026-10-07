import asyncio
import json
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse

from src.platform.access.adapters.agent.dependencies import get_agent_service
from src.platform.access.adapters.agent.runtime.models import Approval, SubmitRun
from src.platform.auth.dependencies import get_current_user

router = APIRouter(prefix="/agents", tags=["agents"])


@router.post("/runs", status_code=202)
def submit(body: SubmitRun, user=Depends(get_current_user), service=Depends(get_agent_service)):
    return service.submit(user.user_id, body)


@router.get("/requests/{project_id}/{request_id}")
def receipt(
    project_id: str,
    request_id: UUID,
    user=Depends(get_current_user),
    service=Depends(get_agent_service),
):
    return service.receipt(user.user_id, project_id, str(request_id))


@router.get("/sessions/{session_id}/runs")
def session_runs(
    session_id: UUID,
    limit: int = Query(default=50, ge=1, le=100),
    before: datetime | None = None,
    user=Depends(get_current_user),
    service=Depends(get_agent_service),
):
    return service.session_runs(user.user_id, str(session_id), limit, before)


@router.get("/sessions")
def history(
    project_id: str,
    agent_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    user=Depends(get_current_user),
    service=Depends(get_agent_service),
):
    return service.history(user.user_id, project_id, agent_id, limit)


@router.get("/runs/{run_id}")
def snapshot(run_id: UUID, user=Depends(get_current_user), service=Depends(get_agent_service)):
    return service.snapshot(user.user_id, str(run_id))


@router.post("/runs/{run_id}/stop")
def stop(run_id: UUID, user=Depends(get_current_user), service=Depends(get_agent_service)):
    return service.command(user.user_id, str(run_id), "stop")


@router.post("/runs/{run_id}/approvals/{call_id}")
def approve(
    run_id: UUID,
    call_id: str,
    body: Approval,
    user=Depends(get_current_user),
    service=Depends(get_agent_service),
):
    return service.command(
        user.user_id,
        str(run_id),
        "approve",
        call=call_id,
        decision=str(body.decision_id),
        allow=body.allow,
    )


@router.get("/runs/{run_id}/events")
async def events(
    run_id: UUID,
    after: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None),
    user=Depends(get_current_user),
    service=Depends(get_agent_service),
):
    await asyncio.to_thread(service.owned, user.user_id, str(run_id))
    if last_event_id is not None:
        if not last_event_id.isdigit() or len(last_event_id) > 18:
            raise HTTPException(400, "Invalid event cursor")
        after = int(last_event_id)

    async def stream():
        async for event in service.events(user.user_id, str(run_id), after):
            if "comment" in event:
                yield ": keepalive\n\n"
            else:
                yield f"id: {event['id']}\nevent: {event['event']}\ndata: {json.dumps(event['data'], ensure_ascii=False)}\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
