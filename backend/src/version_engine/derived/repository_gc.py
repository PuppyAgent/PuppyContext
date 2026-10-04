"""Native collection under a durable, non-expiring repository sweep fence."""

from __future__ import annotations

import uuid

from src.version_engine.derived.object_gc import _run_git_object_gc
from src.version_engine.domain.errors import ObjectNotFoundError
from src.version_engine.infrastructure.supabase.ref_authority_repository import (
    RefAuthorityRepository,
)
from src.version_engine.storage.mutation_context import collection_storage
from src.version_engine.write_engine.git_object_format import encode_object, object_id_bytes


class _InventoryBackend:
    def __init__(self, backend, metadata):
        self.backend, self.metadata = backend, metadata

    def __getattr__(self, name):
        return getattr(self.backend, name)

    def all_hashes_with_metadata(self):
        return self.metadata


class _PhysicalStore:
    def __init__(self, store, metadata=None):
        self.store = store
        self.metadata = metadata
        self._backend = store._backend if metadata is None else _InventoryBackend(store._backend, metadata)

    def all_hashes(self):
        return self.store.all_hashes() if self.metadata is None else list(self.metadata)

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
    def __init__(self, repo, metadata=None):
        self.repo = repo
        self.store = _PhysicalStore(repo.store, metadata)

    def __getattr__(self, name):
        return getattr(self.repo, name)


class RepositoryCollector:
    def __init__(self, control: RefAuthorityRepository):
        self.control = control

    def _capacity_inventory(self, repo, token, object_format):
        cursor, allocations = '', {}
        width = object_id_bytes(object_format) * 2
        while True:
            result = self.control.call('get_version_repository_capacity_inventory',
                                       p_project_id=repo._project_id, p_token=token, p_after=cursor, p_limit=200)
            if not isinstance(result, dict) or not isinstance(result.get('objects'), dict):
                raise RuntimeError('invalid capacity inventory response')
            page = result['objects']
            if len(page) > 200:
                raise RuntimeError('capacity inventory page budget exceeded')
            for oid, facts in page.items():
                if (len(oid) != width or oid == '0' * width or not set(oid) <= set('0123456789abcdef') or oid <= cursor
                        or not isinstance(facts, dict) or type(facts.get('unsettled')) is not bool
                        or not isinstance(facts.get('created_at'), str)):
                    raise RuntimeError('invalid capacity inventory object')
            allocations.update(page)
            if len(allocations) > 1_000_000:
                raise RuntimeError('capacity inventory object budget exceeded')
            if len(page) < 200:
                break
            cursor = max(page)
        if not allocations:
            return None, ()
        backend = repo.store._backend
        metadata = dict(backend.all_hashes_with_metadata())
        protected = []
        for oid, facts in allocations.items():
            if facts['unsettled']:
                protected.append(oid)
            if oid not in metadata:
                # A failed reservation or post-DELETE SQL outage can leave no
                # physical/index key to enumerate. Confirm actual absence, not
                # just a listing miss, before making the allocation actionable.
                try:
                    backend.get_durable(oid)
                except ObjectNotFoundError:
                    metadata[oid] = {'size': 0, 'last_modified': facts['created_at'], 'capacity_missing': True}
                else:
                    raise RuntimeError('capacity object missing from physical inventory')
        return metadata, tuple(protected)

    def run(self, repo, **options):
        backend = getattr(repo.store, "_backend", None)
        bound_project = getattr(backend, "publication_project_id", None)
        if bound_project is not None and bound_project != repo._project_id:
            raise ValueError("collection backend belongs to another Project")
        token = str(uuid.uuid4())
        if options.get("dry_run", True):
            actor = "system:gc-inspection"
            snapshot = self.control.begin_read(repo._project_id, actor, token)
            try:
                roots = self.control.call("get_version_repository_gc_roots", p_project_id=repo._project_id)
                empty, _ = encode_object("tree", b"", object_format=snapshot["object_format"])
                metadata, protected = self._capacity_inventory(repo, token, snapshot['object_format'])
                return _run_git_object_gc(_PhysicalRepo(repo, metadata), **options,
                                          additional_roots=(*roots, empty, *protected))
            finally:
                self.control.release(repo._project_id, actor, token)
        snapshot = self.control.begin_gc(repo._project_id, token)
        # No finally-unlock. Unknown S3 timeout, incomplete roots or process
        # failure retain the fence, preventing a paused deleter from racing a
        # subsequent publication. Recovery must prove both worker and remote
        # storage/index I/O quiescence; merely killing a worker is insufficient.
        empty, _ = encode_object("tree", b"", object_format=snapshot["object_format"])
        with collection_storage(repo._project_id, token):
            metadata, protected = self._capacity_inventory(repo, token, snapshot['object_format'])
            result = _run_git_object_gc(
                _PhysicalRepo(repo, metadata), **options, additional_roots=(*snapshot["roots"], empty, *protected),
            )
        if not result.errors and not result.sweep_skipped_for_safety:
            self.control.finish_gc(repo._project_id, token)
        return result
