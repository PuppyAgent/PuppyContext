"""
ProviderRegistry — Central registry for all integration connectors.

Responsibilities:
  - Register connector instances by provider name
  - Look up connectors by provider
  - List all registered ConnectorSpecs (for GET /integrations/connectors API)
  - Manage OAuth service mapping for credential resolution
"""

from typing import Any, Optional

from src.provider._base import BaseProvider, ProviderSpec, Credentials
from src.provider.materializers import SourceMaterializer
from src.utils.logger import log_info, log_error


class ProviderRegistry:
    """
    Central registry for connector instances and OAuth services.

    SynchronizeEngine uses this to look up connectors and resolve credentials.
    The /integrations/connectors API uses list_specs() for frontend dynamic rendering.
    """

    def __init__(self) -> None:
        self._providers: dict[str, BaseProvider] = {}
        self._oauth_services: dict[str, Any] = {}
        self._materializers: dict[str, dict[str, dict[int, SourceMaterializer]]] = {}

    # ── Provider registration ───────────────────────────────

    def register(self, adapter: BaseProvider) -> None:
        provider = adapter.spec().provider
        self._providers[provider] = adapter
        log_info(f"[Registry] Registered connector: {provider}")

    def get(self, provider: str) -> Optional[BaseProvider]:
        return self._providers.get(provider)

    def list_specs(self) -> list[ProviderSpec]:
        return [c.spec() for c in self._providers.values()]

    def providers(self) -> list[str]:
        return list(self._providers.keys())

    # ── Materializer registration ─────────────────────────────

    def register_materializer(self, materializer: SourceMaterializer) -> None:
        by_schema = self._materializers.setdefault(materializer.provider, {})
        by_version = by_schema.setdefault(materializer.schema.id, {})
        if materializer.schema.version in by_version:
            raise ValueError(
                f"Duplicate materializer: {materializer.provider} "
                f"{materializer.schema.id}@{materializer.schema.version}"
            )
        by_version[materializer.schema.version] = materializer
        log_info(
            "[Registry] Registered materializer: "
            f"{materializer.provider}:{materializer.schema.id}@{materializer.schema.version}"
        )

    def get_materializer(self, provider: str) -> Optional[SourceMaterializer]:
        return self.latest_materializer(provider)

    def materializers_for_provider(self, provider: str) -> list[SourceMaterializer]:
        by_schema = self._materializers.get(provider) or {}
        result: list[SourceMaterializer] = []
        for by_version in by_schema.values():
            result.extend(by_version.values())
        return sorted(result, key=lambda item: (item.schema.id, item.schema.version))

    def latest_materializer(self, provider: str) -> Optional[SourceMaterializer]:
        materializers = self.materializers_for_provider(provider)
        if not materializers:
            return None
        return max(materializers, key=lambda item: item.schema.version)

    def latest_schema_version(self, provider: str, schema_id: str | None = None) -> int | None:
        materializers = self.materializers_for_provider(provider)
        if schema_id:
            materializers = [item for item in materializers if item.schema.id == schema_id]
        if not materializers:
            return None
        return max(item.schema.version for item in materializers)

    def resolve_materializer(
        self,
        provider: str,
        schema_ref: Optional[dict[str, Any]] = None,
    ) -> Optional[SourceMaterializer]:
        by_schema = self._materializers.get(provider) or {}
        if not by_schema:
            return None
        if not schema_ref:
            return self.latest_materializer(provider)

        schema_id = schema_ref.get("id")
        version = schema_ref.get("version")
        if not schema_id or version is None:
            raise ValueError(f"Invalid materialization schema ref for {provider}: {schema_ref!r}")
        try:
            version_int = int(version)
        except (TypeError, ValueError):
            raise ValueError(f"Invalid materialization schema version for {provider}: {version!r}") from None

        materializer = by_schema.get(str(schema_id), {}).get(version_int)
        if materializer is None:
            raise ValueError(f"Unknown materialization schema for {provider}: {schema_id}@{version_int}")
        return materializer

    def pin_materialization_schema(
        self,
        provider: str,
        config: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        pinned = dict(config or {})
        materializer = self.resolve_materializer(
            provider,
            pinned.get("materialization_schema"),
        )
        if materializer is not None:
            pinned["materialization_schema"] = materializer.schema.ref()
        return pinned

    # ── OAuth service registration ───────────────────────────

    def register_oauth(self, oauth_type: str, service: Any) -> None:
        """Register an OAuth service for credential resolution."""
        self._oauth_services[oauth_type] = service

    async def resolve_credentials(
        self,
        oauth_type: Optional[str],
        user_id: str,
        *,
        required: bool = True,
    ) -> Credentials:
        """
        Resolve credentials for a given oauth_type and user_id.

        Handles token refresh automatically. Returns empty Credentials
        if no OAuth is needed (e.g. URL connector). Optional OAuth callers
        can continue without a token, which lets public URL imports work while
        still using a connected account for private resources when available.
        """
        if not oauth_type or oauth_type not in self._oauth_services:
            return Credentials()

        if not user_id:
            if not required:
                return Credentials()
            raise ValueError(
                f"Cannot resolve {oauth_type} credentials: user_id is empty. "
                f"Please re-create this sync."
            )

        service = self._oauth_services[oauth_type]

        try:
            connection = await service.refresh_token_if_needed(user_id)
            if not connection:
                if not required:
                    return Credentials()
                raise ValueError(
                    f"No {oauth_type} connection found for user. Please authorize first."
                )

            return Credentials(
                access_token=connection.access_token,
                metadata=connection.metadata or {},
            )
        except Exception as e:
            log_error(f"[Registry] Failed to resolve credentials for {oauth_type}: {e}")
            if not required:
                return Credentials()
            raise

    # ── Serialization (for API response) ─────────────────────

    def specs_to_dicts(self, include_hidden: bool = False) -> list[dict]:
        """Serialize specs to dicts for API response. Filters ui_visible=False by default."""
        result = []
        for adapter in self._providers.values():
            s = adapter.spec()
            if not include_hidden and not s.ui_visible:
                continue
            latest_materializer = self.latest_materializer(s.provider)
            latest_version = (
                latest_materializer.schema.version if latest_materializer else None
            )
            result.append({
                "provider": s.provider,
                "display_name": s.display_name,
                "description": s.description,
                "auth": s.auth.value,
                "oauth_type": s.oauth_type,
                "oauth_ui_type": s.oauth_ui_type,
                "default_node_type": s.default_node_type,
                "supported_sync_modes": list(s.supported_sync_modes),
                "default_sync_mode": s.default_sync_mode,
                "creation_mode": s.creation_mode,
                "supported_directions": s.supported_directions,
                "accept_types": list(s.accept_types),
                "config_fields": [
                    {
                        "key": f.key,
                        "label": f.label,
                        "type": f.type,
                        "required": f.required,
                        "default": f.default,
                        "options": f.options,
                        "placeholder": f.placeholder,
                        "hint": f.hint,
                    }
                    for f in s.config_fields
                ],
                "icon": s.icon,
                "icon_url": s.icon_url,
                "materialization_schema": (
                    latest_materializer.schema.to_dict(
                        provider=s.provider,
                        latest=True,
                        latest_version=latest_version,
                    )
                    if latest_materializer
                    else None
                ),
                "materialization_schemas": [
                    materializer.schema.to_dict(
                        provider=s.provider,
                        latest=materializer.schema.version == latest_version,
                        latest_version=latest_version,
                    )
                    for materializer in self.materializers_for_provider(s.provider)
                ],
            })
        return result
