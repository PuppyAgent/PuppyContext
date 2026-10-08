"""Provider contracts consumed by the Agent lifecycle, without provider SDK types."""

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

Row = dict[str, Any]


class WorkerLost(RuntimeError):
    """Provider no longer has an unconfirmed filesystem to preserve."""


class WorkerDisconnected(ConnectionError):
    """Transport loss does not prove the provider's working copy is gone."""


class WorkspaceWorker(Protocol):
    execution_id: str
    resource: Row
    recovery: Row | None
    restore_point: Row | None
    git_handler: Callable[[Row], Awaitable[Row]] | None

    async def create(self) -> Row: ...
    async def start(self, config: Row) -> None: ...
    async def control(self, action: str, value: Row | None = None) -> Row: ...
    async def snapshot(self) -> Row: ...
    async def send(self, value: Row) -> None: ...
    async def receive(self) -> Row: ...
    async def touch(self) -> None: ...
    async def stop(self) -> None: ...
    async def retain(self) -> None: ...


class WorkerLifecycle(Protocol):
    async def attach(self, resource: Row, project_id: str) -> WorkspaceWorker: ...
    async def cleanup(self, resource: Row) -> None: ...
