"""Upload-job reaper: fail `running` upload jobs orphaned by a dead API process.

A live finalize cannot outlast the HTTP request, so the stale window sits well
above any request lifetime — only dead-process orphans are reaped, and the write
is guarded on status='running' so a job that finalizes mid-scan is never clobbered.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from src.platform.upload.repository import UploadJobRepository, _parse_dt

UTC = timezone.utc


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class _Table:
    def __init__(self, rows: list[dict]):
        self.rows = rows
        self._op = "select"
        self._patch: dict | None = None
        self._eqs: dict = {}

    def select(self, *_a, **_k):
        self._op = "select"; return self

    def update(self, patch):
        self._op = "update"; self._patch = patch; return self

    def eq(self, col, val):
        self._eqs[col] = val; return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def execute(self):
        match = [r for r in self.rows if all(r.get(c) == v for c, v in self._eqs.items())]
        if self._op == "select":
            return SimpleNamespace(data=[dict(r) for r in match])
        out = []
        for r in match:
            r.update(self._patch)
            out.append(dict(r))
        return SimpleNamespace(data=out)


class _Client:
    def __init__(self, rows): self._rows = rows
    def table(self, _name): return _Table(self._rows)


class _SB:
    def __init__(self, rows): self.client = _Client(rows)


def _row(jid: str, status: str, started_delta: timedelta) -> dict:
    now = datetime.now(UTC)
    return {"id": jid, "status": status,
            "started_at": _iso(now - started_delta),
            "created_at": _iso(now - started_delta)}


def test_recover_reaps_only_stale_running_jobs():
    rows = [
        _row("stale", "running", timedelta(hours=2)),
        _row("fresh", "running", timedelta(minutes=5)),
        _row("done", "completed", timedelta(hours=3)),
    ]
    repo = UploadJobRepository(_SB(rows))
    recovered = repo.recover_stale_jobs(stale_seconds=3600, limit=100)
    assert recovered == ["stale"]
    by_id = {r["id"]: r for r in rows}
    assert by_id["stale"]["status"] == "failed"     # dead-process orphan reaped
    assert by_id["fresh"]["status"] == "running"    # live finalize untouched
    assert by_id["done"]["status"] == "completed"   # terminal not selected


def test_parse_dt_variants():
    assert _parse_dt(None) is None
    assert _parse_dt("bad") is None
    assert _parse_dt("2026-06-20T00:00:00Z").tzinfo is not None
