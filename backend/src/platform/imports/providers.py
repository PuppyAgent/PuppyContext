"""Import admission; source capability registration alone grants no lifecycle."""
from src.provider._base import Capability
from src.provider.dependencies import get_provider_registry
from src.provider.registry import ProviderRegistry

# Explicitly reviewed one-shot adapters. Notion's public snapshot uses URL fetch.
IMPORT_PROVIDERS = frozenset({
    "github", "url", "gmail", "google_docs", "google_sheets", "google_drive",
    "google_calendar", "google_search_console",
})
IMPORT_ALIASES = {"notion": "url"}


def import_adapter_name(provider: str) -> str:
    name = provider.strip().lower()
    name = IMPORT_ALIASES.get(name, name)
    if name not in IMPORT_PROVIDERS:
        raise ValueError(f"Provider {provider!r} is not approved for Import")
    return name


def require_import_provider(registry: ProviderRegistry, provider: str):
    name = import_adapter_name(provider)
    adapter = registry.get(name)
    if adapter is None or not adapter.spec().capabilities & Capability.PULL:
        raise ValueError(f"Provider {provider!r} does not support Import snapshots")
    return adapter


def import_specs(registry: ProviderRegistry) -> list[dict]:
    specs = []
    for spec in registry.specs_to_dicts():
        try:
            require_import_provider(registry, spec["provider"])
        except ValueError:
            continue
        specs.append(spec)
    by_provider = {spec["provider"]: spec for spec in specs}
    for alias, target in IMPORT_ALIASES.items():
        if target in by_provider:
            specs.append({**by_provider[target], "provider": alias,
                          "display_name": "Notion (public snapshot)"})
    return specs


def get_import_provider_registry() -> ProviderRegistry:
    registry = get_provider_registry()
    return registry.select(
        name for name in IMPORT_PROVIDERS
        if registry.get(name) is not None
        and registry.get(name).spec().capabilities & Capability.PULL
    )
