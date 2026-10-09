"""Versioned, checksum-bound upgrade phases; no executable commands in JSON."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from ..catalog import DataMigrationCatalog


@dataclass(frozen=True)
class Phase:
    id: str
    kind: str
    value: str
    until_schema: str | None = None
    prepare: str | None = None


@dataclass(frozen=True)
class ReleasePlan:
    phases: tuple[Phase, ...]
    checksum: str
    schema_versions: frozenset[str]

    @classmethod
    def load(cls, root: Path) -> ReleasePlan:
        path = root / "supabase/releases/upgrade-plan.json"
        document = json.loads(path.read_text())
        if set(document) != {"format_version", "phases"} or document["format_version"] != 1:
            raise ValueError("Invalid release plan version")
        catalog = DataMigrationCatalog(root)
        migrations = sorted((root / "supabase/migrations").glob("*.sql"))
        versions = frozenset(p.name[:14] for p in migrations)
        if len(versions) != len(migrations):
            raise ValueError("Duplicate schema version")
        digest = hashlib.sha256(path.read_bytes())
        phases = []
        seen = set()
        data_seen = set()
        boundary = ""
        for item in document["phases"]:
            phase = Phase(**item)
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", phase.id) or phase.id in seen:
                raise ValueError("Invalid or duplicate release phase")
            seen.add(phase.id)
            if phase.kind == "schema":
                end = max(versions) if phase.value == "latest" else phase.value
                if end not in versions or end <= boundary or phase.prepare or phase.until_schema:
                    raise ValueError("Schema phases must advance through known versions")
                for migration in migrations:
                    if boundary < migration.name[:14] <= end:
                        body = migration.read_text()
                        requirement = re.search(r"^-- requires-data-migration: (\w+)$", body, re.M)
                        if requirement and requirement[1] not in data_seen:
                            raise ValueError(f"Unscheduled data prerequisite: {migration.name}")
                        if requirement:
                            checksum = re.search(
                                r"^-- data-migration-checksum: ([0-9a-f]{64})$", body, re.M
                            )
                            if not checksum or checksum[1] != catalog.get(requirement[1]).checksum:
                                raise ValueError("Contract artifact checksum mismatch")
                boundary = end
            elif phase.kind == "data":
                artifact = catalog.get(phase.value)
                if phase.value in data_seen or any(
                    v > boundary for v in artifact.manifest.requires_schema
                ):
                    raise ValueError("Data phase lacks its schema prerequisites")
                if phase.until_schema is not None and (
                    phase.until_schema not in versions or phase.until_schema <= boundary
                ):
                    raise ValueError("Invalid data phase retirement boundary")
                if phase.prepare not in {None, "entrypoint"}:
                    raise ValueError("Unknown preparation handler")
                digest.update(artifact.checksum.encode())
                data_seen.add(phase.value)
            else:
                raise ValueError("Unknown release phase kind")
            phases.append(phase)
        if boundary != max(versions) or not phases or phases[-1].kind != "schema":
            raise ValueError("Release plan must cover the complete candidate schema")
        for migration in migrations:
            digest.update(migration.name.encode())
            digest.update(migration.read_bytes())
        return cls(tuple(phases), digest.hexdigest(), versions)
