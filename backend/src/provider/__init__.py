"""
Data Source Providers — One provider per external system.

Each provider lives in its own directory and implements BaseProvider,
declaring its capabilities via ProviderSpec. Providers are auto-discovered
at startup — no manual registration required.

To add a new connector:
  1. Create  provider/<provider>/adapter.py  with a BaseProvider subclass
  2. Add a  setup(deps) -> ProviderSetup  function in that file
"""

from src.provider._base import (
    BaseProvider,
    ProviderSpec,
    Capability,
    AuthRequirement,
    TriggerMode,
    Credentials,
    FetchResult,
    ConfigField,
    ProviderDeps,
    ProviderSetup,
)

__all__ = [
    "BaseProvider",
    "ProviderSpec",
    "Capability",
    "AuthRequirement",
    "TriggerMode",
    "Credentials",
    "FetchResult",
    "ConfigField",
    "ProviderDeps",
    "ProviderSetup",
]
