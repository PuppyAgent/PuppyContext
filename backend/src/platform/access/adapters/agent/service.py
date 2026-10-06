"""Existing Agent entry point, now backed by durable run admission."""

import asyncio

from fastapi import HTTPException

from src.config import settings
from src.platform.access.adapters.agent.runtime.admission import Admission, digest
from src.platform.access.adapters.agent.runtime.models import public_run
from src.platform.access.adapters.agent.runtime.repository import RunRepository
from src.platform.authorization.models import ProjectAction


class AgentService:
    def __init__(self, repository=None, admission=None):
        self.repository = repository or RunRepository()
        self.admission = admission or Admission(self.repository)

    def submit(self, user_id, request):
        self.admission.authorize(user_id, request.project_id)
        identity = digest(request.model_dump(mode="json"))
        old = self.repository.receipt(user_id, request.project_id, str(request.request_id))
        if old:
            self.owned(user_id, old["id"])
            if old["input_sha256"] != identity:
                raise HTTPException(409, "Request identity was reused with different input")
            return public_run(old, self.repository.tools(old["id"]))
        if not settings.MANAGED_AI_ENABLED:
            raise HTTPException(503, {"code": "managed_agent_inference_disabled"})
        surface = self.admission.resolve(
            user_id, request.project_id, request.agent_id, request.scope_id
        )
        policy = self.admission.policy(surface)
        if not policy["readonly"]:
            self.admission.authorize(user_id, request.project_id, ProjectAction.CONTENT_WRITE)
        if not policy["model"]:
            raise HTTPException(409, "Configure an Agent model before submitting")
        run = self.repository.rpc(
            "submit",
            user=user_id,
            project=request.project_id,
            agent=surface["id"],
            session=request.session_id,
            request=str(request.request_id),
            digest=identity,
            prompt=request.prompt,
            policy=policy,
            timeout=settings.RUNTIME_AGENT_TIMEOUT_SECONDS,
        )
        return public_run(run)

    def owned(self, user_id, run_id, *, action=ProjectAction.AGENT_READ):
        run = self.repository.get(run_id)
        if run is None or run["user_id"] != user_id:
            raise HTTPException(404, "Run not found")
        self.admission.authorize(user_id, run["project_id"], action)
        if not self.admission.configs.is_visible_to(run["agent_id"], user_id):
            raise HTTPException(404, "Run not found")
        return run

    def session_runs(self, user_id, session_id, limit=50, before=None):
        rows = self.repository.session_runs(user_id, session_id, limit, before)
        if not rows:
            raise HTTPException(404, "Session runs not found")
        # Every session is bound to one Agent and one submitting user.
        self.owned(user_id, rows[0]["id"])
        return [public_run(row) for row in rows]

    def snapshot(self, user_id, run_id):
        run = self.owned(user_id, run_id)
        return public_run(run, self.repository.tools(run_id))

    def command(self, user_id, run_id, command, **values):
        run = self.owned(user_id, run_id, action=ProjectAction.AGENT_RUN)
        if command == "approve":
            self.admission.recheck(run)
        return public_run(
            self.repository.rpc("command", run=run_id, user=user_id, command=command, **values)
        )

    async def events(self, user_id, run_id, after):
        from src.platform.access.adapters.agent.runtime.models import TERMINAL

        while True:
            run = await asyncio.to_thread(self.owned, user_id, run_id)
            sequence = run["sequence"]
            if after > sequence or after < max(0, sequence - 512):
                yield {
                    "event": "reset",
                    "id": sequence,
                    "data": await asyncio.to_thread(self.snapshot, user_id, run_id),
                }
                return
            rows = await asyncio.to_thread(self.repository.events, run_id, after, sequence)
            if rows and rows[0]["sequence"] != after + 1:
                yield {
                    "event": "reset",
                    "id": sequence,
                    "data": await asyncio.to_thread(self.snapshot, user_id, run_id),
                }
                return
            for row in rows:
                after = row["sequence"]
                yield {"event": row["kind"], "id": after, "data": row["payload"]}
            if run["state"] in TERMINAL and after >= sequence:
                return
            if not rows:
                yield {"comment": "keepalive"}
                await asyncio.sleep(1)
