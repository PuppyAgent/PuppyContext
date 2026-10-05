from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from src.config import settings
from src.exceptions import AppException, ErrorCode
from src.infra.supabase.client import SupabaseClient
from src.platform.entitlements.service import EntitlementService
from src.platform.project.repository import ProjectRepositorySupabase
from src.utils.logger import log_warning
from src.version_engine.write_engine.tree import tree_to_flat

if TYPE_CHECKING:
    from src.version_engine.storage.publication import ClosureManifest

STORAGE_METRIC = "storage.logical_bytes"
logger = logging.getLogger(__name__)


class StorageUsage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    org_id: str
    metric: str = STORAGE_METRIC
    value: int = 0
    version: int = 0
    threshold_percent: int = 0
    full_reconciled_at: datetime | None = None


class StorageUsageRepository:
    TABLE = "organization_usage_counters"

    def __init__(self, supabase_client: SupabaseClient | None = None) -> None:
        self._client = (supabase_client or SupabaseClient()).get_client()

    def get(self, org_id: str) -> StorageUsage:
        response = (
            self._client.table(self.TABLE)
            .select("*")
            .eq("org_id", org_id)
            .eq("metric", STORAGE_METRIC)
            .limit(1)
            .execute()
        )
        rows = response.data or []
        return StorageUsage.model_validate(rows[0]) if rows else StorageUsage(org_id=org_id)

    def reconcile(
        self,
        *,
        org_id: str,
        value: int,
        limit: int | None,
        idempotency_key: str,
        source: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        response = self._client.rpc(
            "reconcile_organization_usage_counter",
            {
                "p_org_id": org_id,
                "p_metric": STORAGE_METRIC,
                "p_value": value,
                "p_limit": limit,
                "p_idempotency_key": idempotency_key,
                "p_source": source,
                "p_metadata": metadata,
            },
        ).execute()
        data = response.data
        if isinstance(data, list) and len(data) == 1:
            data = data[0]
        if not isinstance(data, dict):
            raise RuntimeError("storage usage reconciliation returned an invalid response")
        return data

    def claim_reconciliation_batch(
        self,
        *,
        limit: int,
        min_age_seconds: int,
    ) -> list[str]:
        response = self._client.rpc(
            "claim_storage_reconciliation_batch",
            {
                "p_limit": limit,
                "p_min_age_seconds": min_age_seconds,
            },
        ).execute()
        rows = response.data or []
        if isinstance(rows, dict):
            rows = [rows]
        return [str(row["org_id"]) for row in rows if isinstance(row, dict) and row.get("org_id")]


@dataclass(frozen=True)
class StorageMeasurement:
    org_id: str
    project_id: str
    old_bytes: int | None
    new_bytes: int | None
    delta_bytes: int
    old_org_bytes: int
    new_org_bytes: int
    limit_bytes: int | None
    entitlement_source_revision: int | None = None
    shadow_would_deny: bool = False
    file_limit_bytes: int | None = None
    oversized_new_file_path: str | None = None
    oversized_new_file_bytes: int | None = None
    shadow_would_deny_file: bool = False


def logical_tree_bytes(store: Any, root_hash: str) -> int:
    if not root_hash:
        return 0
    manifest = tree_to_flat(store, root_hash, include_gitlinks=False)
    # Gitlinks name commits in external repositories, not locally stored files.
    # Logical active size counts each path, even when content-addressed blobs
    # deduplicate physically. This is predictable to customers and providers.
    return sum(len(store.get(oid)) for oid in manifest.values())


def logical_verified_tree_bytes(manifest: ClosureManifest, tree_oid: str) -> int:
    """Measure a verified native tree without expanding paths or loading blobs.

    Memoize subtree sizes, not visited paths: two edges to the same subtree
    still count twice. Only current-tree edges participate, never commit history
    or external gitlinks. The manifest must come from physical closure proof.
    """
    records = manifest.objects
    if tree_oid not in records or records[tree_oid].kind != 'tree':
        raise ValueError('logical billing requires a verified tree')
    sizes: dict[str, int] = {}
    active: set[str] = set()
    stack = [(tree_oid, False)]
    while stack:
        oid, exiting = stack.pop()
        if oid in sizes:
            continue
        record = records.get(oid)
        if record is None or record.kind not in {'tree', 'blob'}:
            raise ValueError('logical billing requires a complete typed tree closure')
        if record.kind == 'blob':
            if type(record.size) is not int or record.size < 0:
                raise ValueError('invalid logical blob size')
            value = record.size
        elif exiting:
            value = sum(sizes[child] for child, _kind in record.edges)
            active.remove(oid)
        else:
            if oid in active:
                raise ValueError('logical tree cycle')
            active.add(oid)
            stack.append((oid, True))
            for child, kind in record.edges:
                if kind not in {'tree', 'blob'} or child not in records or records[child].kind != kind:
                    raise ValueError('logical billing requires a complete typed tree closure')
                stack.append((child, False))
            continue
        if value > 2**63 - 1:
            raise OverflowError('logical tree bytes exceed the usage counter range')
        sizes[oid] = value
    return sizes[tree_oid]


def logical_tree_delta(store: Any, old_root_hash: str, new_root_hash: str) -> int:
    """Measure only changed logical paths instead of rereading every blob."""

    if old_root_hash == new_root_hash:
        return 0
    old_manifest = tree_to_flat(store, old_root_hash, include_gitlinks=False) if old_root_hash else {}
    new_manifest = tree_to_flat(store, new_root_hash, include_gitlinks=False) if new_root_hash else {}
    sizes: dict[str, int] = {}

    def size(oid: str) -> int:
        cached = sizes.get(oid)
        if cached is None:
            cached = len(store.get(oid))
            sizes[oid] = cached
        return cached

    delta = 0
    for path in old_manifest.keys() | new_manifest.keys():
        old_oid = old_manifest.get(path)
        new_oid = new_manifest.get(path)
        if old_oid == new_oid:
            continue
        if old_oid is not None:
            delta -= size(old_oid)
        if new_oid is not None:
            delta += size(new_oid)
    return delta


def oversized_new_logical_file(
    store: Any,
    old_root_hash: str,
    new_root_hash: str,
    limit_bytes: int,
) -> tuple[str, int] | None:
    """Find a newly introduced logical file above the plan limit.

    OID occurrence counts make a pure rename of a grandfathered oversized
    file legal, while copying that same blob to an additional path is treated
    as a new logical file and remains subject to the current plan.
    """

    old_manifest = tree_to_flat(store, old_root_hash, include_gitlinks=False) if old_root_hash else {}
    new_manifest = tree_to_flat(store, new_root_hash, include_gitlinks=False) if new_root_hash else {}
    excess = Counter(new_manifest.values()) - Counter(old_manifest.values())
    sizes: dict[str, int] = {}
    for path, oid in sorted(new_manifest.items()):
        if old_manifest.get(path) == oid:
            continue
        if excess[oid] <= 0:
            continue
        excess[oid] -= 1
        size = sizes.get(oid)
        if size is None:
            size = len(store.get(oid))
            sizes[oid] = size
        if size > limit_bytes:
            return path, size
    return None


class StorageQuotaService:
    def __init__(
        self,
        *,
        entitlement_service: EntitlementService | None = None,
        usage_repository: StorageUsageRepository | None = None,
    ) -> None:
        self._entitlements = entitlement_service or EntitlementService()
        self._usage_repository = usage_repository

    @property
    def _usage(self) -> StorageUsageRepository:
        if self._usage_repository is None:
            self._usage_repository = StorageUsageRepository()
        return self._usage_repository

    async def check_publish(
        self,
        *,
        project_id: str,
        repo_manager: Any,
        store: Any,
        old_root_hash: str,
        new_root_hash: str,
    ) -> StorageMeasurement | None:
        if settings.STORAGE_ENFORCEMENT_MODE == "disabled":
            return None
        project = await asyncio.to_thread(ProjectRepositorySupabase().get_by_id, project_id)
        if project is None:
            if settings.STORAGE_ENFORCEMENT_MODE == "required":
                raise AppException(
                    code=ErrorCode.INTERNAL_SERVER_ERROR,
                    status_code=503,
                    message="Storage quota cannot resolve the project organization",
                    details={"code": "storage_billing_context_missing"},
                )
            log_warning(f"[storage-quota] project context missing project={project_id}")
            return None
        delta_bytes = await asyncio.to_thread(
            logical_tree_delta,
            store,
            old_root_hash,
            new_root_hash,
        )
        usage = await asyncio.to_thread(self._usage.get, project.org_id)
        if usage.version > 0:
            old_org_bytes = usage.value
            old_bytes = None
            new_bytes = None
        else:
            old_bytes = await asyncio.to_thread(
                logical_tree_bytes,
                store,
                old_root_hash,
            )
            projects = await asyncio.to_thread(
                ProjectRepositorySupabase().get_by_org_id,
                project.org_id,
            )

            def baseline() -> int:
                total = 0
                for candidate in projects:
                    if candidate.id == project_id:
                        total += old_bytes
                        continue
                    candidate_repo = repo_manager.get_server_repo(candidate.id)
                    total += logical_tree_bytes(
                        candidate_repo.store,
                        candidate_repo.get_root_hash(),
                    )
                return total

            old_org_bytes = await asyncio.to_thread(baseline)
            new_bytes = max(0, old_bytes + delta_bytes)
        new_org_bytes = max(0, old_org_bytes + delta_bytes)
        entitlement_source_revision = None
        try:
            snapshot = self._entitlements.get_snapshot(project.org_id)
            raw_limits = snapshot.entitlements["limits"]
            raw_limit = raw_limits["storage.max_bytes"]
            raw_file_limit = raw_limits["upload.max_single_file_bytes"]
            entitlement_source_revision = int(snapshot.source_revision)
        except AppException as exc:
            if settings.STORAGE_ENFORCEMENT_MODE == "required":
                raise
            details = exc.details if isinstance(exc.details, dict) else {}
            logger.warning(
                "storage_shadow_snapshot_unavailable",
                extra={
                    "org_id": project.org_id,
                    "reason": details.get("code", "entitlement_snapshot_unavailable"),
                },
            )
            raw_limit = None
            raw_file_limit = None
        limit = int(raw_limit) if raw_limit is not None else None
        file_limit = int(raw_file_limit) if raw_file_limit is not None else None
        oversized_file = None
        if file_limit is not None:
            oversized_file = await asyncio.to_thread(
                oversized_new_logical_file,
                store,
                old_root_hash,
                new_root_hash,
                file_limit,
            )
        would_deny = limit is not None and delta_bytes > 0 and new_org_bytes > limit
        would_deny_file = oversized_file is not None
        measurement = StorageMeasurement(
            org_id=project.org_id,
            project_id=project_id,
            old_bytes=old_bytes,
            new_bytes=new_bytes,
            delta_bytes=delta_bytes,
            old_org_bytes=old_org_bytes,
            new_org_bytes=new_org_bytes,
            limit_bytes=limit,
            entitlement_source_revision=entitlement_source_revision,
            shadow_would_deny=would_deny,
            file_limit_bytes=file_limit,
            oversized_new_file_path=oversized_file[0] if oversized_file else None,
            oversized_new_file_bytes=oversized_file[1] if oversized_file else None,
            shadow_would_deny_file=would_deny_file,
        )
        if would_deny_file and settings.STORAGE_ENFORCEMENT_MODE == "required":
            raise AppException(
                code=ErrorCode.FORBIDDEN,
                status_code=413,
                message="File exceeds the organization plan limit",
                details={
                    "code": "file_size_limit_exceeded",
                    "org_id": project.org_id,
                    "path": measurement.oversized_new_file_path,
                    "file_bytes": measurement.oversized_new_file_bytes,
                    "limit_bytes": file_limit,
                    "retryable": False,
                },
            )
        if would_deny_file:
            logger.warning(
                "file_limit_shadow_would_deny",
                extra={
                    "org_id": project.org_id,
                    "project_id": project_id,
                    "path_hash": hashlib.sha256(
                        (measurement.oversized_new_file_path or "").encode("utf-8")
                    ).hexdigest()[:16],
                    "file_bytes": measurement.oversized_new_file_bytes,
                    "limit_bytes": file_limit,
                },
            )
        return measurement


class StorageReconciliationService:
    """Schedule checked reconciliation; retain explicit legacy test compatibility."""

    def __init__(
        self,
        *,
        repo_manager: Any,
        entitlement_service: EntitlementService | None = None,
        usage_repository: StorageUsageRepository | None = None,
        project_repository: ProjectRepositorySupabase | None = None,
        checked_reconciler: Any = None,
    ) -> None:
        self._checked = checked_reconciler
        self._repo_manager = repo_manager
        self._entitlements = entitlement_service or EntitlementService()
        self._usage = usage_repository or StorageUsageRepository()
        self._projects = project_repository or ProjectRepositorySupabase()

    async def reconcile_once(self, *, limit: int, min_age_seconds: int) -> dict[str, int]:
        if self._checked is not None:
            for _ in range(10):
                if await asyncio.to_thread(self._checked.prune) < 200:
                    break
        org_ids = await asyncio.to_thread(
            self._usage.claim_reconciliation_batch,
            limit=limit,
            min_age_seconds=min_age_seconds,
        )
        summary = {"claimed": len(org_ids), "reconciled": 0, "failed": 0}
        for org_id in org_ids:
            try:
                if self._checked is not None:
                    await asyncio.to_thread(self._checked.reconcile, org_id)
                    summary["reconciled"] += 1
                    continue
                projects = await asyncio.to_thread(self._projects.get_by_org_id, org_id)

                def measure(project_rows=projects) -> tuple[int, list[tuple[str, str]]]:
                    total = 0
                    roots: list[tuple[str, str]] = []
                    for project in project_rows:
                        repo = self._repo_manager.get_server_repo(project.id)
                        root_hash = repo.get_root_hash()
                        total += logical_tree_bytes(repo.store, root_hash)
                        roots.append((project.id, root_hash))
                    return total, sorted(roots)

                total, roots = await asyncio.to_thread(measure)
                raw_limit = self._entitlements.limit_value(org_id, "storage.max_bytes")
                storage_limit = int(raw_limit) if raw_limit is not None else None
                fingerprint = hashlib.sha256(
                    json.dumps(
                        {
                            "org_id": org_id,
                            "total": total,
                            "roots": roots,
                            "limit": storage_limit,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()[:32]
                await asyncio.to_thread(
                    self._usage.reconcile,
                    org_id=org_id,
                    value=total,
                    limit=storage_limit,
                    idempotency_key=f"storage-full:{fingerprint}",
                    source="storage_reconciler",
                    metadata={
                        "schema_version": "1.0",
                        "project_count": len(projects),
                        "root_fingerprint": fingerprint,
                    },
                )
                summary["reconciled"] += 1
            except Exception:
                summary["failed"] += 1
                logger.exception("storage_reconciliation_failed", extra={"org_id": org_id})
        return summary


_storage_quota_service: StorageQuotaService | None = None


def get_storage_quota_service() -> StorageQuotaService:
    global _storage_quota_service
    if _storage_quota_service is None:
        _storage_quota_service = StorageQuotaService()
    return _storage_quota_service
