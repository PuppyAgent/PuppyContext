"""Synchronize execution engine.

This is the durable Synchronize runtime: trigger -> connector fetch/push ->
project-root Version Engine write -> connection state update.
"""

from __future__ import annotations

import time
from typing import Any

from src.config import settings
from src.provider._base import AuthRequirement, Capability
from src.provider.registry import ProviderRegistry
from src.platform.synchronize.run_repository import SyncRunRepository
from src.platform.synchronize.paths import plan_fetch_result, plan_materialized_result
from src.platform.synchronize.repository import SynchronizeRepository
from src.platform.synchronize.version_write_port import (
    SynchronizeVersionWritePort,
    VersionEngineWritePort,
)
from src.utils.logger import log_debug, log_error, log_info


class SynchronizeEngine:
    def __init__(
        self,
        registry: ProviderRegistry,
        repository: SynchronizeRepository,
        run_repo: SyncRunRepository | None = None,
        write_port: SynchronizeVersionWritePort | None = None,
        runtime_metering: Any | None = None,
    ):
        self.registry = registry
        self.repository = repository
        self.run_repo = run_repo
        self.write_port = write_port or VersionEngineWritePort()
        self._runtime_metering = runtime_metering

    def _runtime_meter(self):
        if self._runtime_metering is None:
            from src.platform.billing.runtime import get_runtime_metering_service

            self._runtime_metering = get_runtime_metering_service()
        return self._runtime_metering

    def _target_exists_as_file(self, project_id: str, target_path: str | None) -> bool:
        if not target_path:
            return False
        try:
            from src.version_engine.bootstrap.dependencies import (
                build_worker_version_engine_container,
            )

            ops = build_worker_version_engine_container().product_operations()
            ops.read_file(project_id, target_path)
            return True
        except Exception:
            return False

    async def execute(
        self,
        connection_id: str,
        trigger_type: str = "manual",
        *,
        run_id: str | None = None,
        _runtime_accounted: bool = False,
    ) -> dict | None:
        connection = self.repository.get_by_id(connection_id)
        if not connection:
            log_error(f"[SynchronizeEngine] Connection not found: {connection_id}")
            return None

        if connection.status not in ("active", "syncing", "error"):
            log_debug(f"[SynchronizeEngine] Skipping {connection_id} (status={connection.status})")
            return None

        adapter = self.registry.get(connection.provider)
        if not adapter:
            log_error(
                f"[SynchronizeEngine] No connector registered for provider: {connection.provider}"
            )
            return None

        # Runtime is reserved at the common AccessSurface execution boundary so
        # API, scheduler and worker callers cannot accidentally bypass billing.
        # Durable sync workers already supply run_id. Older direct callers get
        # a per-invocation identity; when they have a run repository, prepare
        # that durable run before reserving so crash retries reuse the same id.
        if settings.RUNTIME_METERING_MODE != "disabled" and not _runtime_accounted:
            from src.ingest.file.config import etl_config

            prepared_run_id = run_id
            if prepared_run_id is None and self.run_repo is not None:
                try:
                    if hasattr(type(self.run_repo), "create_queued_single_lane"):
                        prepared, created = self.run_repo.create_queued_single_lane(
                            connection.id,
                            trigger_type=trigger_type,
                        )
                        if not created:
                            log_debug(
                                f"[SynchronizeEngine] Skipping {connection_id} "
                                f"(active run={prepared.id} status={prepared.status})"
                            )
                            return None
                    else:
                        prepared = self.run_repo.create(
                            connection.id,
                            trigger_type=trigger_type,
                        )
                    prepared_run_id = prepared.id
                except Exception as exc:
                    log_debug(f"[SynchronizeEngine] Could not prepare metered run: {exc}")
            billing_run_id = prepared_run_id or (
                f"connector:direct:{connection.id}:{time.time_ns()}"
            )
            return await self._runtime_meter().execute(
                audit_context={
                    "run_id": f"connector:pull:{billing_run_id}",
                    "source": "connector",
                    "project_id": connection.project_id,
                    "user_id": connection.created_by,
                    "maximum_runtime_units": max(
                        1,
                        etl_config.sync_task_timeout // 60 + 1,
                    ),
                },
                operation=lambda: self.execute(
                    connection_id,
                    trigger_type,
                    run_id=prepared_run_id,
                    _runtime_accounted=True,
                ),
            )

        run = None
        if self.run_repo:
            try:
                if run_id:
                    run = self.run_repo.get_by_id(run_id)
                    if not run:
                        log_error(f"[SynchronizeEngine] Run not found: {run_id}")
                        return None
                    if run.status in {"success", "completed", "failed", "cancelled", "skipped"}:
                        log_debug(
                            f"[SynchronizeEngine] Skipping run {run_id} (status={run.status})"
                        )
                        return None
                else:
                    if hasattr(type(self.run_repo), "create_queued_single_lane"):
                        run, created = self.run_repo.create_queued_single_lane(
                            connection.id,
                            trigger_type=trigger_type,
                        )
                        if not created:
                            log_debug(
                                f"[SynchronizeEngine] Skipping {connection_id} "
                                f"(active run={run.id} status={run.status})"
                            )
                            return None
                    else:
                        run = self.run_repo.create(connection.id, trigger_type=trigger_type)
            except Exception as exc:
                log_debug(f"[SynchronizeEngine] Could not prepare run record: {exc}")

        try:
            if run and self.run_repo and getattr(run, "status", "running") != "running":
                if hasattr(type(self.run_repo), "claim_running"):
                    claimed = self.run_repo.claim_running(run.id)
                    if not claimed:
                        log_debug(f"[SynchronizeEngine] Run not claimed: {run.id}")
                        return None
                    run = claimed
                else:
                    run = self.run_repo.mark_running(run.id) or run
            self.repository.update_status(connection.id, "syncing")

            spec = adapter.spec()
            user_id = connection.created_by or (connection.config or {}).get("user_id", "")
            credentials = await self.registry.resolve_credentials(
                oauth_type=spec.oauth_type,
                user_id=user_id,
                required=spec.auth != AuthRequirement.OPTIONAL_OAUTH,
            )

            result = await adapter.fetch(connection.config or {}, credentials)

            if result.content_hash and result.content_hash == connection.remote_hash:
                self.repository.update_status(connection.id, "active")
                if run and self.run_repo:
                    self.run_repo.complete(
                        run.id,
                        status="skipped",
                        result_summary="No changes detected",
                    )
                return None

            materializer = self.registry.resolve_materializer(
                connection.provider,
                (connection.config or {}).get("materialization_schema"),
            )
            if materializer is not None:
                write_plan = plan_materialized_result(
                    sync=connection,
                    materialized=materializer.materialize(result, connection),
                )
            else:
                target_exists_as_file = self._target_exists_as_file(
                    connection.project_id,
                    connection.path,
                )
                write_plan = plan_fetch_result(
                    sync=connection,
                    result=result,
                    target_exists_as_file=target_exists_as_file,
                )

            source = (connection.config or {}).get("source") or {}
            external_resource_id = source.get("resource_id", "")
            actor = f"integration:{connection.provider}:{external_resource_id}"

            outcome = await self.write_port.write_plan(
                project_id=connection.project_id,
                plan=write_plan,
                actor=actor,
            )
            commit_id = outcome.commit_id
            self.repository.update_sync_point(
                sync_id=connection.id,
                last_sync_commit_id=commit_id,
                remote_hash=result.content_hash,
            )

            log_info(
                f"[SynchronizeEngine] {connection.provider}:{external_resource_id} "
                f"-> {write_plan.result_path} commit={commit_id}"
            )

            if run and self.run_repo:
                self.run_repo.complete(
                    run.id,
                    status="success",
                    result_summary=result.summary,
                )

            return {
                "access_point_id": connection.id,
                "connection_id": connection.id,
                "path": write_plan.result_path,
                "provider": connection.provider,
                "commit_id": commit_id,
                "status": "success",
                "summary": result.summary,
                "run_id": run.id if run else None,
            }

        except NotImplementedError:
            log_debug(f"[SynchronizeEngine] fetch not implemented for {connection.provider}")
            self.repository.update_status(connection.id, "active")
            if run and self.run_repo:
                self.run_repo.complete(
                    run.id,
                    status="skipped",
                    result_summary="Fetch not implemented",
                )
            return None
        except Exception as exc:
            log_error(f"[SynchronizeEngine] Failed for {connection_id}: {exc}")
            self.repository.update_error(connection.id, str(exc))
            if run and self.run_repo:
                self.run_repo.complete(run.id, status="failed", error=str(exc))
            return None

    async def execute_all(self, provider: str | None = None) -> list[dict]:
        connections = self.repository.list_active(provider)
        results = []
        for connection in connections:
            if connection.direction == "outbound":
                continue
            result = await self.execute(connection.id)
            if result:
                results.append(result)
        if results:
            log_info(f"[SynchronizeEngine] execute_all: {len(results)} connections updated")
        return results

    async def execute_for_access_surface(self, adapter) -> str | None:
        if adapter.kind in ("cli", "agent", "sandbox", "git_remote"):
            log_debug(f"[SynchronizeEngine] access connector cannot run on demand: {adapter.id}")
            return None
        result = await self.execute(adapter.id, trigger_type="manual")
        return (result or {}).get("run_id")

    async def push_execute(
        self,
        path: str,
        commit_id: str,
        content: Any,
        node_type: str,
        *,
        _runtime_accounted: bool = False,
    ) -> dict | None:
        connection = self.repository.find_owner_by_path(path)
        if not connection:
            return None
        if connection.direction == "inbound" or connection.status != "active":
            return None
        if commit_id and connection.last_sync_commit_id == commit_id:
            return None

        adapter = self.registry.get(connection.provider)
        if not adapter:
            log_error(f"[SynchronizeEngine] push: no connector for provider {connection.provider}")
            return None

        spec = adapter.spec()
        if not (spec.capabilities & Capability.PUSH):
            return None

        if settings.RUNTIME_METERING_MODE != "disabled" and not _runtime_accounted:
            stable_commit = commit_id or f"unversioned:{time.time_ns()}"
            return await self._runtime_meter().execute(
                audit_context={
                    "run_id": f"connector:push:{connection.id}:{stable_commit}",
                    "source": "connector",
                    "project_id": connection.project_id,
                    "user_id": connection.created_by,
                },
                operation=lambda: self.push_execute(
                    path=path,
                    commit_id=commit_id,
                    content=content,
                    node_type=node_type,
                    _runtime_accounted=True,
                ),
            )

        run = None
        if self.run_repo:
            try:
                run = self.run_repo.create(connection.id, trigger_type="push")
            except Exception as exc:
                log_debug(f"[SynchronizeEngine] Could not create push run record: {exc}")

        try:
            push_result = await adapter.push(connection, content, node_type)
            if not push_result.success:
                error = push_result.error or "Push returned failure"
                self.repository.update_error(connection.id, error)
                if run and self.run_repo:
                    self.run_repo.complete(run.id, status="failed", error=error)
                return None

            self.repository.update_sync_point(
                sync_id=connection.id,
                last_sync_commit_id=commit_id,
                remote_hash=push_result.remote_hash,
            )
            if run and self.run_repo:
                self.run_repo.complete(
                    run.id,
                    status="success",
                    result_summary=f"Pushed commit {commit_id}",
                )
            return {
                "access_point_id": connection.id,
                "connection_id": connection.id,
                "path": path,
                "provider": connection.provider,
                "commit_id": commit_id,
                "direction": "push",
                "status": "success",
                "run_id": run.id if run else None,
            }
        except NotImplementedError:
            if run and self.run_repo:
                self.run_repo.complete(
                    run.id,
                    status="skipped",
                    result_summary="Push not implemented",
                )
            return None
        except Exception as exc:
            self.repository.update_error(connection.id, str(exc))
            if run and self.run_repo:
                self.run_repo.complete(run.id, status="failed", error=str(exc))
            return None
