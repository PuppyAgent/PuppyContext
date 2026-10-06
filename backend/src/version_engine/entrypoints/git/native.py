"""Authenticated canonical native Git dispatch, separate from legacy projections.

Authority is selected by the manager from PG, not a request header or URL hint.
No policy enrollment, authority switch or legacy-view widening happens here.
"""
from __future__ import annotations

from fastapi import HTTPException

from src.version_engine.adapters.git import execution as limits
from src.version_engine.adapters.git.native_repository import (
    NativeGitRepository,
    PublicationIndeterminateError,
)
from src.version_engine.read.repository_snapshot import repository_snapshot
from src.version_engine.write_engine.ref_transaction import admitted_actor


class NativeGitEndpoint:
    def __init__(self, service, grant, audit):
        self.actor = admitted_actor(grant, service.project_id, write=False)
        self.grant = grant
        self.audit = audit
        self.repository = NativeGitRepository(service)

    def record_audit(self, event_type, actor, detail):
        self.audit.record(event_type, actor, detail)

    def info_refs(self, service, *, protocol=""):
        return self.repository.info_refs(self.grant, service, protocol=protocol)

    def upload(self, path, *, protocol=""):
        return self.repository.upload(self.grant, path, protocol=protocol)

    def receive(self, path):
        try:
            return self.repository.receive(self.grant, path)
        except PublicationIndeterminateError as exc:
            # Do not emit an `ng` report for an operation that may be durable.
            raise HTTPException(503, "Git publication outcome unavailable; fetch refs before retrying",
                                headers={"Cache-Control": "no-store"}) from exc

    def health(self):
        service = self.repository.service
        with repository_snapshot(service.control, service.backend, self.grant,
                                 project_id=service.project_id) as snapshot:
            # Health is a physical diagnostic, not refs-only advertisement. Do
            # not claim an unavailable graph is healthy, truncate it, or repair it.
            if snapshot.roots:
                service.verifier.verify(snapshot.roots, progress=snapshot.check_live)
            revision = snapshot.revision(allow_absent=True)
            writable = True
            try:
                admitted_actor(self.grant, service.project_id, write=True)
            except PermissionError:
                writable = False
            wire = snapshot.to_wire()
            empty = revision.commit_oid is None
            return {
                "project_id": service.project_id, "scope_path": "", "scope_excludes": [],
                "repository_profile": "native", "object_format": service.object_format,
                "generation": wire["generation"], "ref_sequence": wire["ref_sequence"],
                "capabilities": {"full_project_git": True, "scope_git": False,
                                 "protocol_versions": [0, 1, 2], "object_format": service.object_format},
                "limits": {"receive_pack_bytes": limits.MAX_PACK_BYTES,
                           "object_body_bytes": limits.MAX_OBJECT_BYTES, "graph_body_bytes": limits.MAX_GRAPH_BYTES,
                           "objects": limits.MAX_OBJECTS, "refs": limits.MAX_REFS,
                           "worker_seconds": limits.MAX_SECONDS, "concurrent_workers_per_process": 2,
                           "object_cache_bytes_per_request": 8 * 1024**2,
                           "fetch_repository_scratch_bytes": 0,
                           "temporary_bytes_excluding_input": limits.MAX_GRAPH_BYTES},
                "health": "empty" if empty else "healthy",
                "git_head": revision.commit_oid, "canonical_head": revision.commit_oid,
                "history_cut": False, "git_usable": True, "clone_usable": True,
                "fetch_usable": True, "push_usable": writable and wire["write_state"] == "active",
                "read_only": not writable,
                "reason": "Selected HEAD is unborn" if empty else "Native object graph verified",
                "recommended_actions": [{"type": "none", "label": "No storage repair required"}],
            }

    def rebuild(self):
        service = self.repository.service
        admitted_actor(self.grant, service.project_id, write=True)
        service.control.check_write(service.project_id, self.actor)
        self.health()
        service.control.check_write(service.project_id, self.actor)
        # Native transport reads canonical objects directly; there is no bare
        # repository to reconstruct and no cache required for correctness.
        return {"repository_profile": "native", "variants": []}


def select_native_git_endpoint(manager, project_id, auth, *, legacy_route=False):
    service = manager.get_native_service(project_id)
    if service is None:
        return None
    if legacy_route:
        # A legacy locator's pinned projection is not a full-repository grant.
        # Migration must supply its compatibility mapping before enabling it.
        raise HTTPException(503, "Legacy repository view is unavailable")
    return NativeGitEndpoint(service, auth.get("_runtime_grant"), manager.get_audit(project_id))
