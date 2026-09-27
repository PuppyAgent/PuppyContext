"""Checksum-pinned history and ID resolution for active and archived data jobs."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .errors import ManifestError

B1_MANIFEST = Path("supabase/baselines/b1/manifest.json")
SQL_NAME = re.compile(r"[0-9]{14}_[a-z0-9_]+\.sql")


def load_baseline(root: Path) -> dict | None:
    path = root / B1_MANIFEST
    if not path.exists():
        return None
    try:
        manifest = json.loads(path.read_text())
        if manifest.get("status") == "candidate":
            return None
        if (
            manifest["api_version"] != 1
            or manifest["id"] != "b1"
            or manifest["status"] != "active"
            or manifest["archive"] != "supabase/archive/before_b1/migrations"
            or manifest["migration"] != "supabase/migrations/20260926000000_baseline_b1.sql"
        ):
            raise ValueError("unsupported baseline")
        archive = root / manifest["archive"]
        sources = manifest["source_migrations"]
        if not sources or set(sources) != {p.name for p in archive.glob("*.sql")}:
            raise ValueError("archive inventory differs")
        for name, checksum in sources.items():
            if not SQL_NAME.fullmatch(name):
                raise ValueError("invalid archived migration filename")
            verify_file(archive / name, checksum)
            if (root / "supabase/migrations" / name).exists():
                raise ValueError("covered source remains in active migrations")
        verify_file(root / manifest["migration"], manifest["files"]["baseline.sql"])
        data_archive = root / manifest["data_archive"]
        if manifest["data_archive"] != "supabase/archive/before_b1/data_migrations":
            raise ValueError("unsupported data archive")
        sources_data = manifest["source_data_migrations"]
        if set(sources_data) != set(manifest["data_jobs"]):
            raise ValueError("data archive compatibility inventory differs")
        if set(sources_data) != {p.name for p in data_archive.iterdir() if p.is_dir()}:
            raise ValueError("data archive inventory differs")
        for migration_id, record in sources_data.items():
            if not re.fullmatch(r"[0-9]{8,14}_[a-z0-9_]+", migration_id):
                raise ValueError("invalid archived data migration ID")
            directory = data_archive / migration_id
            if directory.is_symlink():
                raise ValueError("data archive cannot contain symlinks")
            actual_files = {
                p.relative_to(directory).as_posix()
                for p in directory.rglob("*")
                if p.is_file() and "__pycache__" not in p.relative_to(directory).parts
            }
            if actual_files != set(record["files"]):
                raise ValueError(f"data archive file inventory differs: {migration_id}")
            for name, checksum in record["files"].items():
                if Path(name).is_absolute() or ".." in Path(name).parts:
                    raise ValueError("invalid archived data path")
                verify_file(directory / name, checksum)
            if (root / "supabase/data_migrations" / migration_id).exists():
                raise ValueError(f"archived data migration remains active: {migration_id}")
        return manifest
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise ManifestError(f"invalid active baseline: {error}") from error


def verify_file(path: Path, checksum: str) -> None:
    if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
        raise ValueError(f"immutable SQL checksum changed: {path.name}")


def data_migration_directories(root: Path) -> list[Path]:
    """One immutable ID namespace, regardless of the artifact's storage location."""
    active = root / "supabase/data_migrations"
    directories = [active]
    baseline = load_baseline(root)
    if baseline:
        directories.append(root / baseline["data_archive"])
    return directories


def data_migration_directory(root: Path, migration_id: str) -> Path:
    if not isinstance(migration_id, str) or not re.fullmatch(
        r"[0-9]{8,14}_[a-z0-9_]+", migration_id
    ):
        raise ManifestError("invalid data migration ID")
    matches = [
        directory / migration_id
        for directory in data_migration_directories(root)
        if (directory / migration_id).is_dir()
    ]
    if len(matches) != 1 or matches[0].is_symlink() or not (matches[0] / "manifest.yml").is_file():
        raise ManifestError(f"expected one immutable data migration: {migration_id}")
    return matches[0]


def historical_source(root: Path, name: str) -> Path:
    if not SQL_NAME.fullmatch(name):
        raise ManifestError("invalid schema filename")
    active = root / "supabase/migrations" / name
    if active.is_file():
        return active
    baseline = load_baseline(root)
    if baseline and name in baseline["source_migrations"]:
        return root / baseline["archive"] / name
    raise ManifestError(f"schema source is missing: {name}")


def covered_versions(baseline: dict) -> set[str]:
    return {name[:14] for name in baseline["source_migrations"]}


def baseline_version(baseline: dict) -> str:
    return Path(baseline["migration"]).name[:14]


def data_job_coverage(root: Path, applied: set[str], migration_id: str) -> tuple[set[str], bool]:
    baseline = load_baseline(root)
    if not baseline or baseline_version(baseline) not in applied:
        return applied, False
    disposition = baseline["data_jobs"].get(migration_id)
    return applied | covered_versions(baseline), disposition == "retired"
