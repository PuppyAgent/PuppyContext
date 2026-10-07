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
        result = self.control.call('check_version_repository_capacity', p_project_id=project_id)
        if (not isinstance(result, dict) or result.get('project_id') != project_id
                or result.get('metric') != 'git.object_body_bytes'):
            raise RuntimeError('invalid repository capacity contract')
        return result

    def seal(self, project_id: str, actor: str, pin: str, manifest: ClosureManifest) -> dict:
        # Reconcile the entire physically verified graph, not only the incoming
        # pack or default branch. Gitlinks are absent from this typed closure.
        rows = [dict(object_id=oid, object_kind=record.kind, body_bytes=record.size)
                for oid, record in sorted(manifest.objects.items())]
        for offset in range(0, len(rows), 200):
            result = self.control.call('reserve_version_object_capacity', p_project_id=project_id,
                                       p_actor=actor, p_pin_id=pin, p_objects=rows[offset:offset + 200], p_io_id=None)
            if not isinstance(result, dict) or any(type(result.get(key)) is not int or result[key] < 0
                                                  for key in ('new_body_bytes', 'new_objects')):
                raise RuntimeError('invalid capacity admission result')
        # No raw-seal fallback. This attestation is as trusted as the existing
        # backend physical-closure attestation; SQL additionally fences old issuers.
        result = self.control.call('seal_capacity_version_object_publication', p_project_id=project_id,
                                   p_actor=actor, p_pin_id=pin, p_manifest_sha256=manifest.digest,
                                   p_root_details=manifest.root_details())
        if (not isinstance(result, dict) or result.get('id') != pin or result.get('project_id') != project_id
                or result.get('manifest_sha256') != manifest.digest or result.get('object_format') != manifest.object_format
                or result.get('roots') != dict(manifest.roots)):
            raise RuntimeError('invalid capacity publication receipt')
        return result
