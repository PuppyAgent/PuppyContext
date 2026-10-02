"""Synchronize owns modes/admission; Provider only supplies source operations."""
from src.provider._base import Capability
from src.provider.dependencies import get_provider_registry
from src.provider.registry import ProviderRegistry
from src.platform.synchronize.config_contract import validate_structured_config

# GitHub's snapshot adapter is deliberately absent: its durable binding uses
# synchronize.github, not the generic fetch/materialize pipeline.
SYNCHRONIZE_MODES = {
    provider: ("manual", "scheduled") for provider in (
        "url", "gmail", "google_docs", "google_sheets", "google_drive",
        "google_calendar", "google_search_console",
    )
}


def require_synchronize_provider(adapter, *, mode="manual", direction="inbound",
                                 trigger=None, config=None):
    if adapter is None:
        raise ValueError("Provider is unavailable for Synchronize")
    spec = adapter.spec()
    modes = SYNCHRONIZE_MODES.get(spec.provider, ())
    if mode not in modes:
        raise ValueError(f"Provider {spec.provider!r} does not support Synchronize mode {mode!r}")
    trigger = trigger or {}
    if trigger.get("type") and trigger["type"] != mode:
        raise ValueError("Synchronize trigger.type must match sync_mode")
    if direction not in spec.supported_directions:
        raise ValueError(f"Unsupported Synchronize direction: {direction!r}")
    required = {
        "inbound": Capability.PULL,
        "outbound": Capability.PUSH,
        "bidirectional": Capability.PULL | Capability.PUSH,
    }.get(direction)
    if required is None or spec.capabilities & required != required:
        raise ValueError(f"Provider {spec.provider!r} lacks required Synchronize capabilities")
    if config is not None:
        validate_structured_config(spec.provider, spec, config, allow_system_keys=True)
    return adapter


def get_synchronize_provider_registry() -> ProviderRegistry:
    registry = get_provider_registry()
    return registry.select(
        name for name in SYNCHRONIZE_MODES
        if registry.get(name) is not None
        and registry.get(name).spec().capabilities & Capability.PULL
    )


def synchronize_specs(registry: ProviderRegistry) -> list[dict]:
    specs = []
    for spec in registry.specs_to_dicts():
        adapter = registry.get(spec["provider"])
        try:
            require_synchronize_provider(adapter)
        except ValueError:
            continue
        modes = SYNCHRONIZE_MODES[spec["provider"]]
        specs.append({**spec, "supported_sync_modes": list(modes),
                      "default_sync_mode": "scheduled" if spec["provider"] == "google_search_console" else "manual",
                      "category": "datasource"})
    return specs
