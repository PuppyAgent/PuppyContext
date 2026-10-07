"""Version Engine FastAPI dependency providers and worker bootstrap helpers."""

from __future__ import annotations

from fastapi import Depends, Request

from src.infra.s3.dependencies import get_s3_service
from src.infra.s3.service import S3Service
from src.infra.supabase.client import SupabaseClient
from src.version_engine.adapters.product.commands import VersionWriteCommandService
from src.version_engine.adapters.product.operation_adapter import ProductOperationAdapter
from src.version_engine.bootstrap.container import (
    VersionEngineContainer,
    build_version_engine_container,
)
from src.version_engine.infrastructure.supabase.repo_manager import VersionRepoManager
from src.version_engine.read.admin import VersionAdminService
from src.version_engine.read.history_graph import HistoryGraphService
from src.version_engine.write_engine.engine import VersionWriteEngine


def _get_supabase_client() -> SupabaseClient:
    return SupabaseClient()


def get_version_engine_container(request: Request) -> VersionEngineContainer:
    """Return the app-scoped container installed by FastAPI lifespan."""

    container = getattr(request.app.state, "version_engine", None)
    if container is None:
        container = build_version_engine_container()
        request.app.state.version_engine = container
    return container


def get_repo_manager(
    container: VersionEngineContainer = Depends(get_version_engine_container),
) -> VersionRepoManager:
    return container.repo_manager


def get_version_admin_service(
    container: VersionEngineContainer = Depends(get_version_engine_container),
) -> VersionAdminService:
    return container.admin_service()


def get_history_graph_service(
    container: VersionEngineContainer = Depends(get_version_engine_container),
) -> HistoryGraphService:
    return container.history_graph()


def get_product_operation_adapter(
    container: VersionEngineContainer = Depends(get_version_engine_container),
) -> ProductOperationAdapter:
    return container.product_operations()


def get_version_write_command_service(
    container: VersionEngineContainer = Depends(get_version_engine_container),
) -> VersionWriteCommandService:
    return container.write_commands()


def get_version_write_engine(
    container: VersionEngineContainer = Depends(get_version_engine_container),
) -> VersionWriteEngine:
    """Native initialization facade; content writes use Product commands."""

    return container.write_engine()


def build_worker_version_engine_container(
    *,
    probe: bool = False,
) -> VersionEngineContainer:
    """Explicit bootstrap for scheduler jobs, ARQ workers, and CLI scripts.

    Long-lived worker processes should pass ``probe=True`` at boot so a
    misconfigured deploy surfaces immediately rather than on the first
    queued task. Short-lived per-call rebuilds keep the default
    (``False``) — they reuse the same long-lived Supabase/S3 services
    so a fresh probe per call would add latency without adding signal.
    """

    return build_version_engine_container(probe=probe)


def build_request_version_engine_container(
    s3: S3Service = Depends(get_s3_service),
    supabase: SupabaseClient = Depends(_get_supabase_client),
) -> VersionEngineContainer:
    """Testing hook for constructing a request-scoped container if needed."""

    return build_version_engine_container(s3=s3, supabase=supabase)
