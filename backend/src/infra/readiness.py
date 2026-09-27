"""Bounded, read-only probes for the dependencies required by core file operations."""

import asyncio
import os

import boto3
import httpx
from botocore.config import Config
from redis.asyncio import Redis


def _probe_bucket():
    client = boto3.client(
        "s3",
        endpoint_url=os.getenv("S3_ENDPOINT_URL") or None,
        aws_access_key_id=os.getenv("S3_ACCESS_KEY_ID") or None,
        aws_secret_access_key=os.getenv("S3_SECRET_ACCESS_KEY") or None,
        region_name=os.getenv("S3_REGION", "us-east-1"),
        config=Config(
            connect_timeout=2,
            read_timeout=2,
            retries={"max_attempts": 0},
            s3={"addressing_style": "path"},
        ),
    )
    try:
        client.head_bucket(Bucket=os.environ["S3_BUCKET_NAME"])
    finally:
        client.close()


async def core_dependency_errors() -> list[str]:
    async def database():
        key = os.environ["SUPABASE_KEY"]
        async with httpx.AsyncClient(timeout=3) as client:
            result = await client.get(
                os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/projects",
                params={"select": "id", "limit": "0"},
                headers={"apikey": key, "Authorization": f"Bearer {key}"},
            )
            result.raise_for_status()

    async def redis():
        url = os.getenv("AUTH_SECURITY_REDIS_URL") or os.getenv("ETL_REDIS_URL")
        if url:
            async with Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2) as client:
                await client.ping()

    results = await asyncio.gather(
        database(),
        asyncio.to_thread(_probe_bucket),
        redis(),
        return_exceptions=True,
    )
    # Never send connection strings or provider exception bodies to /ready.
    return [
        f"{name} is unavailable"
        for name, result in zip(
            ("Database", "Object storage", "Redis"),
            results,
            strict=True,
        )
        if isinstance(result, BaseException)
    ]
