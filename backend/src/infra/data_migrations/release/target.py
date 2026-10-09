"""Database release operations shared by Docker and Railway adapters."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from ..catalog import DataMigrationCatalog
from ..database import PsqlClient
from ..runner import DataMigrationRunner
from .journal import Journal
from .plan import Phase, ReleasePlan
from .schema import Schema


class DatabaseTarget:
    def __init__(self, root: Path, database: PsqlClient, platform, environment: dict[str, str]):
        self.root, self.db, self.platform = root, database, platform
        self.environment = dict(environment)
        self.schema_executor = Schema(root, database)
        self.journal = Journal(root, database)
        self.catalog = DataMigrationCatalog(root)
        self.runner = DataMigrationRunner(self.catalog, database, environment=environment)
        self.plan = None

    def lock(self):
        # All environments targeting the same physical DB share this lock.
        return self.db.advisory_lock("puppyone-environment-release")

    def begin(self, source: str, plan: ReleasePlan) -> bool:
        self.platform.preflight(source)
        self.plan = plan
        self.runner.source_sha = source
        return self.journal.begin(source, plan.checksum)

    def prepare_build(self, source: str) -> None:
        print(json.dumps({"release_phase": "build", "state": "running"}), flush=True)
        self.platform.prepare_build(source)
        self.schema_executor.adopt()

    def pending(self, phase: Phase) -> bool:
        if phase.kind == "schema":
            return bool(self.schema_executor.pending(phase.value))
        applied = self.schema_executor.applied()
        if phase.until_schema in applied:
            return False
        if not applied:
            return True
        receipt = self.db.receipt(phase.value)
        if receipt:
            if (
                receipt.get("artifact_checksum") != self.catalog.get(phase.value).checksum
                or receipt.get("verified") is not True
            ):
                raise ValueError("Stored data completion differs from the approved artifact")
            return False
        return True

    def requires_quiescence(self, pending: list[Phase]) -> bool:
        if any(p.kind == "data" for p in pending):
            return True
        return any(
            "_contract_" in path.name
            for phase in pending
            for path in self.schema_executor.pending(phase.value)
        )

    def checkpoint(self, name: str, result: dict | None = None) -> None:
        self.journal.checkpoint(name, result)
        print(json.dumps({"release_phase": name, "state": "completed"}), flush=True)

    def quiesce(self) -> dict:
        evidence = self.platform.quiesce()
        if (
            evidence.get("producer_stop_verified") is not True
            or evidence.get("queue_drain_verified") is not True
            or evidence.get("old_consumers_exited") is not True
        ):
            raise RuntimeError("Platform did not establish real quiescence")
        # This variable is set only AFTER the adapter has stopped and checked
        # the actual consumers. Operator-supplied yes flags are never trusted.
        self.runner.environment["NATIVE_MIGRATION_WRITERS_DRAINED"] = "yes"
        return evidence

    def backup(self) -> dict:
        evidence = self.platform.backup(self.db)
        if not evidence.get("restore_point_ref") or evidence.get("restore_verified") is not True:
            raise RuntimeError("No verified recovery point")
        return evidence

    def schema(self, phase: Phase) -> None:
        print(json.dumps({"release_phase": phase.id, "state": "running"}), flush=True)
        self.schema_executor.apply(phase.value)

    def data(self, phase: Phase) -> None:
        print(json.dumps({"release_phase": phase.id, "state": "running"}), flush=True)
        if (
            phase.value == "20261007_native_repository_inventory"
            and self.environment.get("RELEASE_TARGET") == "docker"
        ):
            self.db.scalar("SELECT public.configure_repository_entitlement_source('disabled')")
        if phase.prepare == "entrypoint":
            self.prepare_entrypoint()
        self.runner.run(phase.value)
        self.runner.verify(phase.value)

    def prepare_entrypoint(self) -> None:
        path = self.root / "scripts/entrypoint_source_decisions.py"
        spec = importlib.util.spec_from_file_location("release_entrypoint_decisions", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        inventory = json.loads(self.db.scalar(module.INVENTORY))
        # Business classification is an explicit reviewed release input. No
        # heuristic may turn an ambiguous Synchronize binding into an Import.
        decisions = self.platform.entrypoint_decisions(inventory)
        module.approve(self.db, module.reviewed_rows(decisions))
        backup = self.journal.evidence("backup")
        quiescence = self.journal.evidence("quiesce")
        module.freeze(
            self.db,
            json.dumps(quiescence, sort_keys=True).encode(),
            restore_point_ref=backup["restore_point_ref"],
            approved_by="release:" + self.runner.source_sha,
        )

    def verify_database(self) -> None:
        self.schema_executor.verify()

    def deploy(self, source: str) -> None:
        self.platform.deploy(source)

    def accept(self) -> dict:
        return self.platform.accept()

    def complete(self, source: str) -> None:
        self.journal.finish("accepted", "accepted")

    def fail(self, phase: str, error_type: str) -> None:
        self.journal.checkpoint("failure", {"phase": phase, "error_type": error_type})
        self.journal.finish("failed", phase)
        print(
            json.dumps({"release_phase": phase, "state": "failed", "error_type": error_type}),
            flush=True,
        )
