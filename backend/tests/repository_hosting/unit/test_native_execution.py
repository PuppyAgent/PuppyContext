"""Cancellation must preserve ownership; limits must stop real subprocesses."""

import asyncio
import os
import subprocess
import sys
import threading
import time

import pytest
from fastapi import HTTPException

from src.version_engine.adapters.git.execution import admission, run_git, run_owned

pytestmark = pytest.mark.hosting_component


@pytest.mark.asyncio
async def test_disconnected_caller_stops_owned_work_before_releasing_its_scope():
    from src.version_engine.infrastructure.owned_work import checkpoint

    exited = threading.Event()

    def worker():
        try:
            while True:
                checkpoint()
                time.sleep(0.005)
        finally:
            exited.set()

    async def disconnected():
        return True

    with pytest.raises(RuntimeError, match="cancelled"):
        await run_owned(worker, cancel_when=disconnected)
    assert exited.is_set()


@pytest.mark.asyncio
async def test_cancelled_native_worker_is_joined_before_lease_scope_exits():
    started, finish, exited = threading.Event(), threading.Event(), threading.Event()

    def worker():
        started.set()
        assert finish.wait(3)
        exited.set()

    task = asyncio.create_task(run_owned(worker))
    while not started.is_set():
        await asyncio.sleep(0.001)
    task.cancel()
    await asyncio.sleep(0.03)
    assert not task.done() and not exited.is_set()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert exited.is_set()


def test_native_worker_capacity_has_no_unbounded_wait_queue():
    with admission(), admission():
        with pytest.raises(HTTPException) as caught, admission():
            pytest.fail("third worker must not be admitted")
        assert caught.value.status_code == 503
    with admission():
        pass


def test_timed_out_native_subprocess_and_child_are_reaped(tmp_path):
    pid = tmp_path / "child"
    script = f"import subprocess,time; p=subprocess.Popen(['sleep','20']); open({str(pid)!r},'w').write(str(p.pid)); time.sleep(20)"
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        run_git([sys.executable, "-c", script], env=dict(os.environ), directory=tmp_path, timeout=1)
    assert time.monotonic() - start < 5
    child = int(pid.read_text())
    # A reparented zombie may remain until the container's init reaps it; it is
    # no longer executing and cannot hold a file or issue remote I/O.
    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(child)], capture_output=True, text=True
    ).stdout.strip()
    assert not state or state.startswith("Z")


@pytest.mark.skipif(
    sys.platform != "linux", reason="Linux production rlimit acceptance runs in Docker"
)
def test_native_child_memory_and_file_limits_fail_without_host_exhaustion(tmp_path):
    memory = run_git(
        [sys.executable, "-c", "bytes(900 * 1024**2)"],
        env=dict(os.environ),
        directory=tmp_path,
        timeout=5,
    )
    assert memory.returncode != 0 and b"MemoryError" in memory.stderr
    path = tmp_path / "oversize"
    code = f"with open({str(path)!r}, 'wb') as f: f.truncate(385 * 1024**2)"
    disk = run_git(
        [sys.executable, "-c", code], env=dict(os.environ), directory=tmp_path, timeout=5
    )
    assert disk.returncode != 0 and path.stat().st_size <= 384 * 1024**2
