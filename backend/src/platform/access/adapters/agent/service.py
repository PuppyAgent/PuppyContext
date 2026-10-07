"""Existing Agent entry point, now backed by durable run admission."""

import asyncio

from fastapi import HTTPException

from src.config import settings
from src.platform.access.adapters.agent.runtime.admission import Admission, digest
from src.platform.access.adapters.agent.runtime.models import public_run
from src.platform.access.adapters.agent.runtime.ports import RunStore
from src.platform.authorization.models import ProjectAction


class AgentService:
    def __init__(self, repository: RunStore, admission=None):
        self.repository = repository
        self.admission = admission or Admission(self.repository)

    def submit(self, user_id, request):
        identity = digest(request.model_dump(mode="json"))
        context = self.admission.load(
            user_id, request.project_id, request.agent_id, request.scope_id, str(request.request_id)
        )
        old = context.receipt
        if old:
            if old["input_sha256"] != identity:
                raise HTTPException(409, "Request identity was reused with different input")
            return public_run(old)
        if not settings.MANAGED_AI_ENABLED:
            raise HTTPException(503, {"code": "managed_agent_inference_disabled"})
        policy = context.policy
        if not policy["model"]:
            raise HTTPException(409, "Configure an Agent model before submitting")
        run = self.repository.submit_run(
            user=user_id,
            project=request.project_id,
            agent=context.agent_id,
            session=request.session_id,
            request=str(request.request_id),
            digest=identity,
            prompt=request.prompt,
            policy=policy,
            timeout=settings.RUNTIME_AGENT_TIMEOUT_SECONDS,
        )
        return public_run(run)

    def view(self, user_id, run_id, after=0, *, action=ProjectAction.AGENT_READ):
        value = self.repository.load_run_view(user_id, run_id, after)
        return self.authorize_view(value, user_id, action=action)

    def authorize_view(self, value, user_id, *, action=ProjectAction.AGENT_READ):
        if not value:
            raise HTTPException(404, "Run not found")
        run = value["run"]
        self.admission.authorization.authorize_facts(
            value["facts"], run["project_id"], user_id, action
        )
        self.admission.visible(value["surface"], user_id, run["project_id"])
        return value

    def owned(self, user_id, run_id, *, action=ProjectAction.AGENT_READ):
        return self.view(user_id, run_id, action=action)["run"]

    def session_runs(self, user_id, session_id, limit=50, before=None):
        value = self.repository.load_session_view(user_id, session_id, limit, before)
        if not value:
            raise HTTPException(404, "Session runs not found")
        rows = value["runs"]
        self.authorize_view({**value, "run": rows[0]}, user_id)
        return [public_run(row) for row in rows]

    def history(self, user_id, project_id, agent_id, limit=50):
        value = self.repository.load_history_view(user_id, project_id, agent_id, limit)
        if not value:
            raise HTTPException(404, "Agent not found")
        self.admission.authorization.authorize_facts(
            value["facts"], project_id, user_id, ProjectAction.AGENT_READ
        )
        self.admission.visible(value["surface"], user_id, project_id)
        return value["sessions"]

    def receipt(self, user_id, project_id, request_id):
        value = self.repository.load_receipt_view(user_id, project_id, request_id)
        if not value:
            raise HTTPException(404, "Request not found")
        self.authorize_view(value, user_id)
        return public_run(value["run"], value["tools"])

    def snapshot(self, user_id, run_id):
        value = self.view(user_id, run_id)
        return public_run(value["run"], value["tools"])

    def command(self, user_id, run_id, command, **values):
        value = self.view(user_id, run_id, action=ProjectAction.AGENT_RUN)
        return public_run(
            self.repository.command_run(
                run=run_id, user=user_id, command=command, revision=value["revision"], **values
            )
        )

    async def events(self, user_id, run_id, after):
        from src.platform.access.adapters.agent.runtime.models import TERMINAL

        while True:
            value = await asyncio.to_thread(self.view, user_id, run_id, after)
            run = value["run"]
            sequence = run["sequence"]
            if after > sequence or after < max(0, sequence - 512):
                yield {
                    "event": "reset",
                    "id": sequence,
                    "data": public_run(run, value["tools"]),
                }
                return
            rows = value["events"]
            if rows and rows[0]["sequence"] != after + 1:
                yield {
                    "event": "reset",
                    "id": sequence,
                    "data": public_run(run, value["tools"]),
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
