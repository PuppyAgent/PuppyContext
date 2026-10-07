"""Measure the complete API subscription lifetime, including streamed polling."""

import logging
import time
from uuid import uuid4

from src.infra.supabase.instrumentation import DatabaseTrace, database_trace

logger = logging.getLogger(__name__)


class AgentDatabaseTelemetry:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith("/api/v1/agents/"):
            return await self.app(scope, receive, send)
        trace = DatabaseTrace(str(uuid4()))
        started = time.monotonic()
        with database_trace(trace):
            try:
                await self.app(scope, receive, send)
            finally:
                logger.info(
                    "cloud_agent_api_performance",
                    extra={
                        "agent_api_performance": {
                            **trace.report(),
                            "elapsed_seconds": time.monotonic() - started,
                            "method": scope["method"],
                            "operation": getattr(scope.get("route"), "path", "unmatched"),
                            "agent_run_id": str(scope.get("path_params", {}).get("run_id", "")),
                        }
                    },
                )
