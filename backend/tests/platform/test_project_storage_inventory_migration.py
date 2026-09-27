from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from src.infra.data_migrations.schema_history import data_migration_directory

REPOSITORY = Path(__file__).resolve().parents[3]


def _migration_module():
    path = data_migration_directory(REPOSITORY, "20260720_project_storage_inventory") / "run.py"
    spec = importlib.util.spec_from_file_location("storage_inventory_migration", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _S3Stub:
    def list_objects_v2(self, **kwargs):
        if kwargs.get("ContinuationToken") == "objects-page-2":
            return {
                "Contents": [{"Key": "users/user-2/processed/project-2/result"}],
                "IsTruncated": False,
            }
        return {
            "Contents": [{"Key": "users/user-1/raw/project-1/input"}],
            "IsTruncated": True,
            "NextContinuationToken": "objects-page-2",
        }

    def list_multipart_uploads(self, **kwargs):
        if kwargs.get("KeyMarker") == "next-key":
            return {"Uploads": [], "IsTruncated": False}
        return {
            "Uploads": [
                {
                    "Key": "users/user-1/etl_artifacts/project-1/archive",
                    "UploadId": "upload-1",
                }
            ],
            "IsTruncated": True,
            "NextKeyMarker": "next-key",
            "NextUploadIdMarker": "next-upload",
        }


def test_inventory_artifact_is_self_contained_and_scans_all_pages() -> None:
    module = _migration_module()
    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "from src." not in source

    recorded: list[tuple[str, set[tuple[str, str]], dict]] = []
    proof = module.observe(
        _S3Stub(),
        "bucket",
        1000,
        on_page=lambda kind, _entries, principals, checkpoint: recorded.append(
            (kind, principals, checkpoint)
        ),
    )

    assert proof.object_count == 2
    assert proof.multipart_count == 1
    assert len(proof.digest) == 64
    assert recorded[0][1] == {("project-1", "user-1")}
    assert recorded[-1][2]["phase"] == "complete"


def test_inventory_artifact_rejects_incomplete_multipart_pagination() -> None:
    module = _migration_module()
    s3 = _S3Stub()
    s3.list_multipart_uploads = lambda **_kwargs: {  # type: ignore[method-assign]
        "Uploads": [],
        "IsTruncated": True,
        "NextKeyMarker": "only-key",
    }

    with pytest.raises(RuntimeError, match="both pagination markers"):
        module.observe(s3, "bucket", 1000)


def test_inventory_artifact_declares_the_forward_control_plane_repair() -> None:
    manifest = (
        REPOSITORY
        / "supabase/archive/before_b1/data_migrations/20260720_project_storage_inventory/manifest.yml"
    ).read_text(encoding="utf-8")

    assert '  - "20260718000000"' in manifest
