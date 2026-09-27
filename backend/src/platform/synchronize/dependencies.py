"""Dependency providers for Synchronize."""

from __future__ import annotations

from typing import Optional

from fastapi import Depends

from src.platform.synchronize.providers import get_synchronize_provider_registry
from src.provider.registry import ProviderRegistry
from src.platform.synchronize.run_repository import SyncRunRepository
from src.infra.supabase.client import SupabaseClient
from src.platform.synchronize.arq_client import SyncArqClient
from src.platform.synchronize.engine import SynchronizeEngine
from src.platform.synchronize.repository import SynchronizeRepository
from src.platform.synchronize.service import SynchronizeService

_sync_arq_client: SyncArqClient | None = None


def _get_supabase_client() -> SupabaseClient:
    return SupabaseClient()


def get_synchronize_engine(
    registry: ProviderRegistry = Depends(get_synchronize_provider_registry),
    supabase: SupabaseClient = Depends(_get_supabase_client),
) -> SynchronizeEngine:
    return SynchronizeEngine(
        registry=registry,
        repository=SynchronizeRepository(supabase),
        run_repo=SyncRunRepository(supabase),
    )


def get_synchronize_service(
    registry: ProviderRegistry = Depends(get_synchronize_provider_registry),
    supabase: SupabaseClient = Depends(_get_supabase_client),
) -> SynchronizeService:
    return _build_synchronize_service(
        registry=registry,
        supabase=supabase,
    )


def get_sync_arq_client() -> SyncArqClient:
    global _sync_arq_client
    if _sync_arq_client is None:
        _sync_arq_client = SyncArqClient()
    return _sync_arq_client


def _build_synchronize_service(
    registry: Optional[ProviderRegistry] = None,
    supabase: Optional[SupabaseClient] = None,
) -> SynchronizeService:
    registry = registry or get_synchronize_provider_registry()
    svc = SynchronizeService(
        repository=SynchronizeRepository(supabase or SupabaseClient()),
    )
    for provider in registry.providers():
        adapter = registry.get(provider)
        if adapter:
            svc.register_provider(adapter)
    return svc


def create_synchronize_engine() -> SynchronizeEngine:
    registry = get_synchronize_provider_registry()
    supabase = SupabaseClient()
    return SynchronizeEngine(
        registry=registry,
        repository=SynchronizeRepository(supabase),
        run_repo=SyncRunRepository(supabase),
    )


__all__ = [
    "create_synchronize_engine",
    "get_synchronize_provider_registry",
    "get_synchronize_engine",
    "get_synchronize_service",
    "get_sync_arq_client",
]
