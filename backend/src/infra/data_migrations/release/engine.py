"""Ordered release execution. Target adapters perform actual I/O, never attest it."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Protocol

from .plan import Phase, ReleasePlan


class Target(Protocol):
    def lock(self) -> AbstractContextManager: ...
    def begin(self, source: str, plan: ReleasePlan) -> bool: ...
    def prepare_build(self, source: str) -> None: ...
    def pending(self, phase: Phase) -> bool: ...
    def requires_quiescence(self, pending: list[Phase]) -> bool: ...
    def checkpoint(self, name: str, result: dict | None = None) -> None: ...
    def quiesce(self) -> dict: ...
    def backup(self) -> dict: ...
    def schema(self, phase: Phase) -> None: ...
    def data(self, phase: Phase) -> None: ...
    def verify_database(self) -> None: ...
    def deploy(self, source: str) -> None: ...
    def accept(self) -> dict: ...
    def complete(self, source: str) -> None: ...
    def fail(self, phase: str, error_type: str) -> None: ...


class Release:
    def __init__(self, plan: ReleasePlan, target: Target):
        self.plan = plan
        self.target = target

    def run(self, source: str) -> dict:
        """The environment lock covers preparation, migration AND app acceptance.

        Failure never automatically starts an old binary against a changed schema.
        Retrying rechecks actual phase postconditions, not a stale job checkbox.
        """
        target = self.target
        phase_name = "admission"
        with target.lock() as guard:

            def fence():
                guard.assert_held()

            fence()
            if not target.begin(source, self.plan):
                return {"state": "already_accepted", "source_sha": source}
            try:
                phase_name = "build"
                target.prepare_build(source)
                fence()
                # Pending data work or destructive schema requires one coordinated
                # pause. Pure additive SQL releases keep the old application live.
                pending = [p for p in self.plan.phases if target.pending(p)]
                if target.requires_quiescence(pending):
                    phase_name = "quiesce"
                    target.checkpoint(phase_name, target.quiesce())
                    fence()
                    phase_name = "backup"
                    target.checkpoint(phase_name, target.backup())
                for phase in self.plan.phases:
                    fence()
                    phase_name = phase.id
                    if not target.pending(phase):
                        continue
                    if phase.kind == "schema":
                        target.schema(phase)
                    else:
                        target.data(phase)
                    target.checkpoint(phase.id)
                phase_name = "database-verification"
                target.verify_database()
                fence()
                target.checkpoint(phase_name)
                phase_name = "deployment"
                target.deploy(source)
                fence()
                target.checkpoint(phase_name)
                phase_name = "acceptance"
                acceptance = target.accept()
                fence()
                target.checkpoint(phase_name, acceptance)
                target.complete(source)
                return {"state": "accepted", "source_sha": source, "acceptance": acceptance}
            except Exception as error:
                target.fail(phase_name, type(error).__name__)
                raise
