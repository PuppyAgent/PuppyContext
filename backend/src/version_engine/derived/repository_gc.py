"""Native collection under a durable, non-expiring repository sweep fence."""

from __future__ import annotations

import uuid

from src.version_engine.derived.object_gc import _run_git_object_gc
from src.version_engine.infrastructure.supabase.ref_authority_repository import (
    RefAuthorityRepository,
)
from src.version_engine.write_engine.git_object_format import encode_object


class _PhysicalStore:
    def __init__(self, store):
        self.store = store

    def __getattr__(self, name):
        return getattr(self.store, name)

    def get_loose(self, oid):
        # Empty trees are intrinsic objects in historical stores; they have no
        # dependencies. Every other object must be read from physical storage.
        format = "sha256" if len(oid) == 64 else "sha1"
        empty, loose = encode_object("tree", b"", object_format=format)
        if oid == empty:
            return loose
        return self.store._backend.get_durable(oid)


class _PhysicalRepo:
    def __init__(self, repo):
        self.repo = repo
        self.store = _PhysicalStore(repo.store)

    def __getattr__(self, name):
        return getattr(self.repo, name)


class RepositoryCollector:
    def __init__(self, control: RefAuthorityRepository):
        self.control = control

    def run(self, repo, **options):
        token = str(uuid.uuid4())
        if options.get("dry_run", True):
            actor = "system:gc-inspection"
            snapshot = self.control.begin_read(repo._project_id, actor, token)
            try:
                roots = self.control.call("get_version_repository_gc_roots", p_project_id=repo._project_id)
                empty, _ = encode_object("tree", b"", object_format=snapshot["object_format"])
                return _run_git_object_gc(_PhysicalRepo(repo), **options, additional_roots=(*roots, empty))
            finally:
                self.control.release(repo._project_id, actor, token)
        snapshot = self.control.begin_gc(repo._project_id, token)
        # No finally-unlock. Unknown S3 timeout, incomplete roots or process
        # failure retain the fence, preventing a paused deleter from racing a
        # subsequent publication. Recovery must prove both worker and remote
        # storage/index I/O quiescence; merely killing a worker is insufficient.
        empty, _ = encode_object("tree", b"", object_format=snapshot["object_format"])
        result = _run_git_object_gc(
            _PhysicalRepo(repo), **options, additional_roots=(*snapshot["roots"], empty),
        )
        if not result.errors and not result.sweep_skipped_for_safety:
            self.control.finish_gc(repo._project_id, token)
        return result
