"""Provider registry dependency injection.

Provides:
  - ProviderRegistry (application-level singleton, built once at startup)

Adding a new connector:
  1. Create  provider/<provider>/adapter.py  with a BaseProvider subclass
  2. Add a  setup(deps) -> ProviderSetup  function in that file
  That's it — the registry auto-discovers everything at startup.
"""

from __future__ import annotations

import importlib
import pathlib
from typing import Optional

from src.provider._base import ProviderDeps, ProviderSetup
from src.provider.registry import ProviderRegistry
from src.provider.materializers import DEFAULT_MATERIALIZERS
from src.utils.logger import log_info, log_error


# ============================================================
# Auto-discovery: scan connector directories for setup()
# ============================================================

_SCAN_PATHS: list[tuple[str, str]] = [
    ("provider", "src.provider"),
]


def _discover_providers(deps: ProviderDeps) -> list[ProviderSetup]:
    """
    Scan connector directories for modules with a setup(deps) function.
    Each adapter.py that exports setup() is called to produce a ProviderSetup.
    """
    src_dir = pathlib.Path(__file__).resolve().parent.parent  # backend/src/
    setups: list[ProviderSetup] = []

    for rel_path, module_prefix in _SCAN_PATHS:
        scan_dir = src_dir / rel_path
        if not scan_dir.is_dir():
            continue

        for child in sorted(scan_dir.iterdir()):
            adapter_file = child / "adapter.py" if child.is_dir() else None

            if child.name == "adapter.py" and not child.is_dir():
                adapter_file = child

            if adapter_file is None or not adapter_file.exists():
                continue

            if child.is_dir():
                module_name = f"{module_prefix}.{child.name}.adapter"
            else:
                module_name = f"{module_prefix}.adapter"

            try:
                mod = importlib.import_module(module_name)
                setup_fn = getattr(mod, "setup", None)
                if setup_fn is None:
                    continue
                result = setup_fn(deps)
                setups.append(result)
                log_info(f"[Registry] Discovered connector: {result.adapter.spec().provider}")
            except Exception as e:
                log_error(f"[Registry] Failed to load {module_name}: {e}")

    return setups


# ============================================================
# Registry: application-level singleton
# ============================================================

_registry_instance: Optional[ProviderRegistry] = None


def _build_registry() -> ProviderRegistry:
    """Build a ProviderRegistry by auto-discovering connector modules."""
    from src.infra.s3.service import S3Service

    registry = ProviderRegistry()
    deps = ProviderDeps(s3_service=S3Service())

    for setup_result in _discover_providers(deps):
        try:
            registry.register(setup_result.adapter)
            for oauth_type, oauth_svc in setup_result.oauth_bindings.items():
                registry.register_oauth(oauth_type, oauth_svc)
        except Exception as e:
            provider = setup_result.adapter.spec().provider
            log_error(f"[Registry] Failed to register {provider}: {e}")

    for materializer in DEFAULT_MATERIALIZERS:
        if registry.get(materializer.provider):
            registry.register_materializer(materializer)

    return registry


def init_registry() -> ProviderRegistry:
    """
    Initialize the application-level ProviderRegistry singleton.
    Called once during app startup (app_lifespan).
    """
    global _registry_instance
    _registry_instance = _build_registry()
    log_info(f"[Registry] Initialized with {len(_registry_instance.providers())} connectors: {_registry_instance.providers()}")
    return _registry_instance


# ============================================================
# FastAPI dependency providers
# ============================================================

def get_provider_registry() -> ProviderRegistry:
    """Return the singleton registry. Falls back to building one if not initialized."""
    if _registry_instance is not None:
        return _registry_instance
    return init_registry()
