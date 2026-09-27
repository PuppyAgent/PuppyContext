"""Explicit Version Engine service container.

FastAPI installs one container on ``app.state`` at startup. Worker processes
can build their own container at bootstrap and pass it down explicitly. This
keeps app-scoped caches out of business modules while preserving the expensive
per-process repository cache where it belongs.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.infra.s3.service import S3Service
from src.infra.supabase.client import SupabaseClient
from src.version_engine.adapters.product.commands import VersionWriteCommandService
from src.version_engine.adapters.product.operation_adapter import ProductOperationAdapter
from src.version_engine.infrastructure.supabase.repo_manager import VersionRepoManager
from src.version_engine.infrastructure.supabase.version_ref_repository import VersionRefStore
from src.version_engine.read.admin import VersionAdminService
from src.version_engine.read.history_graph import HistoryGraphService
from src.version_engine.write_engine.engine import VersionWriteEngine


@dataclass
class VersionEngineContainer:
    """App/worker scoped Version Engine object graph."""

    repo_manager: VersionRepoManager
    version_ref_store: VersionRefStore
    history_graph_service: HistoryGraphService

    def admin_service(self) -> VersionAdminService:
        return VersionAdminService(self.repo_manager)

    def history_graph(self) -> HistoryGraphService:
        return self.history_graph_service

    def product_operations(self) -> ProductOperationAdapter:
        return ProductOperationAdapter(self.repo_manager)

    def write_commands(self) -> VersionWriteCommandService:
        return VersionWriteCommandService(self.product_operations())

    def write_engine(self) -> VersionWriteEngine:
        return VersionWriteEngine(self.repo_manager)


def build_version_engine_container(
    *,
    s3: S3Service | None = None,
    supabase: SupabaseClient | None = None,
    probe: bool = False,
) -> VersionEngineContainer:
    """Build a fresh container for one FastAPI app or worker process.

    ``probe=True`` runs a cheap connectivity check against Supabase + S3
    so misconfigured credentials surface as a startup error rather than
    crashing the first user request. Default is ``False`` because this
    function is also used for cheap per-request rebuilds (testing hook,
    connector helpers); the **process boot** entry point in ``main.py``
    and the worker bootstrap helper opt into probing explicitly.
    """

    s3_svc = s3 or S3Service()
    supa = supabase or SupabaseClient()
    if probe:
        _probe_dependencies(s3_svc, supa)
    repo_manager = VersionRepoManager(s3_svc, supa)
    version_ref_store = VersionRefStore(client=supa)
    return VersionEngineContainer(
        repo_manager=repo_manager,
        version_ref_store=version_ref_store,
        history_graph_service=HistoryGraphService(repo_manager, version_ref_store),
    )


def _probe_dependencies(s3_svc: S3Service, supa: SupabaseClient) -> None:
    """Fail loud at startup if S3 / Supabase aren't reachable.

    Cheap surface-level probes: a head_bucket-style call on S3 and a
    trivial select on Supabase. Detailed schema checks belong in
    migrations, not here. A probe failure raises ``RuntimeError`` with
    a clear hint so deploy logs say what's wrong rather than the first
    write erroring at request time.
    """
    from src.utils.logger import log_error, log_info

    try:
        head_bucket = getattr(s3_svc, "head_bucket", None)
        if callable(head_bucket):
            head_bucket()
        else:
            s3_svc.client.head_bucket(Bucket=s3_svc.bucket_name)
    except Exception as exc:
        log_error(f"[version_engine][bootstrap] S3 probe failed: {exc}")
        raise RuntimeError(
            f"Version Engine bootstrap failed: cannot reach S3 backend "
            f"({type(exc).__name__}: {exc})",
        ) from exc

    try:
        # A minimal query that exercises auth + network without scanning
        # a real table. ``db_names`` is loaded at import time so this
        # also validates the deferred name mapping is in place.
        supa.client.table("projects").select("id").limit(1).execute()
    except Exception as exc:
        log_error(f"[version_engine][bootstrap] Supabase probe failed: {exc}")
        raise RuntimeError(
            f"Version Engine bootstrap failed: cannot reach Supabase "
            f"({type(exc).__name__}: {exc})",
        ) from exc

    log_info("[version_engine][bootstrap] S3 + Supabase probes OK")
