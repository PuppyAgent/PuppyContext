import asyncio

import httpx
import pytest

from src.infra.supabase.instrumentation import (
    DatabaseTrace,
    DatabaseTransport,
    database_trace,
    instrument_httpx_client,
)


def test_attempts_include_failure_and_explicit_retry_without_secret_data():
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        if count == 1:
            raise httpx.ConnectError("failure", request=request)
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=DatabaseTransport(httpx.MockTransport(handler)))
    trace = DatabaseTrace("retry")
    with database_trace(trace):
        with pytest.raises(httpx.ConnectError):
            client.get("https://db.test/rest/v1/rpc/context?secret=never-log")
        assert client.get("https://db.test/rest/v1/rpc/context?secret=never-log").is_success
    assert trace.attempts == 2 and trace.failures == 1 and trace.inflight == 0
    assert "never-log" not in str(trace.report())
    assert trace.operations == {"GET rpc/context": 2}


def test_instrumentation_preserves_proxy_routes(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:19080")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    with httpx.Client(trust_env=True) as client:
        remote = client._transport_for_url(httpx.URL("https://supabase.example"))
        local = client._transport_for_url(httpx.URL("http://127.0.0.1:9000"))
        instrument_httpx_client(client)
        assert client._transport_for_url(httpx.URL("https://supabase.example")).transport is remote
        assert client._transport_for_url(httpx.URL("http://127.0.0.1:9000")).transport is local


@pytest.mark.asyncio
async def test_streamed_api_telemetry_includes_late_database_calls(caplog):
    from src.platform.access.adapters.agent.runtime.telemetry import AgentDatabaseTelemetry

    client = httpx.Client(
        transport=DatabaseTransport(httpx.MockTransport(lambda _: httpx.Response(200)))
    )

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await asyncio.to_thread(client.get, "http://db.test/rest/v1/rpc/first")
        await asyncio.sleep(0)
        await asyncio.to_thread(client.get, "http://db.test/rest/v1/rpc/late")
        await send({"type": "http.response.body", "body": b"done"})

    async def ignore(*args):
        pass

    with caplog.at_level("INFO"):
        await AgentDatabaseTelemetry(app)(
            {"type": "http", "path": "/api/v1/agents/runs/id/events", "method": "GET"},
            ignore,
            ignore,
        )
    record = next(
        record for record in caplog.records if record.message == "cloud_agent_api_performance"
    )
    assert record.agent_api_performance["attempts"] == 2
