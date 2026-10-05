"""
VersionRepoManager — per-project version repository factory

Each PuppyOne project corresponds to one logical version repository, composed of:
  - ObjectStore (S3StorageBackend)  -> content storage
  - HistoryManager (Supabase)       -> version history
  - AuditLog (Supabase)             -> audit logs

Provides three repo views:
  - ProjectRepo       -> used by VersionAdminService (admin/history operations)
  - PuppyOneServerRepo -> used by the Write Engine
  - HostClientHandle  -> long-lived in-process version client per (project, scope),
                         bundled with a per-key lock that serialises pushes
                         against the same scope. Cached so the tree-index
                         walk only happens on first access; later pushes reuse
                         the cached path-to-blob index, mirroring how a
                         long-lived Git working copy keeps local object state
                         between commits.
"""

from __future__ import annotations

import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from src.version_engine.write_engine.merge import ConflictResolver
from src.version_engine.storage.object_store import ObjectStore
from src.infra.s3.service import S3Service
from src.infra.supabase.client import SupabaseClient
from src.platform.repository_target.models import RepositoryPathProjection
from src.version_engine.domain.intents import ProjectWriteState
from src.version_engine.infrastructure.supabase import safe_data
from src.version_engine.storage.backends.s3 import CachedStorageBackend, S3StorageBackend
from src.version_engine.infrastructure.supabase.audit_backend import SupabaseAuditManager
from src.version_engine.infrastructure.supabase.history_repository import SupabaseHistoryManager
from src.version_engine.infrastructure.supabase.scope_repository import SupabaseScopeBackend
from src.version_engine.infrastructure.supabase.db_names import PROJECT_WRITE_STATE_RPC
from src.version_engine.infrastructure.supabase.server_repo import PuppyOneServerRepo
from src.version_engine.infrastructure.supabase.scope_manager import ScopeManager
from src.version_engine.infrastructure.supabase.transaction_ledger import SupabaseVersionTransactionLedger
from src.utils.logger import log_error

if TYPE_CHECKING:
    from src.version_engine.adapters.batch.in_process_client import InProcessVersionClient


def _objects_dir_for(project_id: str) -> Path:
    """Resolve the per-project object cache directory.

    Honors ``VERSION_ENGINE_OBJECTS_DIR`` if set; otherwise uses the OS
    temp directory under ``version-engine/<project>``. ``/tmp`` is not
    portable to Windows runners, and tests/CI sometimes run with
    read-only ``/`` so we never write outside the temp tree.
    """
    base = os.environ.get("VERSION_ENGINE_OBJECTS_DIR")
    if base:
        return Path(base) / project_id
    return Path(tempfile.gettempdir()) / "version-engine" / project_id


@dataclass
class HostClientHandle:
    """A cached host-side version client + the lock used to serialise pushes.

    Lifetime: process-wide, per (project_id, scope_path). On first access
    the manager creates the client and runs ``load_scope_index`` once to
    populate its ``_file_hashes`` tree map; subsequent accesses return
    the same instance so callers can ``push`` without re-indexing. The
    lock serialises concurrent writes against the same scope (within
    one process) so two requests don't race on the snapshot rebuild.
    """

    client: "InProcessVersionClient"
    lock: threading.Lock


@dataclass
class ProjectRepo:
    """A single project's version repository instance."""
    project_id: str
    store: ObjectStore
    history: SupabaseHistoryManager
    audit: SupabaseAuditManager
    resolver: ConflictResolver


class VersionRepoManager:
    """Manages version repository instances for all projects."""

    def __init__(self, s3: S3Service, supabase: SupabaseClient):
        self._s3 = s3
        self._supabase = supabase
        self.transaction_ledger = SupabaseVersionTransactionLedger(supabase.client)
        self._cache: dict[str, ProjectRepo] = {}
        self._name_cache: dict[str, str] = {}
        self._lock = threading.Lock()
        # Host-side version clients keyed by (project_id, scope_path).
        # Created lazily on first push to that scope and reused for the
        # rest of the process lifetime. See HostClientHandle.
        self._host_clients: dict[tuple[str, str], HostClientHandle] = {}
        self._host_clients_lock = threading.Lock()

    def repository_metadata(self, project_id: str) -> dict | None:
        """Fresh authority selection; absence is explicit, not an RPC fallback."""
        from src.version_engine.infrastructure.supabase.ref_authority_repository import (
            RefAuthorityRepository,
        )

        metadata = RefAuthorityRepository(self._supabase.client).snapshot(project_id)
        if metadata is not None and (
            metadata.get("project_id") != project_id
            or metadata.get("authority") not in {"shadow", "native"}
            or metadata.get("object_format") not in {"sha1", "sha256"}
        ):
            raise RuntimeError("invalid repository authority metadata")
        return metadata

    def get_native_ref_metadata(self, project_id: str, grant):
        """Current-reader refs/profile discovery; no backend, pin or lease."""
        from src.version_engine.infrastructure.supabase.ref_authority_repository import (
            AdmittedRefAuthorityRepository,
        )
        from src.version_engine.read.ref_metadata import repository_ref_metadata

        control = AdmittedRefAuthorityRepository(self._supabase.client, lease_provider=lambda _: None)
        return repository_ref_metadata(control, project_id, grant)

    def get_native_operation_status(self, project_id: str, grant, request_key: str):
        """Historical native result discovery, independent of current write policy.

        Only the admitted SQL lookup is used: no cached facade, S3 backend,
        repository enrollment, snapshot/read pin or legacy result fallback.
        """
        from src.version_engine.infrastructure.supabase.ref_authority_repository import (
            AdmittedRefAuthorityRepository,
        )
        from src.version_engine.read.operation_status import operation_status

        control = AdmittedRefAuthorityRepository(self._supabase.client, lease_provider=lambda _: None)
        return operation_status(control, project_id, grant, request_key)

    def get_native_service(self, project_id: str):
        """Select an explicitly native repository with mandatory checked admission.

        This neither enrolls repositories nor initializes policy/usage. Missing
        migration/RPC capability is an error, never permission to use old roots.
        The service and physical backend are request-owned, not cached authority.
        """
        from src.platform.project.write_lease import active_project_write_lease
        from src.version_engine.infrastructure.supabase.billing_repository import RepositoryBilling
        from src.version_engine.infrastructure.supabase.capacity_repository import (
            RepositoryCapacity,
        )
        from src.version_engine.infrastructure.supabase.file_policy_repository import (
            RepositoryFilePolicy,
        )
        from src.version_engine.infrastructure.supabase.ref_authority_repository import (
            AdmittedRefAuthorityRepository,
        )
        from src.version_engine.write_engine.ref_transaction import RefTransactionService

        metadata = self.repository_metadata(project_id)
        if metadata is None or metadata["authority"] == "shadow":
            return None
        control = AdmittedRefAuthorityRepository(
            self._supabase.client, lease_provider=active_project_write_lease,
        )
        return RefTransactionService(
            control, S3StorageBackend(self._s3, project_id, supabase=self._supabase),
            project_id=project_id, object_format=metadata["object_format"],
            capacity=RepositoryCapacity(control), billing=RepositoryBilling(control),
            policy=RepositoryFilePolicy(control),
        )

    def get_repo(self, project_id: str) -> ProjectRepo:
        metadata = self.repository_metadata(project_id)
        if metadata is not None and metadata["authority"] == "native":
            raise RuntimeError("native repository requires authority-aware access")
        if project_id in self._cache:
            return self._cache[project_id]
        with self._lock:
            if project_id not in self._cache:
                self._cache[project_id] = self._create_repo(project_id)
            return self._cache[project_id]

    def get_server_repo(
        self,
        project_id: str,
        *,
        project_name: str | None = None,
    ) -> PuppyOneServerRepo:
        """Create a PuppyOneServerRepo for Write Engine handlers.

        Returns a NEW instance every time — PuppyOneServerRepo holds per-request
        mutable state (_pending_scope, _last_scope_build) that must not be shared
        across concurrent pushes. The expensive underlying components (store,
        history, audit) are shared via the cached ProjectRepo.
        """
        proj = self.get_repo(project_id)
        project_name = project_name or self._get_project_name(project_id)
        scope_backend = SupabaseScopeBackend(self._supabase, project_id)
        return PuppyOneServerRepo(
            project_id=project_id,
            project_name=project_name,
            store=proj.store,
            history=proj.history,
            audit=proj.audit,
            scopes=ScopeManager(scope_backend),
        )

    def get_gc_repo(self, project_id: str):
        """Backend-only inventory for GC; not a bypass for current-tree access."""
        from src.version_engine.infrastructure.supabase.gc_repository import (
            ObjectGcInventory,
            ObjectGcRepository,
        )

        metadata = self.repository_metadata(project_id)
        backend = S3StorageBackend(self._s3, project_id, supabase=self._supabase)
        store = ObjectStore(
            objects_dir=_objects_dir_for(project_id), backend=backend,
            object_format=metadata["object_format"] if metadata is not None else "sha1",
        )
        return ObjectGcRepository(
            project_id, store, ObjectGcInventory(SupabaseHistoryManager(self._supabase, project_id)),
        )

    def get_audit(self, project_id: str) -> SupabaseAuditManager:
        """Audit is independent of legacy root/history selection."""
        return SupabaseAuditManager(self._supabase, project_id)

    def create_usage_reconciler(self):
        """Backend scheduler capability; never exposed to Runtime credentials."""
        from src.version_engine.infrastructure.supabase.ref_authority_repository import (
            RefAuthorityRepository,
        )
        from src.version_engine.infrastructure.supabase.usage_reconciliation import (
            RepositoryUsageReconciler,
        )

        return RepositoryUsageReconciler(
            RefAuthorityRepository(self._supabase.client),
            lambda project_id: S3StorageBackend(self._s3, project_id, supabase=self._supabase),
        )

    def get_scope_backend(self, project_id: str) -> SupabaseScopeBackend:
        """Return a lightweight ScopeBackend for listing a project's scopes.

        Used by the admission layer to compute carved_excludes — the set of
        child-scope paths that must be hidden from a parent scope's view.
        """
        return SupabaseScopeBackend(self._supabase, project_id)

    def get_project_write_state(
        self,
        project_id: str,
        user_id: str,
    ) -> ProjectWriteState | None:
        """Load authorization + root/head state for a product write.

        This is the request-path read boundary for Product/Web writes. It
        intentionally requires the final SQL RPC instead of falling back to
        scattered table reads; otherwise a code deploy without the migration
        silently regresses Save latency and correctness observability.
        """

        try:
            resp = self._supabase.client.rpc(
                PROJECT_WRITE_STATE_RPC,
                {
                    "p_project_id": project_id,
                    "p_user_id": user_id,
                },
            ).execute()
        except Exception as exc:
            raise RuntimeError(
                f"{PROJECT_WRITE_STATE_RPC} RPC is required for product "
                "writes. Apply migration "
                "20260518001000_write_state_and_object_locations.sql first."
            ) from exc

        data = safe_data(resp)
        if not data:
            return None
        row = data[0] if isinstance(data, list) else data
        return ProjectWriteState(
            project_id=str(row.get("project_id") or project_id),
            project_name=str(row.get("project_name") or "project"),
            org_id=str(row.get("org_id") or ""),
            visibility=str(row.get("visibility") or "org"),
            role=str(row.get("role") or ""),
            can_write=bool(row.get("can_write")),
            root_hash=str(row.get("root_hash") or ""),
            head_commit_id=str(row.get("head_commit_id") or ""),
        )

    def get_host_client(
        self, project_id: str, scope_path: str, who: str = "puppyone-host",
    ) -> HostClientHandle:
        """Return the cached host version client for ``(project_id, scope_path)``.

        First access for a given key: create a fresh ``InProcessVersionClient``,
        run ``load_scope_index`` to seed its ``_file_hashes`` from the scope's
        current tree (one-time cost — bounded by the number of tree
        nodes in the scope, blob bytes never touched), cache it with a
        dedicated lock, and return it.

        Subsequent accesses return the same handle so the caller can push
        modifications without re-indexing. The lock should be held while
        mutating the client (push) so concurrent requests against the
        same scope serialise cleanly within this process.

        Concurrency: uses double-checked locking so the slow
        ``load_scope_index`` runs OUTSIDE ``_host_clients_lock``. Holding a
        single global lock during a multi-second index walk would freeze
        every other scope's first-time access AND every pre-existing
        cache lookup, plus block uvicorn's graceful shutdown — that
        was the previous behaviour and it is what wedged the server.
        Trade-off: under a rare race two concurrent first-accesses for
        the *same* scope may both build an index; the second's work is then
        discarded. That's wasted CPU, not a deadlock.

        ``who`` is used only for the FIRST index refresh's audit row. Per-push
        audit identity is set separately via
        ``client._set_audit_agent(who)`` inside the lock.
        """
        from src.version_engine.adapters.batch.in_process_client import InProcessVersionClient

        key = (project_id, scope_path or "")

        # Fast path: cache hit, return immediately under the lock.
        with self._host_clients_lock:
            cached = self._host_clients.get(key)
            if cached is not None:
                return cached

        # Slow path: build + index OUTSIDE the lock so other scopes can
        # progress concurrently and a stuck index walk can never freeze the
        # whole process.
        client = InProcessVersionClient(
            self,
            project_id,
            RepositoryPathProjection(path_prefix=scope_path or ""),
            actor=who,
        )
        client.load_scope_index()
        new_handle = HostClientHandle(client=client, lock=threading.Lock())

        # Re-check under the lock: someone else may have populated the
        # cache while we were cloning. If so, drop our work and return
        # theirs — both clients would have produced equivalent state
        # anyway, but using one keeps the lock semantics correct.
        with self._host_clients_lock:
            existing = self._host_clients.get(key)
            if existing is not None:
                return existing
            self._host_clients[key] = new_handle
            return new_handle

    def invalidate_host_client(self, project_id: str, scope_path: str) -> None:
        """Drop the cached host client for ``(project_id, scope_path)``.

        Call after detecting the cached state is no longer in sync with
        the server (e.g. an external CLI client pushed). Next
        ``get_host_client`` call will rebuild a fresh index.
        """
        key = (project_id, scope_path or "")
        with self._host_clients_lock:
            self._host_clients.pop(key, None)

    def _create_repo(self, project_id: str) -> ProjectRepo:
        s3_backend = S3StorageBackend(self._s3, project_id, supabase=self._supabase)
        cached_backend = CachedStorageBackend(s3_backend)
        store = ObjectStore(
            objects_dir=_objects_dir_for(project_id),
            backend=cached_backend,
        )
        history = SupabaseHistoryManager(self._supabase, project_id)
        audit = SupabaseAuditManager(self._supabase, project_id)
        resolver = ConflictResolver()

        return ProjectRepo(
            project_id=project_id,
            store=store,
            history=history,
            audit=audit,
            resolver=resolver,
        )

    def _get_project_name(self, project_id: str) -> str:
        """Get project name, cached to avoid per-request DB queries."""
        if project_id in self._name_cache:
            return self._name_cache[project_id]
        name = self._lookup_project_name(project_id)
        self._name_cache[project_id] = name
        return name

    def _lookup_project_name(self, project_id: str) -> str:
        try:
            resp = (
                self._supabase.client.table("projects")
                .select("name")
                .eq("id", project_id)
                .maybe_single()
                .execute()
            )
            data = safe_data(resp)
            if data:
                return data.get("name", "project")
            return "project"
        except Exception as e:
            log_error(f"[RepoManager] Failed to lookup project name: {e}")
            return "project"
