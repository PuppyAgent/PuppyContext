"""Durable Project write admission outside the Version Engine.

The Version Engine stays protocol- and lifecycle-neutral. Every production
entry point wraps a write in this renewable database lease; Project deletion
atomically closes new admission, drains active leases, and only then removes
relational state and starts object cleanup.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from contextvars import ContextVar, Token
from types import TracebackType
from typing import Any, Protocol
from uuid import uuid4

from fastapi import Depends, Request

from src.exceptions import AppException, ErrorCode
from src.infra.supabase.client import SupabaseClient
from src.version_engine.bootstrap.container import VersionEngineContainer
from src.version_engine.bootstrap.dependencies import get_version_engine_container

MAX_PROJECT_WRITE_LEASE_TTL_SECONDS = 7200
PHYSICAL_S3_WRITE_LEASE_TTL_SECONDS = 7200

_active_project_leases: ContextVar[tuple[ProjectWriteLease, ...]] = ContextVar(
    "active_project_write_leases",
    default=(),
)


def active_project_write_lease(project_id: str) -> ProjectWriteLease | None:
    """Return a live claim, never a stale copied ContextVar membership.

    asyncio copies ContextVar containers into child tasks.  The lease object is
    deliberately shared, so its active/lost/released state remains observable
    after the parent context exits.  A copied background context therefore
    cannot mistake a released lease for current write admission.
    """

    for lease in reversed(_active_project_leases.get()):
        if lease.project_id == project_id and lease.is_active:
            return lease
    return None


def has_active_project_write_lease(project_id: str) -> bool:
    return active_project_write_lease(project_id) is not None


class ProjectWriteLeaseFactory(Protocol):
    def __call__(self, project_id: str, operation: str, **kwargs: Any) -> Any: ...


def get_project_write_lease_factory() -> ProjectWriteLeaseFactory:
    """FastAPI/worker seam for the process-wide admission implementation."""

    return ProjectWriteLease


class ProjectWriteLeaseRepository:
    def __init__(self, client=None):
        self._client = client or SupabaseClient().get_client()

    def acquire(
        self,
        *,
        project_id: str,
        lease_id: str,
        holder_id: str,
        operation: str,
        ttl_seconds: int,
        initialization_operation_key: str | None = None,
        initialization_actor: str | None = None,
        initialization_worker: str | None = None,
    ) -> dict[str, Any]:
        response = self._client.rpc(
            "acquire_project_write_lease",
            {
                "p_project_id": project_id,
                "p_lease_id": lease_id,
                "p_holder_id": holder_id,
                "p_operation": operation,
                "p_ttl_seconds": ttl_seconds,
                "p_initialization_operation_key": initialization_operation_key,
                "p_initialization_actor": initialization_actor,
                "p_initialization_worker": initialization_worker,
            },
        ).execute()
        data = response.data
        if isinstance(data, list) and len(data) == 1:
            data = data[0]
        if not isinstance(data, dict):
            raise RuntimeError("acquire_project_write_lease returned an invalid response")
        return data

    def renew(self, *, lease_id: str, holder_id: str, ttl_seconds: int) -> bool:
        response = self._client.rpc(
            "renew_project_write_lease",
            {
                "p_lease_id": lease_id,
                "p_holder_id": holder_id,
                "p_ttl_seconds": ttl_seconds,
            },
        ).execute()
        return bool(response.data)

    def release(self, *, lease_id: str, holder_id: str) -> bool:
        response = self._client.rpc(
            "release_project_write_lease",
            {"p_lease_id": lease_id, "p_holder_id": holder_id},
        ).execute()
        return bool(response.data)


class ProjectWriteLease:
    def __init__(
        self,
        project_id: str,
        operation: str,
        *,
        repository: ProjectWriteLeaseRepository | None = None,
        ttl_seconds: int = 120,
        initialization_operation_key: str | None = None,
        initialization_actor: str | None = None,
        initialization_worker: str | None = None,
        reuse_active: bool = True,
        heartbeat_interval_seconds: float | None = None,
    ) -> None:
        self.project_id = project_id
        self.operation = operation
        self.repository = repository or ProjectWriteLeaseRepository()
        self.ttl_seconds = max(
            30,
            min(ttl_seconds, MAX_PROJECT_WRITE_LEASE_TTL_SECONDS),
        )
        self.initialization_operation_key = initialization_operation_key
        self.initialization_actor = initialization_actor
        self.initialization_worker = initialization_worker
        self.reuse_active = reuse_active
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.lease_id = str(uuid4())
        self.holder_id = f"writer:{uuid4()}"
        self._heartbeat: asyncio.Task[None] | None = None
        self._owner: asyncio.Task[Any] | None = None
        self._lost_error: Exception | None = None
        self._context_token: Token[tuple[ProjectWriteLease, ...]] | None = None
        self._reused = False
        self._active = False

    @property
    def is_active(self) -> bool:
        return self._active and self._lost_error is None

    def initialization_proof(self) -> dict[str, str | None]:
        """Proof a physical child lease may inherit while this claim is live."""

        if not self.is_active:
            return {}
        return {
            "initialization_operation_key": self.initialization_operation_key,
            "initialization_actor": self.initialization_actor,
            "initialization_worker": self.initialization_worker,
        }

    async def __aenter__(self) -> ProjectWriteLease:
        active = _active_project_leases.get()
        if self.reuse_active and active_project_write_lease(self.project_id) is not None:
            self._reused = True
            return self
        outcome = await asyncio.to_thread(
            self.repository.acquire,
            project_id=self.project_id,
            lease_id=self.lease_id,
            holder_id=self.holder_id,
            operation=self.operation,
            ttl_seconds=self.ttl_seconds,
            initialization_operation_key=self.initialization_operation_key,
            initialization_actor=self.initialization_actor,
            initialization_worker=self.initialization_worker,
        )
        result = str(outcome.get("outcome") or "")
        if result == "unavailable":
            raise AppException(
                code=ErrorCode.VERSION_CONFLICT,
                status_code=409,
                message="Project is not accepting writes",
                details={"code": "project_write_admission_closed"},
            )
        if result not in {"acquired", "replayed"}:
            raise RuntimeError(f"Project write lease was not acquired: {result or 'missing'}")
        self._owner = asyncio.current_task()
        self._active = True
        live = tuple(lease for lease in active if lease.is_active)
        self._context_token = _active_project_leases.set((*live, self))
        self._heartbeat = asyncio.create_task(self._renew_loop())
        return self

    async def _renew_loop(self) -> None:
        try:
            while True:
                interval = self.heartbeat_interval_seconds
                if interval is None:
                    interval = min(60, max(10, self.ttl_seconds // 3))
                await asyncio.sleep(interval)
                renewed = await asyncio.to_thread(
                    self.repository.renew,
                    lease_id=self.lease_id,
                    holder_id=self.holder_id,
                    ttl_seconds=self.ttl_seconds,
                )
                if not renewed:
                    raise RuntimeError("Project write lease was lost")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._lost_error = exc
            # Mutate the shared claim before cancellation. Child tasks holding
            # a copied ContextVar see the invalidation immediately.
            self._active = False
            if self._owner is not None:
                self._owner.cancel()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        if self._reused:
            return False
        self._active = False
        if self._heartbeat is not None:
            self._heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await self._heartbeat
        release_task = asyncio.create_task(
            asyncio.to_thread(
                self.repository.release,
                lease_id=self.lease_id,
                holder_id=self.holder_id,
            )
        )
        try:
            try:
                await asyncio.shield(release_task)
            except asyncio.CancelledError:
                # Releasing is a safety boundary.  Do not let cancellation
                # orphan the thread while the context appears closed.
                await release_task
                raise
        finally:
            if self._context_token is not None:
                _active_project_leases.reset(self._context_token)
            if self._lost_error is not None:
                raise AppException(
                    code=ErrorCode.REPOSITORY_STORAGE_UNAVAILABLE,
                    status_code=503,
                    message="Project write admission lease was lost",
                    details={"code": "project_write_lease_lost", "retryable": True},
                ) from self._lost_error
        return False


_WRITE_METHODS = {
    "write_file",
    "write_bytes",
    "mkdir",
    "move",
    "copy",
    "touch",
    "delete",
    "bulk_write",
    "bulk_write_refs",
}


class LeasedVersionWriteCommandService:
    """Transparent lease boundary for the Product command ingress."""

    def __init__(
        self,
        commands: Any,
        *,
        write_lease_factory: ProjectWriteLeaseFactory = ProjectWriteLease,
    ):
        self._commands = commands
        self._write_lease_factory = write_lease_factory

    @property
    def ops(self):
        return self._commands.ops

    def __getattr__(self, name: str):
        target = getattr(self._commands, name)
        if name not in _WRITE_METHODS:
            return target

        async def leased(project_id: str, *args, **kwargs):
            async with self._write_lease_factory(project_id, f"product.{name}"):
                return await target(project_id, *args, **kwargs)

        return leased


def lease_write_commands(
    commands: Any,
    *,
    write_lease_factory: ProjectWriteLeaseFactory = ProjectWriteLease,
) -> LeasedVersionWriteCommandService:
    if isinstance(commands, LeasedVersionWriteCommandService):
        return commands
    return LeasedVersionWriteCommandService(
        commands,
        write_lease_factory=write_lease_factory,
    )


def build_leased_worker_write_commands(
    *,
    write_lease_factory: ProjectWriteLeaseFactory = ProjectWriteLease,
    project_id: str | None = None,
    user_id: str | None = None,
    operation_key: str | None = None,
) -> LeasedVersionWriteCommandService:
    from src.version_engine.adapters.product.commands import VersionWriteCommandService
    from src.version_engine.bootstrap.dependencies import (
        build_worker_version_engine_container,
    )

    container = build_worker_version_engine_container()
    ops = container.product_operations()
    if project_id is not None or user_id is not None:
        if not project_id or not user_id:
            raise PermissionError("producer requires its Project and initiating user")
        ops = ops.for_user(project_id, user_id, operation_key=operation_key)
    return lease_write_commands(
        VersionWriteCommandService(ops),
        write_lease_factory=write_lease_factory,
    )


def get_leased_version_write_command_service(
    container: VersionEngineContainer = Depends(get_version_engine_container),
    write_lease_factory: ProjectWriteLeaseFactory = Depends(get_project_write_lease_factory),
) -> LeasedVersionWriteCommandService:
    return lease_write_commands(
        container.write_commands(),
        write_lease_factory=write_lease_factory,
    )


async def git_project_write_lease(request: Request) -> AsyncIterator[None]:
    """Authorize, then fence every Git request for its full response lifetime.

    Clone/fetch requests are product reads, but they also materialize the
    Project-owned Git transport cache on the host.  They therefore participate
    in the same lifecycle drain as canonical mutations.  Read-only credentials
    remain valid for those requests; only receive-pack and explicit cache
    rebuild operations require a write-capable runtime grant.
    """

    path = request.url.path
    is_mutation = path.endswith("/git-receive-pack") or path.endswith("/rebuild-cache")
    if path.endswith("/info/refs"):
        is_mutation = str(request.query_params.get("service") or "") == "git-receive-pack"

    project_id = str(request.path_params.get("project_id") or "")
    auth: dict[str, Any] | None = None
    if not project_id:
        access_key = str(request.path_params.get("access_key") or "")
        if access_key:
            from src.version_engine.entrypoints.git.router import (
                resolve_git_access_point,
            )

            project_id, auth = await resolve_git_access_point(access_key, request)
    else:
        from src.version_engine.entrypoints.git.auth import (
            resolve_git_project_auth,
            resolve_git_scope_auth,
        )

        scope_id = str(request.path_params.get("scope_id") or "")
        auth = (
            await resolve_git_scope_auth(project_id, scope_id, request)
            if scope_id
            else await resolve_git_project_auth(project_id, request)
        )
    if not project_id:
        yield
        return
    runtime_grant = (auth or {}).get("_runtime_grant")
    if runtime_grant is None:
        raise AppException(
            code=ErrorCode.FORBIDDEN,
            status_code=403,
            message="Git credential does not allow repository access",
        )
    if is_mutation and not bool(getattr(runtime_grant, "can_write", False)):
        raise AppException(
            code=ErrorCode.FORBIDDEN,
            status_code=403,
            message="Git credential does not allow writes",
        )

    async with ProjectWriteLease(project_id, f"git.{request.method.lower()}"):
        yield
