"""Integration lifecycle service.

The service owns product semantics: durable connection lifecycle, target path,
trigger configuration, and initial execution handoff. Provider connectors stay
storage-agnostic.
"""

from __future__ import annotations

from typing import Any, Optional
from copy import deepcopy

from src.provider._base import AuthRequirement, BaseProvider
from src.provider.schemas import ResourceInfo, SourceInput
from src.platform.synchronize.providers import get_synchronize_provider_registry, require_synchronize_provider
from src.platform.synchronize.paths import (
    canonical_provider,
    join_path,
    normalize_path,
    safe_filename,
)
from src.platform.synchronize.models import SynchronizeBinding
from src.platform.synchronize.repository import SynchronizeRepository
from src.utils.logger import log_info


class SynchronizeService:
    def __init__(self, repository: SynchronizeRepository, registry=None):
        self.repository = repository
        self.registry = registry
        # Compatibility for old helpers that receive a service-like object.
        self.sync_repo = repository
        self._providers: dict[str, BaseProvider] = {}

    def register_provider(self, adapter: BaseProvider) -> None:
        self._providers[canonical_provider(adapter.spec().provider)] = adapter

    def _get_provider(self, provider: str) -> Optional[BaseProvider]:
        return self._providers.get(canonical_provider(provider))

    async def _source_input(self, adapter, config, user_id) -> SourceInput:
        spec = adapter.spec()
        registry = self.registry or get_synchronize_provider_registry()
        credentials = await registry.resolve_credentials(
            oauth_type=spec.oauth_type, user_id=user_id or "",
            required=spec.auth not in {AuthRequirement.NONE, AuthRequirement.OPTIONAL_OAUTH},
        )
        return SourceInput(
            config={key: deepcopy(value) for key, value in (config or {}).items()
                    if key in {"source", "options", "materialization_schema"}},
            credentials=credentials,
        )

    def remove_sync(self, connection_id: str) -> None:
        self.repository.delete(connection_id)
        log_info(f"[Integration] Removed connection #{connection_id}")

    def pause_sync(self, connection_id: str) -> None:
        self.repository.update_status(connection_id, "paused")

    def resume_sync(self, connection_id: str) -> None:
        self.repository.update_status(connection_id, "active")

    def _default_target_path(
        self,
        *,
        provider: str,
        config: dict,
        fallback_name: str,
        target_folder_path: Optional[str],
    ) -> str:
        for explicit in (target_folder_path, config.get("target_path"), config.get("path")):
            if explicit is not None:
                if not isinstance(explicit, str):
                    raise ValueError("Synchronize destination must be a string")
                return normalize_path(explicit)
        source = config.get("source") if isinstance(config.get("source"), dict) else {}
        name = source.get("resource_name") or fallback_name or provider
        return safe_filename(str(name), fallback=provider)

    async def bootstrap(
        self,
        project_id: str,
        provider: str,
        config: dict,
        target_folder_path: Optional[str] = None,
        credentials_ref: Optional[str] = None,
        direction: str = "bidirectional",
        conflict_strategy: str = "three_way_merge",
        sync_mode: str = "manual",
        trigger: Optional[dict] = None,
        user_id: Optional[str] = None,
    ) -> list[SynchronizeBinding]:
        if sync_mode == "import_once":
            raise ValueError("One-time imports must use ImportJob, not Integration")

        canonical = canonical_provider(provider)
        adapter = self._get_provider(canonical)
        if not adapter:
            raise ValueError(f"No integration connector for provider: {provider}")
        require_synchronize_provider(adapter, mode=sync_mode, direction=direction, trigger=trigger)
        if adapter.spec().creation_mode != "bootstrap":
            raise ValueError(
                f"AccessSurface {canonical} must use direct connection creation"
            )

        trigger_data = dict(trigger or {})
        if not trigger_data.get("type"):
            trigger_data["type"] = sync_mode

        resources = await adapter.list_resources(await self._source_input(adapter, config, user_id))
        created: list[SynchronizeBinding] = []

        for resource in resources:
            existing = self.repository.find_by_config_key(
                canonical, "external_resource_id", resource.external_resource_id,
                project_id=project_id,
            )
            if existing:
                continue

            target_path = self._target_path_for_resource(
                target_folder_path=target_folder_path,
                resource=resource,
            )
            source = dict((config or {}).get("source") or {})
            source.update({
                "provider": canonical,
                "resource_type": resource.node_type,
                "resource_id": resource.external_resource_id,
                "resource_name": resource.name,
            })
            connection_config = {
                **config,
                "source": source,
                "options": dict((config or {}).get("options") or {}),
                "target_path": target_path,
            }
            if user_id:
                connection_config["user_id"] = user_id

            connection = self.repository.create(
                project_id=project_id,
                path=target_path,
                direction=direction,
                provider=canonical,
                config=connection_config,
                credentials_ref=credentials_ref,
                conflict_strategy=conflict_strategy,
                trigger=trigger_data,
                created_by=user_id,
            )
            created.append(connection)
            log_info(
                f"[Integration] Bound {canonical}:{resource.external_resource_id} "
                f"-> {target_path}"
            )

        return created

    def _target_path_for_resource(
        self,
        *,
        target_folder_path: Optional[str],
        resource: ResourceInfo,
    ) -> str:
        base = normalize_path(target_folder_path)
        name = safe_filename(resource.name, resource.external_resource_id)
        return join_path(base, name) if base else name

    async def create_sync(
        self,
        project_id: str,
        provider: str,
        config: dict,
        target_folder_path: Optional[str] = None,
        *,
        credentials_ref: Optional[str] = None,
        direction: str = "inbound",
        conflict_strategy: str = "three_way_merge",
        sync_mode: str = "manual",
        trigger: Optional[dict] = None,
        user_id: Optional[str] = None,
    ) -> SynchronizeBinding:
        return await self.create_connection(
            project_id=project_id,
            provider=provider,
            config=config,
            target_path=target_folder_path,
            credentials_ref=credentials_ref,
            direction=direction,
            conflict_strategy=conflict_strategy,
            sync_mode=sync_mode,
            trigger=trigger,
            user_id=user_id,
        )

    async def create_connection(
        self,
        project_id: str,
        provider: str,
        config: dict,
        target_path: Optional[str] = None,
        *,
        credentials_ref: Optional[str] = None,
        direction: str = "inbound",
        conflict_strategy: str = "three_way_merge",
        sync_mode: str = "manual",
        trigger: Optional[dict] = None,
        user_id: Optional[str] = None,
    ) -> SynchronizeBinding:
        if sync_mode == "import_once":
            raise ValueError("One-time imports must use ImportJob, not Integration")

        canonical = canonical_provider(provider)
        adapter = self._get_provider(canonical)
        if not adapter:
            raise ValueError(f"No integration connector for provider: {provider}")

        require_synchronize_provider(adapter, mode=sync_mode, direction=direction,
                                     trigger=trigger, config=config)
        spec = adapter.spec()
        if spec.creation_mode != "direct":
            raise ValueError(
                f"AccessSurface {canonical} must be created via Integration bootstrap"
            )

        connection_config = dict(config or {})
        connection_config["options"] = dict(connection_config.get("options") or {})
        resolved_target_path = self._default_target_path(
            provider=canonical,
            config=connection_config,
            fallback_name=spec.display_name,
            target_folder_path=target_path,
        )
        connection_config["target_path"] = resolved_target_path
        if user_id:
            connection_config["user_id"] = user_id

        trigger_data = dict(trigger or {})
        if not trigger_data.get("type"):
            trigger_data["type"] = sync_mode

        connection = self.repository.create(
            project_id=project_id,
            path=resolved_target_path,
            direction=direction,
            provider=canonical,
            config=connection_config,
            credentials_ref=credentials_ref,
            conflict_strategy=conflict_strategy,
            trigger=trigger_data,
            created_by=user_id,
        )
        log_info(
            f"[Integration] Created {canonical} connection -> "
            f"{resolved_target_path} (mode={sync_mode})"
        )
        return connection

    def update_trigger(self, connection_id: str, *, mode: str, trigger: dict | None = None):
        connection = self.repository.get_by_id(connection_id)
        if connection is None:
            raise ValueError("Synchronize binding not found")
        trigger_data = dict(trigger or {})
        if not trigger_data.get("type"):
            trigger_data["type"] = mode
        require_synchronize_provider(
            self._get_provider(connection.provider), mode=mode,
            direction=connection.direction, trigger=trigger_data, config=connection.config,
        )
        return self.repository.update(connection_id, trigger=trigger_data)

    async def pull_sync(self, connection_id: str) -> Optional[dict]:
        raise RuntimeError(
            "Integration pull runs must be queued through the sync worker"
        )

    async def pull_all(self, provider: Optional[str] = None) -> list[dict]:
        raise RuntimeError(
            "Integration pull runs must be queued through the sync worker"
        )

    async def push_node(
        self,
        path: str,
        commit_id: str,
        content: Any,
        node_type: str,
    ) -> list[dict]:
        connection = self.repository.find_owner_by_path(path)
        if not connection:
            return []
        if commit_id and connection.last_synchronize_commit_id == commit_id:
            return []
        if connection.status != "active" or connection.direction == "inbound":
            return []
        adapter = self._get_provider(connection.provider)
        if not adapter:
            return []

        try:
            require_synchronize_provider(
                adapter, mode=(connection.trigger or {}).get("type", "manual"),
                direction=connection.direction, config=connection.config,
            )
            source = await self._source_input(adapter, connection.config, connection.created_by)
            push_result = await adapter.push(source, content, node_type)
            if not push_result.success:
                self.repository.update_error(
                    connection.id, push_result.error or "push failed",
                )
                return []
            self.repository.update_sync_point(
                sync_id=connection.id,
                last_synchronize_commit_id=commit_id,
                remote_hash=push_result.remote_hash,
            )
            return [{
                "path": connection.path,
                "provider": connection.provider,
                "success": True,
            }]
        except Exception as exc:
            self.repository.update_error(connection.id, str(exc))
            return []


__all__ = ["SynchronizeService"]
