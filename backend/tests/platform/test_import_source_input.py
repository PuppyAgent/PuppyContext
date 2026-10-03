"""Regression for the flat ImportJob -> structured shared adapter boundary.

The local PostgreSQL/Redis/MinIO smoke reproduced this with a real CLI/worker.
Here only external HTTP and the write port are substituted, not UrlProvider.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from src.platform.imports.repository import ImportJob
from src.platform.imports.runner import OneTimeImportRunner
from src.provider.registry import ProviderRegistry
from src.provider.url.adapter import UrlProvider
from src.provider.utils.url_parser import UrlParser


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["url", "notion"])
async def test_import_url_reaches_real_parser_and_write_port(monkeypatch, provider):
    from src.platform.imports import runner
    from src.provider.url import adapter

    parser = UrlParser()
    await parser.client.aclose()
    parser.client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"content-type": "text/html"},
            text="<html><title>Snapshot</title><body><h1>Snapshot</h1><p>Real parser bytes</p></body></html>")
    ))
    monkeypatch.setattr(parser.firecrawl_client, "is_available", lambda: False)
    monkeypatch.setattr(adapter, "UrlParser", lambda: parser)
    registry = ProviderRegistry()
    registry.register(UrlProvider())
    monkeypatch.setattr(runner, "get_import_provider_registry", lambda: registry)
    writer = SimpleNamespace(write_bytes=AsyncMock(return_value=SimpleNamespace(
        result=SimpleNamespace(commit_id="commit-from-write-port"))))
    monkeypatch.setattr(runner, "build_leased_worker_write_commands", lambda **kwargs: writer)
    original = {"source": {"resource_url": "https://wrong.example.test"},
                "crawl_options": {"limit": 2}}
    job = ImportJob(id="job-url", project_id="project", created_by="user", provider=provider,
        source_url="https://example.test/page", target_path="snapshot", config=original)
    result = await OneTimeImportRunner().run(job)
    assert result.path == "snapshot/data.md"
    assert result.commit_id == "commit-from-write-port"
    assert b"Real parser bytes" in writer.write_bytes.call_args.args[2]
    assert job.config == original  # queued provenance is not rewritten


@pytest.mark.asyncio
async def test_structured_options_and_source_ids_survive_import_projection(monkeypatch):
    from src.platform.imports import runner

    adapter = UrlProvider()
    seen = []

    async def reject_after_input(config, credentials):
        seen.append(config)
        raise RuntimeError("stop before network")

    monkeypatch.setattr(adapter, "fetch", reject_after_input)
    registry = ProviderRegistry()
    registry.register(adapter)
    monkeypatch.setattr(runner, "get_import_provider_registry", lambda: registry)
    job = ImportJob(id="job", project_id="project", created_by="user", provider="url",
        source_url="https://example.test", config={
            "source": {"resource_id": "selected-id"},
            "options": {"crawl_options": {"limit": 3}}, "crawl_options": {"limit": 9}})
    try:
        with pytest.raises(RuntimeError, match="stop before network"):
            await OneTimeImportRunner().run(job)
        assert seen[0]["source"] == {"resource_id": "selected-id", "resource_url": job.source_url}
        assert seen[0]["options"] == {"crawl_options": {"limit": 3}}
    finally:
        await adapter.url_parser.close()
