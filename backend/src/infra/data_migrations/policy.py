"""Repository policy for immutable and explicitly staged database changes."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .catalog import DataMigrationCatalog
from .errors import ManifestError
from .schema_history import historical_source, load_baseline

SCHEMA_BASELINE_RELATIVE = Path("supabase/data_migrations/schema_history_baseline.json")
EXTERNAL_STEP_RE = re.compile(r"(?:\bpython\b|\.py\b|\bscripts?/)", re.IGNORECASE)
SCHEMA_MIGRATION_NAME_RE = re.compile(r"^(?P<version>[0-9]{14})_[a-z0-9]+(?:_[a-z0-9]+)*\.sql$")
DESTRUCTIVE_SCHEMA_RE = re.compile(
    r"(?:"
    r"\bDROP\s+(?:TABLE|COLUMN|SCHEMA|TYPE|DOMAIN)\b"
    r"|\bTRUNCATE(?:\s+TABLE)?\b"
    r"|\bALTER\s+TABLE\b[^;]*\bALTER\s+(?:COLUMN\s+)?[a-zA-Z_][a-zA-Z0-9_]*\s+TYPE\b"
    r")",
    re.IGNORECASE,
)
CONTRACT_MARKER_RE = re.compile(
    r"^--\s*requires-data-migration:\s*([0-9]{8,14}_[a-z0-9_]+)\s*$",
    re.MULTILINE,
)
CONTRACT_CHECKSUM_RE = re.compile(
    r"^--\s*data-migration-checksum:\s*([0-9a-f]{64})\s*$",
    re.MULTILINE,
)

HISTORICAL_RELEASE = Path("supabase/releases/20260923_production_catchup.json")


def historical_compatibility(repository: Path) -> set[str]:
    """Admit only unchanged, explicitly reviewed Qubits history for promotion."""
    path = repository / HISTORICAL_RELEASE
    if not path.exists():
        return set()
    try:
        plan = json.loads(path.read_text())
        if plan["api_version"] != 1 or plan["id"] != "20260923_production_catchup":
            raise ValueError("unsupported historical release")
        hashes = plan["schema_sha256"]
        names = set(plan["policy_compatibility"])
        if names != {"20260716000000_remove_workspace_binding.sql"}:
            raise ValueError("unreviewed compatibility entry")
        for name, checksum in hashes.items():
            if SCHEMA_MIGRATION_NAME_RE.fullmatch(name) is None:
                raise ValueError("invalid schema filename")
            source = historical_source(repository, name)
            if hashlib.sha256(source.read_bytes()).hexdigest() != checksum:
                raise ValueError(f"historical schema checksum changed: {name}")
        if not names <= hashes.keys():
            raise ValueError("compatibility entry must have a pinned checksum")
        return names
    except (KeyError, TypeError, ValueError, OSError) as error:
        raise ManifestError(f"invalid historical release: {error}") from error


@dataclass(frozen=True, slots=True)
class ChangedPath:
    status: str
    path: str


def _schema_history_baseline(catalog: DataMigrationCatalog) -> dict[str, str]:
    path = catalog.repository_root / SCHEMA_BASELINE_RELATIVE
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ManifestError(f"cannot read schema history baseline: {error}") from error
    if not isinstance(raw, dict) or raw.get("api_version") != 1:
        raise ManifestError("schema history baseline must use api_version 1")
    checksums = raw.get("schema_sha256")
    if not isinstance(checksums, dict):
        raise ManifestError("schema history baseline must define schema_sha256")
    validated: dict[str, str] = {}
    for name, checksum in checksums.items():
        if not isinstance(name, str) or SCHEMA_MIGRATION_NAME_RE.fullmatch(name) is None:
            raise ManifestError(f"invalid schema history baseline filename: {name!r}")
        if not isinstance(checksum, str) or re.fullmatch(r"[0-9a-f]{64}", checksum) is None:
            raise ManifestError(f"invalid schema history baseline checksum: {name}")
        validated[name] = checksum
    return validated


def git_changed_paths(repository_root: Path, base_ref: str) -> list[ChangedPath]:
    validate_baseline_transition(repository_root, base_ref)
    result = subprocess.run(
        [
            "git",
            "diff",
            "--name-status",
            "--find-renames",
            f"{base_ref}...HEAD",
            "--",
            "supabase/migrations",
            "supabase/data_migrations",
            "supabase/releases",
            "supabase/archive",
            "supabase/baselines",
        ],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ManifestError(result.stderr.strip() or f"cannot diff against {base_ref}")
    changes: list[ChangedPath] = []
    for line in result.stdout.splitlines():
        columns = line.split("\t")
        if len(columns) < 2:
            continue
        status = columns[0]
        # A rename has old and new paths; policy applies to both released facts.
        for path in columns[1:]:
            changes.append(ChangedPath(status=status, path=path))
    return changes


def validate_baseline_transition(root: Path, base_ref: str) -> None:
    """An archive move cannot rewrite released bytes by changing its manifest."""
    baseline = load_baseline(root)
    if not baseline:
        return
    ancestor = subprocess.run(
        ["git", "merge-base", base_ref, "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    old_manifest = subprocess.run(
        ["git", "show", f"{ancestor}:supabase/baselines/b1/manifest.json"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if old_manifest.returncode == 0:
        previous = json.loads(old_manifest.stdout)
        if previous.get("status") == "active" and previous != baseline:
            raise ManifestError("active baseline manifest is immutable; introduce a new baseline")
        if (
            previous["source_migrations"] != baseline["source_migrations"]
            or previous["files"] != baseline["files"]
        ):
            raise ManifestError("baseline source inventory or SQL changed during activation")
    for name, expected in baseline["source_migrations"].items():
        directory = (
            baseline["archive"]
            if old_manifest.returncode == 0 and previous.get("status") == "active"
            else "supabase/migrations"
        )
        source = subprocess.run(
            ["git", "show", f"{ancestor}:{directory}/{name}"],
            cwd=root,
            capture_output=True,
        )
        if source.returncode or hashlib.sha256(source.stdout).hexdigest() != expected:
            raise ManifestError(f"baseline archive does not preserve base-branch SQL: {name}")
    for migration_id, record in baseline["source_data_migrations"].items():
        directory = (
            previous["data_archive"]
            if old_manifest.returncode == 0 and previous.get("data_archive")
            else "supabase/data_migrations"
        )
        prefix = f"{directory}/{migration_id}"
        names = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", f"{ancestor}:{prefix}"],
            cwd=root,
            capture_output=True,
            text=True,
        )
        if names.returncode or set(names.stdout.splitlines()) != set(record["files"]):
            raise ManifestError(f"data archive differs from base-branch inventory: {migration_id}")
        for name, expected in record["files"].items():
            source = subprocess.run(
                ["git", "show", f"{ancestor}:{prefix}/{name}"], cwd=root, capture_output=True
            )
            if source.returncode or hashlib.sha256(source.stdout).hexdigest() != expected:
                raise ManifestError(
                    f"data archive does not preserve base-branch bytes: {migration_id}/{name}"
                )


def validate_repository_policy(
    catalog: DataMigrationCatalog,
    changes: list[ChangedPath],
) -> None:
    artifacts = {item.manifest.id: item for item in catalog.load_all()}
    violations: list[str] = []
    baseline = _schema_history_baseline(catalog)
    historical = historical_compatibility(catalog.repository_root)
    active_baseline = load_baseline(catalog.repository_root)
    archived = active_baseline["source_migrations"] if active_baseline else {}
    archived_data_paths = {
        f"{active_baseline['data_archive']}/{migration_id}/{name}"
        for migration_id, record in (active_baseline or {})
        .get("source_data_migrations", {})
        .items()
        for name in record["files"]
    }

    versions: dict[str, list[str]] = {}
    for migration_path in sorted(
        (catalog.repository_root / "supabase" / "migrations").glob("*.sql")
    ):
        match = SCHEMA_MIGRATION_NAME_RE.fullmatch(migration_path.name)
        if match is None:
            violations.append(
                f"schema migration filename must be timestamped snake_case: "
                f"{migration_path.relative_to(catalog.repository_root)}"
            )
            continue
        versions.setdefault(match.group("version"), []).append(migration_path.name)
        if (
            active_baseline
            and migration_path.name != Path(active_baseline["migration"]).name
            and match.group("version") <= Path(active_baseline["migration"]).name[:14]
        ):
            violations.append(
                f"new schema migrations must follow the active baseline: {migration_path.name}"
            )
    for version, names in versions.items():
        if len(names) > 1:
            violations.append(f"duplicate schema migration version {version}: {', '.join(names)}")

    for name, expected_checksum in baseline.items():
        try:
            migration_path = historical_source(catalog.repository_root, name)
        except ManifestError:
            violations.append(f"grandfathered schema migration is missing: {name}")
            continue
        actual_checksum = hashlib.sha256(migration_path.read_bytes()).hexdigest()
        if actual_checksum != expected_checksum:
            violations.append(f"grandfathered schema migration checksum changed: {name}")

    for change in changes:
        path = change.path
        status = change.status[0]
        if path in archived_data_paths:
            if change.status not in {"A", "R100"}:
                violations.append(f"archived data artifact is immutable: {path}")
            continue
        if path.startswith("supabase/archive/before_b1/data_migrations/"):
            violations.append(f"unregistered data archive: {path}")
            continue
        if path.startswith("supabase/archive/") and path.endswith(".sql"):
            if not active_baseline or path != f"{active_baseline['archive']}/{Path(path).name}":
                violations.append(f"unregistered schema archive: {path}")
            elif Path(path).name not in archived or status not in {"A", "R"}:
                violations.append(f"archived schema SQL is immutable: {path}")
        if path == HISTORICAL_RELEASE.as_posix() and status != "A":
            violations.append("historical release plan is immutable after adoption")
        if path.startswith("supabase/migrations/") and path.endswith(".sql"):
            relative_schema_path = Path(path)
            if len(relative_schema_path.parts) != 3:
                violations.append(f"schema migrations must be direct files: {path}")
                continue
            if status != "A":
                if (
                    active_baseline
                    and path == active_baseline["migration"]
                    and change.status == "R100"
                ):
                    continue
                if (
                    relative_schema_path.name in archived
                    and change.status in {"D", "R100"}
                    and not (catalog.repository_root / path).exists()
                ):
                    # Exact bytes have already been checked in the archive.
                    continue
                violations.append(
                    f"applied/shared schema migrations are immutable; add a forward file: {path}"
                )
                continue
            full_path = catalog.repository_root / path
            if not full_path.is_file():
                continue
            if full_path.is_symlink():
                violations.append(f"schema migrations cannot be symlinks: {path}")
                continue
            if full_path.name in baseline or full_path.name in historical:
                # Pre-governance files may contain legacy patterns, but the
                # baseline permits only their exact, already-shared bytes.
                continue
            if active_baseline and path == active_baseline["migration"]:
                # Generated, verified schema replacement, not a new data step.
                continue
            text = full_path.read_text(encoding="utf-8")
            external_match = EXTERNAL_STEP_RE.search(text)
            if external_match:
                violations.append(
                    f"schema migration names an external application step "
                    f"({external_match.group(0)}): {path}"
                )
            if DESTRUCTIVE_SCHEMA_RE.search(text):
                marker = CONTRACT_MARKER_RE.search(text)
                checksum_marker = CONTRACT_CHECKSUM_RE.search(text)
                if "_contract_" not in full_path.name or marker is None:
                    violations.append(
                        f"destructive schema migration must be a marked contract file: {path}"
                    )
                elif marker.group(1) not in artifacts:
                    violations.append(
                        f"contract references unknown data migration {marker.group(1)}: {path}"
                    )
                elif checksum_marker is None:
                    violations.append(f"contract must pin its data migration checksum: {path}")
                elif checksum_marker.group(1) != artifacts[marker.group(1)].checksum:
                    violations.append(
                        f"contract checksum does not match data migration {marker.group(1)}: {path}"
                    )
                else:
                    reviewed_contract = (
                        artifacts[marker.group(1)].directory / "contract.pending.sql"
                    )
                    if reviewed_contract.is_file() and (
                        full_path.read_bytes() != reviewed_contract.read_bytes()
                    ):
                        violations.append(
                            f"contract must exactly match the reviewed pending contract "
                            f"for {marker.group(1)}: {path}"
                        )

        relative = Path(path)
        if len(relative.parts) >= 3 and relative.parts[:2] == (
            "supabase",
            "data_migrations",
        ):
            migration_id = relative.parts[2]
            if migration_id in {"README.md", "manifest.schema.json"}:
                continue
            if relative == SCHEMA_BASELINE_RELATIVE:
                if status != "A":
                    violations.append("schema history baseline is immutable after adoption")
                continue
            full_path = catalog.repository_root / relative
            if (
                active_baseline
                and change.status in {"D", "R100"}
                and not full_path.exists()
                and f"{active_baseline['data_archive']}/{'/'.join(relative.parts[2:])}"
                in archived_data_paths
            ):
                continue
            if full_path.is_symlink():
                violations.append(f"data migration artifacts cannot be symlinks: {path}")
            # An artifact may be assembled freely in its introducing PR. Once
            # it exists on the base branch, every file is immutable.
            if status != "A":
                violations.append(
                    f"released data migration artifacts are immutable; add a new ID: {path}"
                )

    if violations:
        raise ManifestError("\n".join(f"- {item}" for item in violations))
