"""Project-leased orchestration for durable OCR provider jobs."""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol

from src.infra.file_processing.ocr.base import (
    ExternalJobCreatedHook,
    ExternalJobTerminalHook,
    OCRProvider,
    ParsedDocument,
    parse_document_with_external_lifecycle,
)


class ProjectWriteLeaseFactory(Protocol):
    """Structural subset of the Project write-lease constructor."""

    def __call__(
        self,
        project_id: str,
        operation: str,
        **kwargs: Any,
    ) -> AbstractAsyncContextManager[Any]: ...


async def _drain_after_cancellation(task: asyncio.Task[Any]) -> None:
    """Wait until ``task`` no longer owns work, despite repeated cancellation."""

    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # A second cancellation of the owner must not make its lease look
            # released while the shielded provider/thread is still running.
            continue
        except BaseException:
            break

    # Retrieve a terminal exception to avoid an unhandled-task warning.  The
    # caller's cancellation remains the externally visible outcome.
    if task.done() and not task.cancelled():
        task.exception()


async def await_before_propagating_cancellation(task: asyncio.Task[Any]) -> Any:
    """Shield work and drain it before propagating owner cancellation.

    ``asyncio.to_thread`` cannot stop its underlying thread when its awaiter is
    cancelled.  A Project lease must therefore remain open until the provider
    lifecycle (including any materialization thread) has actually stopped.
    """

    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await _drain_after_cancellation(task)
        raise


async def run_ocr_lifecycle_under_project_lease(
    *,
    lease_factory: ProjectWriteLeaseFactory,
    project_id: str,
    provider: OCRProvider,
    file_url: str,
    data_id: str | None = None,
    on_created: ExternalJobCreatedHook | None = None,
    on_terminal: ExternalJobTerminalHook | None = None,
    operation: str = "etl.ocr_external",
    lease_kwargs: dict[str, Any] | None = None,
) -> ParsedDocument:
    """Create, persist, wait, and materialize OCR under one Project lease.

    ``ProjectWriteLease`` can be passed directly as ``lease_factory``.  Its
    normal active-lease reuse semantics make this helper safe both at a worker
    entry point and inside an already leased job.
    """

    async with lease_factory(project_id, operation, **(lease_kwargs or {})):
        lifecycle = asyncio.create_task(
            parse_document_with_external_lifecycle(
                provider,
                file_url=file_url,
                data_id=data_id,
                on_created=on_created,
                on_terminal=on_terminal,
            )
        )
        return await await_before_propagating_cancellation(lifecycle)
