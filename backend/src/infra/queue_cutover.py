"""Read-only drain verification for coordinated entrypoint worker upgrades.

Run on the OLD deployment after disabling API/scheduler/webhook producers and
letting old workers finish ALL ready, deferred and retry jobs. Never delete or
rewrite serialized jobs to make this check pass. An empty snapshot is not proof
that a producer stopped; that requires separate deployment/operator evidence.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone

from arq.connections import RedisSettings, create_pool
from arq.utils import timestamp_ms

from src.infra.queue_config import QueueConnectionConfig


async def inspect_drain(redis, queues: list[str]) -> dict:
    now = timestamp_ms()
    counts = {}
    for queue in queues:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.zcard(queue)
            pipe.zcount(queue, "-inf", now)
            total, ready = await pipe.execute()
        counts[queue] = {"total": total, "ready": ready, "deferred": total - ready}
    # Conservative across this ARQ Redis DB: unknown workers/retry ownership is
    # a blocker, not grounds to remove keys. No payloads or credentials logged.
    in_progress = sum([1 async for _ in redis.scan_iter(match="arq:in-progress:*")])
    retries = sum([1 async for _ in redis.scan_iter(match="arq:retry:*")])
    return {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "queues": counts,
        "in_progress": in_progress,
        "retry_keys": retries,
        "drained": not any(item["total"] for item in counts.values()) and not in_progress and not retries,
        "producer_stop_verified": False,
    }


async def _main(args) -> int:
    redis = await create_pool(RedisSettings.from_dsn(QueueConnectionConfig().redis_url))
    try:
        report = await inspect_drain(redis, args.queues)
        print(json.dumps(report, indent=2))
        return 0 if report["drained"] else 2
    finally:
        await redis.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queues", nargs="+", required=True,
                        help="Actual old queue names (normally imports syncs etl)")
    raise SystemExit(asyncio.run(_main(parser.parse_args())))
