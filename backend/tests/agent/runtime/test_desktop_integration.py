"""Production Desktop Agent component + IPC + JWT + HTTP + PG/S3 + real Pi.

Enrollment/readiness and model/billing are explicit fixture seams. This is not
hosted E2B/provider or full Desktop application acceptance.
"""

import asyncio
import json
import os
import secrets
import time
from pathlib import Path

import jwt
import pytest
from fastapi import FastAPI

from src.config import settings
from src.infra.supabase.instrumentation import DatabaseTrace
from src.platform.access.adapters.agent.dependencies import get_agent_service
from src.platform.access.adapters.agent.router import router
from src.platform.access.adapters.agent.runtime.telemetry import AgentDatabaseTelemetry
from src.platform.auth import dependencies as auth_dependencies
from src.platform.auth.service import AuthService
from src.platform.project.write_lease import ProjectWriteLease
from tests.agent.runtime.test_data_access import FileQuestionModel
from tests.agent.runtime.test_supervisor import prepared as prepared_fixture
from tests.repository_hosting.harness.http_server import _serve_git_app

prepared = prepared_fixture
pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_desktop_renders_real_worker_file_answers_and_restores_session(
    prepared, monkeypatch, tmp_path, caplog
):
    desktop_value = os.environ.get("AGENT_DESKTOP_REPO")
    if not desktop_value:
        pytest.skip(
            "Set AGENT_DESKTOP_REPO to the matching Desktop checkout for cross-repository acceptance"
        )
    desktop = Path(desktop_value).resolve()
    electron = desktop / "node_modules/.bin/electron"
    runner = desktop / "tests/integration/cloud/agent/cloud-agent-runtime.smoke.mjs"
    assert electron.exists() and runner.exists()
    c = prepared
    c.postgres.sql(f"UPDATE agent_runs SET state='failed' WHERE id='{c.run['id']}'")
    async with ProjectWriteLease(c.project, "fixture.desktop"):
        await c.ops.bulk_write(
            c.project,
            {"alpha.md": b"first knowledge note", "beta.md": b"second", "gamma.md": b"third"},
            who="user:" + c.user,
            source_channel="access_git",
        )
    secret = secrets.token_urlsafe(48)
    monkeypatch.setattr(settings, "SKIP_AUTH", False)
    monkeypatch.setattr(settings, "JWT_SECRET", secret)
    monkeypatch.setattr(settings, "SUPABASE_PUBLIC_URL", "http://owned-agent.test")
    monkeypatch.setattr(auth_dependencies, "_auth_service", AuthService(c.repo.client))
    token = jwt.encode(
        {
            "sub": c.user,
            "email": "agent-fixture@example.test",
            "aud": "authenticated",
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
            "iss": "http://owned-agent.test/auth/v1",
            "role": "authenticated",
        },
        secret,
        algorithm="HS256",
    )
    application = FastAPI()
    application.include_router(router, prefix="/api/v1")
    from src.platform.access.adapters.agent.chat import dependencies as chat_dependencies
    from src.platform.access.adapters.agent.chat.router import router as chat_router

    monkeypatch.setattr(chat_dependencies, "_chat_service", None)
    application.include_router(chat_router, prefix="/api/v1")
    application.add_middleware(AgentDatabaseTelemetry)
    application.dependency_overrides[get_agent_service] = lambda: c.service
    reports = []
    aggregate = DatabaseTrace("desktop-three-turns-including-dispatch-and-history")
    c.repo.client.transport.aggregate = aggregate
    stop = asyncio.Event()

    async def dispatch():
        while not stop.is_set():
            run = await asyncio.to_thread(c.repo.claim_run, worker="desktop-acceptance")
            if run is None:
                await asyncio.sleep(1)
                continue
            supervisor = c.supervisor(model=FileQuestionModel())
            await supervisor.run_claim(run)
            reports.append(
                {
                    **supervisor.metrics.report(),
                    "elapsed_seconds": supervisor.elapsed_seconds,
                    "first_text_seconds": supervisor.first_text_seconds,
                }
            )

    task = asyncio.create_task(dispatch())
    output = Path(os.environ.get("AGENT_DESKTOP_REPORT_DIR", str(tmp_path / "evidence")))
    output.mkdir(parents=True, exist_ok=True)
    config_file = tmp_path / "owned-desktop-session.json"
    caplog.set_level("INFO")
    try:
        with _serve_git_app(application) as address:
            config_file.write_text(
                json.dumps(
                    {
                        "api": address + "/api/v1",
                        "project": c.project,
                        "user": c.user,
                        "token": token,
                        "model": "deterministic adapter; real Pi find/read",
                        "output": str(output),
                        "prompts": [
                            f"第 {turn} 轮：这个仓库有多少个文件？列出名称，并读取 alpha.md。"
                            for turn in range(1, 4)
                        ],
                        "expected": [FileQuestionModel.expected] * 3,
                    }
                )
            )
            config_file.chmod(0o600)
            proc = await asyncio.create_subprocess_exec(
                str(electron),
                str(runner),
                str(config_file),
                cwd=desktop,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                raise
            assert proc.returncode == 0, (
                stderr.decode()[-4000:],
                (output / "desktop-result.json").read_text()
                if (output / "desktop-result.json").exists()
                else "No report",
            )
            result = json.loads((output / "desktop-result.json").read_text())
            assert result["passed"] and len(result["turns"]) == 3
            assert len({turn["session_id"] for turn in result["turns"]}) == 1
            stop.set()
            await asyncio.wait_for(task, timeout=5)
            assert len(reports) == 3
            assert all(report["max_inflight"] <= 2 for report in reports)
            assert all(report["attempts"] <= 70 for report in reports)
            api = [
                record.agent_api_performance
                for record in caplog.records
                if hasattr(record, "agent_api_performance")
            ]
            # A keepalive must not trigger a 200-ms snapshot/reconnect loop.
            assert len(api) <= 40
            assert sum(row["attempts"] for row in api) <= 90
            assert aggregate.failures == 0 and aggregate.inflight == 0
            assert 1 <= aggregate.max_inflight <= 4
            assert aggregate.attempts <= 310
            assert not any(
                aggregate.operations[operation]
                for operation in (
                    "GET projects",
                    "GET org_members",
                    "GET project_members",
                    "GET access_surfaces",
                )
            )
            (output / "backend-performance.json").write_text(
                json.dumps(
                    {"worker": reports, "api": api, "aggregate": aggregate.report()}, indent=2
                )
            )
    finally:
        config_file.unlink(missing_ok=True)
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        c.repo.client.transport.aggregate = None
