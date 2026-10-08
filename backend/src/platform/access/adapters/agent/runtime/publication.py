"""Run-bound Git synchronization and publication; no project bytes or object codecs."""

import asyncio

from src.platform.access.adapters.agent.runtime.ports import GitTransport
from src.platform.access.adapters.agent.runtime.workspace import (
    require_full_project,
    workspace_state,
)
from src.platform.project.write_lease import ProjectWriteLease
from src.version_engine.adapters.git.run_transport import RunPublication
from src.version_engine.write_engine.native_operation_writer import NativeWriteBase


class PublicationRejected(ValueError):
    """A preflight rejected the candidate before publication."""


def actor(run):
    return f"cloud-agent:{run['id']}:{run['execution_id']}:{run['fence']}"


class Publication:
    def __init__(self, transport: GitTransport):
        self.transport = transport

    def capture(self, run, grant):
        require_full_project(run)
        return workspace_state(run, self.transport.describe(run["project_id"], grant))

    async def exchange(self, run, value, grant, frame):
        publishing = "git-receive-pack" in frame.get("path", "")
        if not publishing:
            return await self.transport.exchange(run["project_id"], grant, frame)
        candidate = value["workspace"].get("tip")
        if run["state"] != "publishing" or run["policy"]["readonly"] or not candidate:
            raise PermissionError("Run has no publication capability")
        binding = RunPublication(run["id"], NativeWriteBase.parse(value["base"]), candidate)
        lease = ProjectWriteLease(run["project_id"], "agent.git.push", reuse_active=False)
        lease.holder_id = actor(run)
        async with lease:
            return await self.transport.exchange(
                run["project_id"], grant, frame, publication=binding
            )

    async def publish(self, run, checkpoint, grant, worker):
        state = checkpoint["workspace"]
        if not state.get("changed"):
            return {"status": "no_changes"}
        if run["policy"]["readonly"]:
            raise PublicationRejected("Read-only Agent cannot publish")
        result = await asyncio.to_thread(self.transport.result, run["project_id"], grant, run["id"])
        if result is not None and result["status"] == "pending":
            raise RuntimeError("Original Git publication is still pending")
        if result is None:
            current = await asyncio.to_thread(self.transport.describe, run["project_id"], grant)
            if NativeWriteBase.parse(current) != NativeWriteBase.parse(checkpoint["base"]):
                return {
                    "status": "conflict",
                    "commit_id": state["tip"],
                    "target_ref": state["target_ref"],
                    "code": "cloud_branch_changed",
                }
            try:
                pushed = await worker.control("push", {"candidate": state["tip"]})
            except Exception:
                # ACK loss must be reconciled by original identity, never by current HEAD.
                result = await asyncio.to_thread(
                    self.transport.result, run["project_id"], grant, run["id"]
                )
                if result is None:
                    raise
            else:
                result = await asyncio.to_thread(
                    self.transport.result, run["project_id"], grant, run["id"]
                )
                if result is None and pushed.get("rejected"):
                    return {
                        "status": "conflict",
                        "commit_id": state["tip"],
                        "target_ref": state["target_ref"],
                        "code": "git_push_rejected",
                    }
        if result is None or result["status"] == "pending":
            raise RuntimeError("Git publication outcome unavailable")
        return {**result, "commit_id": state["tip"], "target_ref": state["target_ref"]}
