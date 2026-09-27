"""GitHub webhook now ENQUEUES the import onto the worker queue instead of
running it in-process. Pins the enqueue/skip/reject decisions + the job wrapper.
"""
from __future__ import annotations

import hmac
from hashlib import sha256
from types import SimpleNamespace

import pytest

from src.platform.synchronize.github import jobs as gh_jobs
from src.platform.synchronize.github import webhook as wh

DEP = "src.platform.synchronize.github.arq_client.get_github_sync_arq_client"


def _sig(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, sha256).hexdigest()


class _FakeArq:
    def __init__(self):
        self.calls: list[dict] = []

    async def enqueue_pull(self, integration_id, *, branch=None,
                                    force=False, triggered_by="webhook", dedup_key=None):
        self.calls.append({"integration_id": integration_id, "branch": branch,
                           "force": force, "triggered_by": triggered_by, "dedup_key": dedup_key})
        return "job-1"


def _integration(**over):
    base = {"id": "int-1", "webhook_secret": "s3cr3t", "default_branch": "main",
            "auto_import": True, "last_imported_sha": "old"}
    base.update(over)
    return base


async def test_dispatch_enqueues_on_valid_push(monkeypatch):
    fake = _FakeArq()
    monkeypatch.setattr(DEP, lambda: fake)
    body = b'{"x":1}'
    headers = {"x-hub-signature-256": _sig("s3cr3t", body)}
    res = await wh._maybe_dispatch(_integration(), "main", "newsha", body, headers)
    assert res["status"] == "queued" and res["worker_job_id"] == "job-1"
    assert fake.calls == [{"integration_id": "int-1", "branch": "main", "force": False,
                           "triggered_by": "webhook", "dedup_key": "gh-import:int-1:newsha"}]


async def test_dispatch_dedup_returns_already_queued(monkeypatch):
    class _Dedup(_FakeArq):
        async def enqueue_pull(self, *a, **k):
            return None
    monkeypatch.setattr(DEP, lambda: _Dedup())
    body = b"{}"
    res = await wh._maybe_dispatch(_integration(), "main", "sha", body,
                                   {"x-hub-signature-256": _sig("s3cr3t", body)})
    assert res["status"] == "queued" and res["reason"] == "already_queued"


async def test_dispatch_skips_without_enqueue(monkeypatch):
    fake = _FakeArq()
    monkeypatch.setattr(DEP, lambda: fake)
    body = b"{}"
    hdr = {"x-hub-signature-256": _sig("s3cr3t", body)}
    r = await wh._maybe_dispatch(_integration(), "feature", "sha", body, hdr)
    assert r["status"] == "skipped" and "branch_mismatch" in r["reason"]
    r = await wh._maybe_dispatch(_integration(auto_import=False), "main", "sha", body, hdr)
    assert r["status"] == "skipped" and r["reason"] == "auto_import_disabled"
    r = await wh._maybe_dispatch(_integration(last_imported_sha="sha"), "main", "sha", body, hdr)
    assert r["status"] == "skipped" and r["reason"] == "already_imported"
    r = await wh._maybe_dispatch(_integration(webhook_secret=None), "main", "sha", body, hdr)
    assert r["status"] == "skipped" and r["reason"] == "no_webhook_secret"
    assert fake.calls == []  # none of these reach the worker


async def test_dispatch_rejects_bad_signature(monkeypatch):
    monkeypatch.setattr(DEP, lambda: _FakeArq())
    with pytest.raises(wh.WebhookRejection) as ei:
        await wh._maybe_dispatch(_integration(), "main", "sha", b"{}",
                                 {"x-hub-signature-256": "sha256=bad"})
    assert ei.value.status == 401


async def test_execute_github_import_runs_branch(monkeypatch):
    seen = {}

    class _Repo:
        async def get_by_id(self, iid):
            seen["fetched"] = iid
            return {"id": iid}

    async def _imp(integration, *, branch, force, triggered_by):
        seen["import"] = {"branch": branch, "force": force, "triggered_by": triggered_by}
        return SimpleNamespace(status="success", git_sha="abc123")

    monkeypatch.setattr(gh_jobs, "GithubSyncRepository", lambda: _Repo())
    monkeypatch.setattr(gh_jobs, "import_branch", _imp)
    out = await gh_jobs.execute_github_import({}, "int-9", branch="main",
                                              force=False, triggered_by="webhook")
    assert out["status"] == "success" and out["integration_id"] == "int-9"
    assert seen["fetched"] == "int-9" and seen["import"]["branch"] == "main"


async def test_execute_github_import_missing_integration(monkeypatch):
    class _Repo:
        async def get_by_id(self, iid):
            return None
    monkeypatch.setattr(gh_jobs, "GithubSyncRepository", lambda: _Repo())
    out = await gh_jobs.execute_github_import({}, "missing")
    assert out["status"] == "skipped" and out["reason"] == "integration_not_found"
