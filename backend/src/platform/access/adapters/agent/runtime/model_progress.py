"""Track the handoff from completed inference to sandbox acknowledgement.

A renewable execution lease proves ownership, not forward progress. Waiting
for a tool approval is deliberately outside this deadline.
"""

import time

from src.platform.access.adapters.agent.runtime.heartbeat import EndRun


class ModelProgress:
    def __init__(self, *, timeout=30, clock=time.monotonic):
        self.timeout, self.clock = timeout, clock
        self.request = None
        self.completed_at = None

    def started(self, request):
        self.request, self.completed_at = request, None

    def delivered(self, request):
        if self.request == request:
            self.completed_at = self.clock()

    def acknowledged(self):
        self.request, self.completed_at = None, None

    def check(self):
        if self.completed_at is not None and self.clock() - self.completed_at >= self.timeout:
            raise EndRun("failed", "worker_response_timeout")
