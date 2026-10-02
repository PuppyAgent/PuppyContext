"""Real ARQ serialization/broker on a private, disposable Unix-socket Redis."""
import asyncio
import shutil
import subprocess
import tempfile
from pathlib import Path
from datetime import timedelta
from redis.exceptions import ConnectionError as RedisConnectionError

import pytest
import pytest_asyncio
from arq.connections import ArqRedis
from arq.jobs import deserialize_job

from src.infra.queue_cutover import inspect_drain
from src.platform.imports.arq_client import ImportArqClient
from src.platform.synchronize.arq_client import SyncArqClient
from src.platform.synchronize.github.arq_client import GithubSyncArqClient


@pytest_asyncio.fixture
async def redis():
    executable = shutil.which("redis-server")
    if not executable:
        pytest.skip("redis-server is needed for the real ARQ cutover test")
    # macOS limits AF_UNIX paths to 104 bytes; pytest's default path is longer.
    temp = tempfile.TemporaryDirectory(prefix="p1q-", dir="/tmp")
    tmp_path = Path(temp.name)
    socket = tmp_path / "redis.sock"
    process = subprocess.Popen([
        executable, "--port", "0", "--unixsocket", str(socket),
        "--save", "", "--appendonly", "no", "--dir", str(tmp_path),
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    client = ArqRedis(unix_socket_path=str(socket))
    try:
        for _ in range(100):
            try:
                if await client.ping():
                    break
            except (RedisConnectionError, OSError):
                if process.poll() is not None:
                    raise
            await asyncio.sleep(0.02)
        else:
            pytest.fail("private Redis did not start")
        yield client
    finally:
        await client.aclose()
        process.terminate()
        process.wait(timeout=5)
        temp.cleanup()


async def test_real_producers_use_domain_queues_and_keep_github_dedup(redis):
    imports, synchronize, github = ImportArqClient(), SyncArqClient(), GithubSyncArqClient()
    for client in (imports, synchronize, github):
        client._pool = redis
    import_id = await imports.enqueue_import("import-1")
    run_id = await synchronize.enqueue_sync_run("run-1")
    github_id = await github.enqueue_pull("binding-1", dedup_key="gh-import:binding-1:sha")
    assert await github.enqueue_pull("binding-1", dedup_key="gh-import:binding-1:sha") is None
    assert await redis.zrange(imports.queue_name, 0, -1) == [import_id.encode()]
    assert set(await redis.zrange(synchronize.queue_name, 0, -1)) == {run_id.encode(), github_id.encode()}
    functions = [deserialize_job(await redis.get(f"arq:job:{job_id}")).function
                 for job_id in (import_id, run_id, github_id)]
    assert functions == ["execute_import_job", "execute_synchronize_run", "execute_synchronize_github_pull"]


async def test_cutover_rejects_deferred_retry_and_inflight_without_mutating_jobs(redis):
    delayed = await redis.enqueue_job("execute_github_import", "old-binding",
                                      _queue_name="imports", _defer_by=timedelta(hours=1))
    await redis.set(f"arq:retry:{delayed.job_id}", 2)
    await redis.set(f"arq:in-progress:{delayed.job_id}", b"1")
    before = await redis.get(f"arq:job:{delayed.job_id}")
    report = await inspect_drain(redis, ["imports", "syncs"])
    assert report["drained"] is False
    assert report["queues"]["imports"] == {"total": 1, "ready": 0, "deferred": 1}
    assert report["in_progress"] == report["retry_keys"] == 1
    assert await redis.get(f"arq:job:{delayed.job_id}") == before
    assert await redis.zcard("imports") == 1


async def test_empty_snapshot_does_not_claim_producers_stopped(redis):
    report = await inspect_drain(redis, ["imports", "syncs"])
    assert report["drained"] is True
    assert report["producer_stop_verified"] is False
