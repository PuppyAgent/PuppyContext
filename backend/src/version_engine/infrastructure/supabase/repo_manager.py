"""Native Git service factories and internal storage maintenance ports."""

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from src.infra.s3.service import S3Service
from src.infra.supabase.client import SupabaseClient
from src.version_engine.infrastructure.supabase.audit_backend import SupabaseAuditManager
from src.version_engine.storage.backends.s3 import S3StorageBackend
from src.version_engine.storage.object_store import ObjectStore

if TYPE_CHECKING:
    pass


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


class VersionRepoManager:
    """Manages version repository instances for all projects."""

    def __init__(self, s3: S3Service, supabase: SupabaseClient):
        self._s3 = s3
        self._supabase = supabase

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

        control = AdmittedRefAuthorityRepository(
            self._supabase.client, lease_provider=lambda _: None
        )
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

        control = AdmittedRefAuthorityRepository(
            self._supabase.client, lease_provider=lambda _: None
        )
        return operation_status(control, project_id, grant, request_key)

    def get_native_service(self, project_id: str):
        """Select an explicitly native repository with mandatory checked admission.

        This neither enrolls repositories nor initializes policy/usage. Missing
        migration/RPC capability is an error, never permission to use old roots.
        The service and physical backend are request-owned, not cached authority.
        """
        metadata = self.repository_metadata(project_id)
        if metadata is None or metadata["authority"] == "shadow":
            return None
        return self.service_from_metadata(project_id, metadata)

    def service_from_metadata(self, project_id: str, metadata):
        """Construct from this request's admitted facts; construction does no I/O.

        This is not cached authority. Read pins and final SQL publication still
        validate current generation, permissions, lease, policy and CAS.
        """
        if metadata.get("project_id") != project_id or metadata.get("object_format") not in {"sha1", "sha256"}:
            raise ValueError("repository metadata binding mismatch")
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

        control = AdmittedRefAuthorityRepository(
            self._supabase.client,
            lease_provider=active_project_write_lease,
        )
        return RefTransactionService(
            control,
            S3StorageBackend(self._s3, project_id, supabase=self._supabase),
            project_id=project_id,
            object_format=metadata["object_format"],
            max_objects=100_000,
            max_bytes=256 * 1024**2,
            capacity=RepositoryCapacity(control),
            billing=RepositoryBilling(control),
            policy=RepositoryFilePolicy(control),
        )

    @contextmanager
    def open_native_read(self, project_id: str, grant):
        """Read identity, refs and pin in one admitted database operation.

        The captured snapshot is the authority for object format and generation;
        a separate service-selection snapshot would only repeat this read.
        """
        from src.version_engine.infrastructure.supabase.ref_authority_repository import (
            AdmittedRefAuthorityRepository,
        )
        from src.version_engine.read.repository_snapshot import repository_snapshot

        control = AdmittedRefAuthorityRepository(self._supabase.client, lease_provider=lambda _: None)
        backend = S3StorageBackend(self._s3, project_id, supabase=self._supabase)
        with repository_snapshot(control, backend, grant, project_id=project_id) as snapshot:
            yield snapshot

    def get_gc_repo(self, project_id: str):
        """Backend-only inventory for GC; not a bypass for current-tree access."""
        from src.version_engine.infrastructure.supabase.gc_repository import (
            ObjectGcInventory,
            ObjectGcRepository,
        )

        metadata = self.repository_metadata(project_id)
        backend = S3StorageBackend(self._s3, project_id, supabase=self._supabase)
        store = ObjectStore(
            objects_dir=_objects_dir_for(project_id),
            backend=backend,
            object_format=metadata["object_format"] if metadata is not None else "sha1",
        )
        return ObjectGcRepository(
            project_id,
            store,
            ObjectGcInventory(self._supabase.client, project_id),
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
