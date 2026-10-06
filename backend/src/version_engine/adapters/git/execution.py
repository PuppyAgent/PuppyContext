"""Bounded native streams and retained legacy subprocess execution helpers."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager, suppress
from pathlib import Path

from fastapi import HTTPException
from starlette.responses import StreamingResponse

from src.version_engine.infrastructure.owned_work import (
    MAX_SECONDS,
    Execution,
    checkpoint,
    current_execution,
)
from src.version_engine.infrastructure.owned_work import run_owned as run_owned

MAX_PACK_BYTES = 128 * 1024**2
MAX_GRAPH_BYTES = 256 * 1024**2
MAX_OBJECT_BYTES = 64 * 1024**2
MAX_OBJECTS = 100_000
MAX_REFS = 10_000
MAX_DISK_BYTES = 768 * 1024**2
_slots = threading.BoundedSemaphore(2)


@contextmanager
def admission():
    if not _slots.acquire(blocking=False):
        raise HTTPException(503, "Git workers are busy", headers={"Retry-After": "1"})
    token = current_execution.set(Execution())
    retained = False

    def retain(response):
        nonlocal retained
        if isinstance(response, OwnedGitResponse):
            response.on_close = _slots.release
            retained = True
        return response

    try:
        yield retain
    finally:
        current_execution.reset(token)
        if not retained:
            _slots.release()


class OwnedGitResponse(StreamingResponse):
    """A bounded disk spool is owned even when iteration never starts."""

    def __init__(self, output):
        self.output = output
        self.on_close = None
        super().__init__(
            self.chunks(),
            media_type="application/x-git-upload-pack-result",
            headers={"Cache-Control": "no-cache"},
        )

    async def chunks(self):
        while chunk := self.output.read(64 * 1024):
            yield chunk

    def close(self):
        self.output.close()
        if self.on_close is not None:
            release, self.on_close = self.on_close, None
            release()

    async def __call__(self, scope, receive, send):
        try:
            async with asyncio.timeout(MAX_SECONDS):
                await super().__call__(scope, receive, send)
        finally:
            self.close()


class ObjectGitResponse(OwnedGitResponse):
    """Pull one pack chunk at a time, retaining the read pin and worker slot.

    There is no output file or producer queue. Cancellation joins the current
    storage read before closing the generator and releasing its snapshot.
    """

    def __init__(self, chunks, snapshot_context):
        self.iterator = iter(chunks)
        self.snapshot_context = snapshot_context
        self.execution = current_execution.get() or Execution()
        self.on_close = None
        self.closed = False
        StreamingResponse.__init__(self, self.chunks(), media_type="application/x-git-upload-pack-result",
                                   headers={"Cache-Control": "no-cache"})

    async def chunks(self):
        sentinel = object()
        token = current_execution.set(self.execution)
        try:
            while True:
                chunk = await run_owned(next, self.iterator, sentinel)
                if chunk is sentinel:
                    break
                if chunk:
                    yield chunk
        finally:
            current_execution.reset(token)

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.iterator.close()
        finally:
            try:
                self.snapshot_context.__exit__(None, None, None)
            finally:
                if self.on_close is not None:
                    release, self.on_close = self.on_close, None
                    release()

    async def __call__(self, scope, receive, send):
        try:
            async with asyncio.timeout(MAX_SECONDS):
                await StreamingResponse.__call__(self, scope, receive, send)
        finally:
            await run_owned(self.close)


def run_git(command, *, env, directory, timeout, input=None, stdin=None, stdout=subprocess.PIPE):
    """Use a fresh exec helper for rlimits; preexec_fn is unsafe in threads."""
    helper = str(Path(__file__).with_name("process_limits.py"))
    with (
        tempfile.TemporaryFile() as captured,
        tempfile.TemporaryFile() as errors,
        tempfile.TemporaryFile() as source,
    ):
        if input is not None:
            if stdin is not None:
                raise ValueError("input and stdin are mutually exclusive")
            source.write(input)
            source.seek(0)
            stdin = source
        target = captured if stdout == subprocess.PIPE else stdout
        process = subprocess.Popen(
            [sys.executable, helper, *command],
            stdin=stdin or subprocess.DEVNULL,
            stdout=target,
            stderr=errors,
            env=env,
            start_new_session=True,
        )
        deadline = time.monotonic() + timeout

        def check():
            checkpoint()
            if time.monotonic() >= deadline:
                raise TimeoutError("Git subprocess deadline exceeded")
            sizes = os.fstat(target.fileno()).st_size + os.fstat(errors.fileno()).st_size
            if sizes > MAX_GRAPH_BYTES + MAX_PACK_BYTES:
                raise ValueError("Git output budget exceeded")
            for root, _directories, files in os.walk(directory):
                for name in files:
                    try:
                        sizes += os.stat(os.path.join(root, name)).st_size
                    except FileNotFoundError:
                        continue
                    if sizes > MAX_DISK_BYTES:
                        raise ValueError("Git temporary disk budget exceeded")

        try:
            while process.poll() is None:
                check()
                with suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=0.05)
            check()
        finally:
            # Also reap grandchildren on failure/cancellation/timeout. A normal
            # Git command should leave none; its process group is request-owned.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        errors.seek(0)
        captured.seek(0)
        return subprocess.CompletedProcess(
            command,
            process.returncode,
            captured.read() if stdout == subprocess.PIPE else None,
            errors.read(8192),
        )
