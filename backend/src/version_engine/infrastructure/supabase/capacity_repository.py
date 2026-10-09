"""Explicit native technical capacity; separate from logical-tree billing.

The canonical caller must supply this admission capability. Low-level dormant
profiles remain available for migration/component tests, never an implicit
fallback when a required capacity RPC or initialized inventory is missing.
"""

from __future__ import annotations

from src.version_engine.infrastructure.supabase.ref_authority_repository import (
    RefAuthorityRepository,
)
from src.version_engine.storage.publication import ClosureManifest


class RepositoryCapacity:
    def __init__(self, control: RefAuthorityRepository):
        self.control = control

    def check(self, project_id: str) -> dict:
        result = self.control.call("check_version_repository_capacity", p_project_id=project_id)
        return self.validate(project_id, result)

    @staticmethod
    def validate(project_id, result):
        if (
            not isinstance(result, dict)
            or result.get("project_id") != project_id
            or result.get("metric") != "git.object_body_bytes"
        ):
            raise RuntimeError("invalid repository capacity contract")
        return result

    def seal(self, project_id: str, actor: str, pin: str, manifest: ClosureManifest) -> dict:
        # Retained proof boundaries have already been accounted for. Only
        # physically verified new facts need capacity reconciliation.
        new_objects = set(manifest.new_objects) if manifest.new_objects is not None else None
        rows = [
            dict(object_id=oid, object_kind=record.kind, body_bytes=record.size)
            for oid, record in sorted(manifest.objects.items())
            if new_objects is None or oid in new_objects
        ]
        for offset in range(0, max(0, len(rows) - 200), 200):
            result = self.control.call(
                "reserve_version_object_capacity",
                p_project_id=project_id,
                p_actor=actor,
                p_pin_id=pin,
                p_objects=rows[offset : offset + 200],
                p_io_id=None,
            )
            if not isinstance(result, dict) or any(
                type(result.get(key)) is not int or result[key] < 0
                for key in ("new_body_bytes", "new_objects")
            ):
                raise RuntimeError("invalid capacity admission result")
        # No raw-seal fallback. This attestation is as trusted as the existing
        # backend physical-closure attestation; SQL additionally fences old issuers.
        tail = ((len(rows) - 1) // 200) * 200 if rows else 0
        result = self.control.call(
            "seal_version_capacity_batch",
            p_project_id=project_id,
            p_actor=actor,
            p_pin_id=pin,
            p_manifest_sha256=manifest.digest,
            p_root_details=manifest.root_details(),
            p_objects=rows[tail:],
        )
        if (
            not isinstance(result, dict)
            or result.get("id") != pin
            or result.get("project_id") != project_id
            or result.get("manifest_sha256") != manifest.digest
            or result.get("object_format") != manifest.object_format
            or result.get("roots") != dict(manifest.roots)
        ):
            raise RuntimeError("invalid capacity publication receipt")
        return result
