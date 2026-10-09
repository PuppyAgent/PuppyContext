"""Owned Docker adapter. The release engine is identical to hosted execution."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

from .backup import create_backup


class Docker:
    def __init__(
        self,
        root: Path,
        compose: list[str],
        *,
        project: str,
        directory: Path,
        accept,
        decisions=None,
    ):
        if (
            not project.startswith("puppyone-install-")
            or "--project-name" not in compose
            or compose[compose.index("--project-name") + 1] != project
        ):
            raise ValueError("Release rehearsal requires its owned Compose project")
        self.root, self.compose, self.project = root, compose, project
        self.directory, self.acceptance, self.decisions = directory, accept, decisions
        self.applications = ["api", "web"]

    def command(self, *args, timeout=1800):
        result = subprocess.run(
            [*self.compose, *args], cwd=self.root, capture_output=True, text=True, timeout=timeout
        )
        if result.returncode:
            diagnostic = self.directory / "docker-operation.log"
            diagnostic.touch(mode=0o600, exist_ok=True)
            diagnostic.write_text(result.stdout + result.stderr)
            raise RuntimeError("Owned Compose release operation failed: " + args[0])
        return result.stdout

    def preflight(self, source):
        services = self.command("config", "--services").splitlines()
        if not {"api", "web", "db", "minio"} <= set(services):
            raise ValueError("Incomplete rehearsal service inventory")

    def prepare_build(self, source):
        self.command("build")

    def quiesce(self):
        self.command("stop", "--timeout", "120", *self.applications)
        running = self.command("ps", "--status", "running", "--services").splitlines()
        if set(self.applications) & set(running):
            raise RuntimeError("A rehearsal writer is still running")
        # This Compose fixture has no separate queue consumers. They have their
        # own real runtime integration suite; do not claim hosted queue drain.
        return {
            "environment": self.project,
            "producer_stop_verified": True,
            "queue_drain_verified": True,
            "old_consumers_exited": True,
            "writers_stopped_at": datetime.now(UTC).isoformat(),
            "records": [
                {
                    "compose_project": self.project,
                    "stopped": self.applications,
                    "queue_consumers": [],
                }
            ],
        }

    def backup(self, db):
        return create_backup(db, self.directory / "backups")

    def entrypoint_decisions(self, inventory):
        if not inventory:
            return {"format_version": 1, "rows": []}
        if self.decisions is None:
            raise ValueError("Rehearsal fixture needs explicit source classifications")
        return self.decisions(inventory)

    def deploy(self, source):
        # Dependencies stay live on their existing volumes. Only the already
        # migrated candidate application starts; no second migration entrypoint.
        self.command(
            "up", "--detach", "--no-deps", "--wait", "--wait-timeout", "420", *self.applications
        )

    def accept(self):
        return self.acceptance()
