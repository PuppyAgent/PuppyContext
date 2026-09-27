"""Provider registration for synchronize; adapters and OAuth instances are shared.

The entrypoint owns job/binding lifecycle. Provider owns source capabilities.
This explicit seam lets each entrypoint select capabilities independently
without a second catalog or duplicate provider setup.
"""
from src.provider.dependencies import get_provider_registry
from src.provider.registry import ProviderRegistry


def get_synchronize_provider_registry() -> ProviderRegistry:
    return get_provider_registry()
