from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[3]
MIGRATION = ROOT / (
    "supabase/archive/before_b1/migrations/"
    "20260716020000_project_deletion_storage_and_org_guard.sql"
)
VERSION_STORAGE = ROOT / "backend/src/version_engine/storage/backends/s3.py"
SHADOW_SNAPSHOTS = ROOT / ("backend/src/version_engine/entrypoints/http/shadow_snapshot.py")
INGEST_ROUTER = ROOT / "backend/src/platform/upload/handlers.py"
INGEST_JOBS = ROOT / "backend/src/ingest/file/jobs/jobs.py"
LANDING = ROOT / "backend/src/platform/landing/service.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_cleanup_manifest_covers_every_project_owned_object_layout() -> None:
    migration = _read(MIGRATION)
    version = _read(VERSION_STORAGE)
    shadow = _read(SHADOW_SNAPSHOTS)
    ingest_router = _read(INGEST_ROUTER)
    ingest_jobs = _read(INGEST_JOBS)

    # Canonical + deferred immutable Git object namespaces.
    assert '_CANONICAL_STORAGE_NAMESPACE = "version"' in version
    assert '_DEFERRED_STORAGE_NAMESPACE = "".join(("m", "ut"))' in version
    assert "'version/' || p_project_id || '/'" in migration
    assert "'mut/' || p_project_id || '/'" in migration

    # Current upload staging/final-source namespace.
    assert 'f"projects/{project_id}/files/' in ingest_router
    assert 'f"projects/{request.project_id}/uploads/' in ingest_router
    assert "'projects/' || p_project_id || '/'" in migration

    # Local-only manifests are S3 objects even though their ownership row is
    # removed by the Project FK cascade.
    assert 'f"shadow-snapshots/{project_id}/{snapshot_id}/manifest.json"' in shadow
    assert "'shadow-snapshots/' || p_project_id || '/'" in migration

    # Historical ETL layouts put the user before the Project in the key, so
    # every distinct task creator must be snapshotted into the deletion job.
    assert 'f"users/{creator_id}/etl_artifacts/{project_id}/' in ingest_jobs
    assert 'f"users/{creator_id}/processed/{project_id}/' in ingest_jobs
    assert 'f"users/{_creator_id(task)}/raw/{task.project_id}/' in ingest_jobs
    for ordinal, namespace in enumerate(("etl_artifacts", "processed", "raw"), 1):
        assert f"({ordinal}, '{namespace}')" in migration


def test_anonymous_landing_preview_is_ttl_data_not_project_owned_data() -> None:
    landing = _read(LANDING)

    assert 'LANDING_PREFIX = "landing"' in landing
    assert "Temp S3 prefix for anonymous previews" in landing
    assert "Put an S3 lifecycle rule on this" in landing
    assert 'f"{LANDING_PREFIX}/{ticket_id}/' in landing
