"""Actual ASGI/service regressions: sync waits must not starve the event loop."""

from __future__ import annotations

import asyncio
import threading
from contextvars import ContextVar
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from src.exceptions import NotFoundException
from src.platform.activity.dependencies import get_activity_service
from src.platform.activity.public_router import router as activity_router
from src.platform.auth.dependencies import get_current_user
from src.version_engine.entrypoints.http.content_history import get_commit_content
from src.version_engine.read.admin import VersionAdminService
from tests.authorization_fakes import authorization_for


@pytest.mark.asyncio
async def test_slow_activity_read_does_not_starve_other_asgi_requests():
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    class Service:
        def list_for_project(self, *_args, **_kwargs):
            entered.set()
            release.wait(2)  # bounded safety net, not the assertion clock
            finished.set()
            return []

    app = FastAPI()
    app.include_router(activity_router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(user_id="user-1")
    app.dependency_overrides[get_activity_service] = Service

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        pending = asyncio.create_task(client.get("/activity/items?project_id=p1"))
        try:
            while not entered.is_set():
                await asyncio.sleep(0.001)
            response = await client.get("/ping")
            assert response.status_code == 200
            assert not finished.is_set(), "sync activity I/O blocked the ASGI event loop"
        finally:
            release.set()
            response = await pending
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_history_read_runs_off_loop_and_preserves_request_context():
    request_marker = ContextVar("read-test-marker", default="missing")
    request_marker.set("request-123")
    loop_thread = threading.get_ident()

    def read(_project, _callback):
        assert threading.get_ident() != loop_thread, "history query ran on event loop"
        assert request_marker.get() == "request-123"
        return []

    admin = VersionAdminService(object(), grant=object())
    admin._read = read
    assert await admin.get_commit_history("p1") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [True, False])
async def test_history_authorization_runs_off_loop_and_still_guards_content(allowed):
    loop_thread = threading.get_ident()
    authorization = authorization_for(*(["p1"] if allowed else []))
    original = authorization._repository.load_project_facts

    def load_facts(project_id, user_id):
        assert threading.get_ident() != loop_thread, "authorization queried on event loop"
        return original(project_id, user_id)

    authorization._repository.load_project_facts = load_facts
    admin = SimpleNamespace(get_commit_content=AsyncMock(return_value=b"hello"))

    def bind(grant):
        assert grant.project_id == "p1"
        return admin

    admin.for_grant = bind
    call = get_commit_content(
        project_id="p1",
        path="note.md",
        commit_id="a" * 40,
        version_admin=admin,
        authorization=authorization,
        current_user=SimpleNamespace(user_id="user-1"),
    )
    if allowed:
        result = await call
        assert result.data["content_text"] == "hello"
        admin.get_commit_content.assert_awaited_once()
    else:
        with pytest.raises(NotFoundException):
            await call
        admin.get_commit_content.assert_not_awaited()
