"""CRUD for Connect execution history stored in ``sync_runs``."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, List

from src.infra.supabase.client import SupabaseClient


NEW_TABLE = "sync_runs"
VALID_TRIGGER_TYPES = {"manual", "scheduled", "webhook", "realtime", "initial", "push"}
ACTIVE_RUN_STATUSES = ("queued", "running")
TERMINAL_RUN_STATUSES = ("completed", "failed", "cancelled", "skipped", "conflict")
DEFAULT_RUN_LEASE_SECONDS = 30 * 60


@dataclass
class SyncRun:
    id: str
    connection_id: str
    status: str = "running"
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_ms: Optional[int] = None
    exit_code: Optional[int] = None
    stdout: Optional[str] = None
    error: Optional[str] = None
    trigger_type: str = "manual"
    result_summary: Optional[str] = None
    worker_job_id: Optional[str] = None
    heartbeat_at: Optional[str] = None
    lease_expires_at: Optional[str] = None
    created_at: Optional[str] = None
    table_name: str = NEW_TABLE


def _normalize_trigger_type(trigger_type: str) -> str:
    if trigger_type not in VALID_TRIGGER_TYPES:
        return "manual"
    return trigger_type


def _status_for_new_table(status: str) -> str:
    if status == "success":
        return "completed"
    return status


def _status_for_response(status: str) -> str:
    if status == "completed":
        return "success"
    return status


class SyncRunRepository:
    def __init__(self, supabase_client: SupabaseClient):
        self.client = supabase_client.client

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _lease_expires_at(lease_seconds: int = DEFAULT_RUN_LEASE_SECONDS) -> str:
        return (
            datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
        ).isoformat()

    @staticmethod
    def _parse_ts(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except (TypeError, ValueError):
            return None

    def _connection_row(self, connection_id: str) -> dict | None:
        response = (
            self.client.table("connections")
            .select("*")
            .eq("id", connection_id)
            .limit(1)
            .execute()
        )
        rows = response.data or []
        return rows[0] if rows else None

    def _to_model(self, row: dict) -> SyncRun:
        return SyncRun(
            id=row["id"],
            connection_id=row.get("connection_id", ""),
            status=_status_for_response(row.get("status", "running")),
            started_at=row.get("started_at"),
            finished_at=row.get("finished_at"),
            duration_ms=row.get("duration_ms"),
            exit_code=row.get("exit_code"),
            stdout=row.get("stdout"),
            error=row.get("error_message"),
            trigger_type=row.get("triggered_by", "manual"),
            result_summary=row.get("message"),
            worker_job_id=row.get("worker_job_id"),
            heartbeat_at=row.get("heartbeat_at"),
            lease_expires_at=row.get("lease_expires_at"),
            created_at=row.get("created_at"),
            table_name=NEW_TABLE,
        )

    def create(
        self,
        sync_id: str,
        trigger_type: str = "manual",
        *,
        status: str = "running",
        message: str | None = None,
        worker_job_id: str | None = None,
        lease_seconds: int = DEFAULT_RUN_LEASE_SECONDS,
    ) -> SyncRun:
        connection = self._connection_row(sync_id)
        if not connection:
            raise RuntimeError(f"Connection {sync_id} not found")
        normalized_status = _status_for_new_table(status)
        running = normalized_status == "running"
        data = {
            "connection_id": sync_id,
            "project_id": connection["project_id"],
            "triggered_by": _normalize_trigger_type(trigger_type),
            "direction": connection.get("direction") or "inbound",
            "status": normalized_status,
            "phase": normalized_status,
            "progress": 5 if running else 0,
            "message": message or ("Sync running" if running else "Queued"),
        }
        if normalized_status in ACTIVE_RUN_STATUSES:
            data["lease_expires_at"] = self._lease_expires_at(lease_seconds)
        if running:
            now = self._now()
            data["started_at"] = now
            data["heartbeat_at"] = now
        if worker_job_id is not None:
            data["worker_job_id"] = worker_job_id
        response = self.client.table(NEW_TABLE).insert(data).execute()
        run = self._to_model(response.data[0])
        self.client.table("connections").update({
            "last_sync_run_id": run.id,
        }).eq("id", sync_id).execute()
        return run

    def create_queued(
        self,
        sync_id: str,
        trigger_type: str = "manual",
        *,
        lease_seconds: int = DEFAULT_RUN_LEASE_SECONDS,
    ) -> SyncRun:
        return self.create(
            sync_id,
            trigger_type,
            status="queued",
            message="Queued",
            lease_seconds=lease_seconds,
        )

    def is_stale(
        self,
        run: SyncRun,
        *,
        lease_seconds: int = DEFAULT_RUN_LEASE_SECONDS,
        now: datetime | None = None,
    ) -> bool:
        """Return true when an active run no longer owns its sync lane."""
        normalized = _status_for_new_table(run.status)
        if normalized not in ACTIVE_RUN_STATUSES:
            return False
        now = now or datetime.now(timezone.utc)
        lease_expires_at = self._parse_ts(run.lease_expires_at)
        if lease_expires_at is not None:
            return lease_expires_at <= now
        fallback_ts = self._parse_ts(run.heartbeat_at or run.started_at or run.created_at)
        if fallback_ts is None:
            return False
        return fallback_ts + timedelta(seconds=lease_seconds) <= now

    def get_active_by_sync(self, sync_id: str) -> Optional[SyncRun]:
        rows = (
            self.client.table(NEW_TABLE)
            .select("*")
            .eq("connection_id", sync_id)
            .in_("status", list(ACTIVE_RUN_STATUSES))
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        ).data or []
        return self._to_model(rows[0]) if rows else None

    def get_blocking_active_by_sync(
        self,
        sync_id: str,
        *,
        lease_seconds: int = DEFAULT_RUN_LEASE_SECONDS,
    ) -> Optional[SyncRun]:
        run = self.get_active_by_sync(sync_id)
        if run and self.is_stale(run, lease_seconds=lease_seconds):
            self.mark_stale(run.id)
            return None
        return run

    def create_queued_single_lane(
        self,
        sync_id: str,
        trigger_type: str = "manual",
        *,
        lease_seconds: int = DEFAULT_RUN_LEASE_SECONDS,
    ) -> tuple[SyncRun, bool]:
        """Create a queued run unless this connection already has one active.

        Returns ``(run, created)``. The pre-check gives normal callers a cheap
        idempotent path; the post-insert fallback handles the DB partial-unique
        race when two API/scheduler processes queue the same connection at once.
        """
        existing = self.get_blocking_active_by_sync(
            sync_id,
            lease_seconds=lease_seconds,
        )
        if existing:
            return existing, False
        try:
            return self.create_queued(
                sync_id,
                trigger_type,
                lease_seconds=lease_seconds,
            ), True
        except Exception:
            existing = self.get_blocking_active_by_sync(
                sync_id,
                lease_seconds=lease_seconds,
            )
            if existing:
                return existing, False
            raise

    def set_worker_job_id(self, run_id: str, worker_job_id: str) -> None:
        self.client.table(NEW_TABLE).update({
            "worker_job_id": worker_job_id,
        }).eq("id", run_id).execute()

    def claim_running(
        self,
        run_id: str,
        *,
        message: str = "Sync running",
        lease_seconds: int = DEFAULT_RUN_LEASE_SECONDS,
    ) -> Optional[SyncRun]:
        now = self._now()
        response = (
            self.client.table(NEW_TABLE)
            .update({
                "status": "running",
                "phase": "running",
                "progress": 5,
                "message": message,
                "started_at": now,
                "heartbeat_at": now,
                "lease_expires_at": self._lease_expires_at(lease_seconds),
            })
            .eq("id", run_id)
            .eq("status", "queued")
            .execute()
        )
        rows = response.data or []
        return self._to_model(rows[0]) if rows else None

    def renew_lease(
        self,
        run_id: str,
        *,
        lease_seconds: int = DEFAULT_RUN_LEASE_SECONDS,
    ) -> bool:
        now = self._now()
        response = (
            self.client.table(NEW_TABLE)
            .update({
                "heartbeat_at": now,
                "lease_expires_at": self._lease_expires_at(lease_seconds),
            })
            .eq("id", run_id)
            .eq("status", "running")
            .execute()
        )
        return bool(response.data)

    def mark_running(
        self,
        run_id: str,
        *,
        message: str = "Sync running",
        lease_seconds: int = DEFAULT_RUN_LEASE_SECONDS,
    ) -> Optional[SyncRun]:
        now = self._now()
        (
            self.client.table(NEW_TABLE)
            .update({
                "status": "running",
                "phase": "running",
                "progress": 5,
                "message": message,
                "started_at": now,
                "heartbeat_at": now,
                "lease_expires_at": self._lease_expires_at(lease_seconds),
            })
            .eq("id", run_id)
            .in_("status", list(ACTIVE_RUN_STATUSES))
            .execute()
        )
        return self.get_by_id(run_id)

    def mark_stale(self, run_id: str) -> Optional[SyncRun]:
        now = self._now()
        response = (
            self.client.table(NEW_TABLE)
            .update({
                "status": "failed",
                "phase": "failed",
                "progress": 100,
                "message": "Sync run lease expired",
                "error_message": "Sync run lease expired before completion",
                "finished_at": now,
                "lease_expires_at": None,
            })
            .eq("id", run_id)
            .in_("status", list(ACTIVE_RUN_STATUSES))
            .execute()
        )
        rows = response.data or []
        return self._to_model(rows[0]) if rows else None

    def recover_stale_active_runs(
        self,
        *,
        lease_seconds: int = DEFAULT_RUN_LEASE_SECONDS,
        limit: int = 100,
    ) -> list[SyncRun]:
        rows = (
            self.client.table(NEW_TABLE)
            .select("*")
            .in_("status", list(ACTIVE_RUN_STATUSES))
            .order("created_at", desc=False)
            .limit(limit)
            .execute()
        ).data or []
        recovered: list[SyncRun] = []
        for row in rows:
            run = self._to_model(row)
            if self.is_stale(run, lease_seconds=lease_seconds):
                stale = self.mark_stale(run.id)
                if stale:
                    recovered.append(stale)
        return recovered

    def complete(
        self,
        run_id: str,
        *,
        status: str = "success",
        stdout: Optional[str] = None,
        error: Optional[str] = None,
        exit_code: Optional[int] = None,
        result_summary: Optional[str] = None,
    ) -> None:
        run = self.get_by_id(run_id)
        if not run:
            return
        if _status_for_new_table(run.status) in TERMINAL_RUN_STATUSES:
            return

        now = self._now()
        data: dict[str, Any] = {
            "status": _status_for_new_table(status),
            "phase": _status_for_new_table(status),
            "progress": 100,
            "finished_at": now,
            "lease_expires_at": None,
        }
        if error is not None:
            data["error_message"] = error[:10_000]
        if result_summary is not None:
            data["message"] = result_summary[:1000]
        if stdout is not None:
            data["stdout"] = stdout[:100_000]
        if exit_code is not None:
            data["exit_code"] = exit_code
        if run.started_at:
            try:
                started = datetime.fromisoformat(run.started_at.replace("Z", "+00:00"))
                finished = datetime.fromisoformat(now)
                data["duration_ms"] = int((finished - started).total_seconds() * 1000)
            except (ValueError, TypeError):
                pass
        self.client.table(NEW_TABLE).update(data).eq("id", run_id).execute()

    def get_by_id(self, run_id: str) -> Optional[SyncRun]:
        response = (
            self.client.table(NEW_TABLE)
            .select("*")
            .eq("id", run_id)
            .limit(1)
            .execute()
        )
        return self._to_model(response.data[0]) if response.data else None

    def list_by_sync(
        self, sync_id: str, limit: int = 20, offset: int = 0,
    ) -> List[SyncRun]:
        rows = (
            self.client.table(NEW_TABLE)
            .select("*")
            .eq("connection_id", sync_id)
            .order("started_at", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        ).data or []
        return [self._to_model(row) for row in rows]

    def list_failed_for_connections(
        self,
        connection_ids: List[str],
        limit: int = 50,
    ) -> List[SyncRun]:
        if not connection_ids:
            return []
        rows = (
            self.client.table(NEW_TABLE)
            .select("*")
            .in_("connection_id", connection_ids)
            .eq("status", "failed")
            .order("started_at", desc=True)
            .limit(limit)
            .execute()
        ).data or []
        return [self._to_model(row) for row in rows]

    def count_by_sync(self, sync_id: str) -> int:
        response = (
            self.client.table(NEW_TABLE)
            .select("id", count="exact")
            .eq("connection_id", sync_id)
            .execute()
        )
        return response.count or 0

    def delete_by_sync(self, sync_id: str) -> None:
        self.client.table(NEW_TABLE).delete().eq("connection_id", sync_id).execute()
