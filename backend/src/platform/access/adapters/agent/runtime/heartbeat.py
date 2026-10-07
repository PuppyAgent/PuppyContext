"""The renewal loop has no admission/configuration service dependency."""

import asyncio

from src.platform.access.adapters.agent.runtime.models import TERMINAL
from src.platform.access.adapters.agent.runtime.ports import RenewalPort


class EndRun(Exception):
    def __init__(self, state, code):
        self.state, self.code = state, code
        super().__init__(code)


class ExecutionLease:
    def __init__(self, port: RenewalPort):
        self.port = port

    async def check(self, run):
        try:
            status = await asyncio.to_thread(self.port.renew_execution, run)
        except Exception as exc:
            # A failed renewal never grants permission to continue.
            raise EndRun("outcome_unknown", "execution_fenced") from exc
        if status["code"] and run["state"] not in TERMINAL:
            raise EndRun(
                "stopped" if status["code"] == "stop_requested" else "failed", status["code"]
            )
        return status

    async def run(self, current, touch, *, interval=5):
        while True:
            await asyncio.sleep(interval)
            await self.check(current())
            await touch()
