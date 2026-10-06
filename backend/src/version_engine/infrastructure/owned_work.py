"""Owned native workers: cancellation never outlives the caller's write lease."""

from __future__ import annotations

import asyncio
import threading
import time
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass, field

MAX_SECONDS = 300


@dataclass
class Execution:
    deadline: float = field(default_factory=lambda: time.monotonic() + MAX_SECONDS)
    cancelled: threading.Event = field(default_factory=threading.Event)

    def check(self):
        if self.cancelled.is_set():
            raise RuntimeError("Repository operation cancelled")
        if time.monotonic() >= self.deadline:
            raise TimeoutError("Repository operation deadline exceeded")


current_execution: ContextVar[Execution | None] = ContextVar(
    "native_repository_execution", default=None
)


def checkpoint():
    execution = current_execution.get()
    if execution is not None:
        execution.check()


async def run_owned(function, *args, cancel_when=None, **kwargs):
    """Join a cancelled worker before its request file/lease can be released.

    Killing a Git process is safe; abandoning a thread with an outstanding S3
    PUT is not. The storage layer retains unknown I/O in its durable ledger.
    """
    execution = current_execution.get() or Execution()
    token = current_execution.set(execution)
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))

    async def monitor():
        while not task.done():
            try:
                cancelled = await cancel_when()
            except Exception:
                cancelled = True
            if cancelled:
                execution.cancelled.set()
                return
            await asyncio.sleep(0.1)

    watcher = asyncio.create_task(monitor()) if cancel_when is not None else None
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        execution.cancelled.set()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        if not task.cancelled():
            error = task.exception()
            if error is None:
                response = task.result()
                close = getattr(response, "close", None)
                if close is not None:
                    close()
        raise
    finally:
        if watcher is not None:
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher
        current_execution.reset(token)
