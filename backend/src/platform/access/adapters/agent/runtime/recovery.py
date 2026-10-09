"""Link conversation/receipts to immutable provider recovery material."""

from src.platform.scope_sandbox.execution.worker_port import WorkspaceWorker


class Recovery:
    async def after_tool(self, worker: WorkspaceWorker, value, frame):
        # Unknown tools and failed bash/edit calls can still have changed files.
        mutating = frame["name"] not in {"read", "ls", "find", "grep"}
        result = {**value, "reason": "after_tool"}
        if mutating:
            result["recovery"] = await worker.snapshot()
        return result

    async def finalize(self, worker: WorkspaceWorker, value, run):
        state = await worker.control("finalize", {"message": "Agent run " + run["id"]})
        result = {**value, "workspace": {**value["workspace"], **state}}
        if state["changed"]:
            result["recovery"] = await worker.snapshot()
        return result
