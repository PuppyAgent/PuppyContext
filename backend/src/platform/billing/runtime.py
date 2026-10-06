from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from src.config import settings
from src.exceptions import AppException, ErrorCode
from src.infra.supabase.client import SupabaseClient
from src.platform.billing.gateway import BillingGatewayError, PuppyPayGateway

logger = logging.getLogger(__name__)
T = TypeVar("T")


class RuntimeBillingRun(BaseModel):
    model_config = ConfigDict(extra="ignore")

    run_id: str
    org_id: str
    project_id: str | None = None
    runtime_kind: str
    compute_profile: str = "standard"
    status: str
    idempotency_key: str
    reservation_id: str | None = None
    estimated_units: int = 0
    actual_units: int | None = None
    started_at: datetime | None = None
    heartbeat_at: datetime | None = None
    settled_at: datetime | None = None
    expires_at: datetime | None = None
    attempts: int = 0
    last_error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class RuntimeRunRepository:
    TABLE = "runtime_billing_runs"

    def __init__(self, supabase_client: SupabaseClient | None = None):
        self._client = (supabase_client or SupabaseClient()).get_client()

    def get(self, run_id: str) -> RuntimeBillingRun | None:
        response = (
            self._client.table(self.TABLE).select("*").eq("run_id", run_id).limit(1).execute()
        )
        rows = response.data or []
        return RuntimeBillingRun.model_validate(rows[0]) if rows else None

    def create_or_get(self, values: dict[str, Any]) -> tuple[RuntimeBillingRun, bool]:
        existing = self.get(str(values["run_id"]))
        if existing is not None:
            return existing, False
        try:
            response = self._client.table(self.TABLE).insert(values).execute()
        except Exception:
            existing = self.get(str(values["run_id"]))
            if existing is None:
                raise
            return existing, False
        return RuntimeBillingRun.model_validate(response.data[0]), True

    def update(self, run_id: str, values: dict[str, Any]) -> RuntimeBillingRun:
        response = self._client.table(self.TABLE).update(values).eq("run_id", run_id).execute()
        return RuntimeBillingRun.model_validate(response.data[0])

    def claim_reservation_retry(
        self,
        *,
        run_id: str,
        expected_status: str,
        stale_before: datetime | None = None,
    ) -> RuntimeBillingRun | None:
        query = (
            self._client.table(self.TABLE)
            .update({"status": "pending_reservation", "last_error": None})
            .eq("run_id", run_id)
            .eq("status", expected_status)
            .is_("reservation_id", "null")
        )
        if stale_before is not None:
            query = query.lte("updated_at", stale_before.isoformat())
        response = query.execute()
        rows = response.data or []
        return RuntimeBillingRun.model_validate(rows[0]) if rows else None

    def claim_action(
        self,
        *,
        run_id: str,
        expected_status: str,
        expected_updated_at: datetime | None,
        metadata: dict[str, Any],
    ) -> RuntimeBillingRun | None:
        """Atomically claim one settlement/cancellation attempt.

        `updated_at` is the fencing token when retrying a row that is already
        in `settling`; the database trigger advances it on the winning update.
        """

        query = (
            self._client.table(self.TABLE)
            .update({"status": "settling", "metadata": metadata})
            .eq("run_id", run_id)
            .eq("status", expected_status)
        )
        if expected_updated_at is not None:
            query = query.eq("updated_at", expected_updated_at.isoformat())
        response = query.execute()
        rows = response.data or []
        return RuntimeBillingRun.model_validate(rows[0]) if rows else None

    def recoverable(
        self,
        *,
        retry_before: datetime,
        limit: int = 50,
    ) -> list[RuntimeBillingRun]:
        bounded_limit = max(1, min(limit, 200))
        retry_response = (
            self._client.table(self.TABLE)
            .select("*")
            .in_("status", ["settling", "failed"])
            .lte("updated_at", retry_before.isoformat())
            .order("updated_at")
            .limit(bounded_limit)
            .execute()
        )
        rows = list(retry_response.data or [])
        remaining = bounded_limit - len(rows)
        if remaining > 0:
            # A live run is never recovered merely because the worker can see it.
            # Only reservations whose heartbeat lease has actually expired are
            # safe to settle after a process crash.
            expired_response = (
                self._client.table(self.TABLE)
                .select("*")
                .in_("status", ["reserved", "running"])
                .lte("expires_at", datetime.now(UTC).isoformat())
                .order("updated_at")
                .limit(remaining)
                .execute()
            )
            rows.extend(expired_response.data or [])
        return [RuntimeBillingRun.model_validate(row) for row in rows]


def _source_kind(source: str) -> str:
    if source in {"chat_agent", "schedule_agent", "automation"}:
        return "automation"
    if source == "workspace":
        return "workspace"
    if source == "connector":
        return "connector"
    return "sandbox"


def guard_unmetered_hosted_runtime(surface: str) -> None:
    """Prevent paid compute from bypassing reservation/settlement.

    A surface may stay available while metering is disabled or shadowed during
    rollout. Once Hosted switches to required enforcement, every compute path
    must have a durable run identifier and reservation integration; an
    unintegrated path therefore fails closed before provider work starts.
    """

    mode = settings.RUNTIME_METERING_MODE
    if mode == "disabled":
        return
    if mode == "shadow":
        logger.warning("runtime_metering_shadow_unintegrated_surface", extra={"surface": surface})
        return
    raise AppException(
        code=ErrorCode.INTERNAL_SERVER_ERROR,
        status_code=503,
        message="Hosted runtime metering is not available for this operation",
        details={
            "code": "runtime_metering_integration_missing",
            "surface": surface,
        },
    )


class RuntimeMeteringService:
    def __init__(
        self,
        *,
        gateway: PuppyPayGateway | None = None,
        repository: RuntimeRunRepository | None = None,
    ) -> None:
        self._gateway = gateway or PuppyPayGateway()
        self._repository = repository
        self._heartbeat_tasks: dict[str, asyncio.Task[None]] = {}

    @property
    def _repo(self) -> RuntimeRunRepository:
        if self._repository is None:
            self._repository = RuntimeRunRepository()
        return self._repository

    async def execute(
        self,
        *,
        audit_context: dict[str, Any],
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        if settings.RUNTIME_METERING_MODE == "disabled":
            return await operation()
        org_id = str(audit_context.get("org_id") or "")
        project_id = str(audit_context.get("project_id") or "") or None
        if not org_id and project_id:
            from src.platform.project.repository import ProjectRepositorySupabase

            project = await asyncio.to_thread(ProjectRepositorySupabase().get_by_id, project_id)
            org_id = str(project.org_id if project is not None else "")
        run_id = str(audit_context.get("run_id") or "")
        if not run_id:
            if settings.RUNTIME_METERING_MODE == "required":
                raise AppException(
                    code=ErrorCode.INTERNAL_SERVER_ERROR,
                    status_code=503,
                    message="Hosted runtime requires a stable billing run identifier",
                    details={"code": "runtime_billing_run_id_missing"},
                )
            run_id = (
                f"{audit_context.get('source', 'sandbox')}:"
                f"{audit_context.get('session_id', 'session')}:{time.time_ns()}"
            )
            logger.warning("runtime_metering_shadow_generated_run_id", extra={"run_id": run_id})
        if not org_id:
            if settings.RUNTIME_METERING_MODE == "required":
                raise AppException(
                    code=ErrorCode.INTERNAL_SERVER_ERROR,
                    status_code=503,
                    message="Hosted runtime is missing organization billing context",
                    details={"code": "runtime_billing_context_missing"},
                )
            logger.warning("runtime_metering_shadow_missing_org", extra={"run_id": run_id})
            return await operation()

        run = await self._reserve(
            run_id=run_id,
            org_id=org_id,
            project_id=project_id,
            actor_id=audit_context.get("user_id"),
            source=_source_kind(str(audit_context.get("source") or "sandbox")),
            maximum_units=int(
                audit_context.get("maximum_runtime_units")
                or settings.RUNTIME_DEFAULT_RESERVATION_UNITS
            ),
        )
        if run is None:
            return await operation()

        started = time.monotonic()
        await asyncio.to_thread(
            self._repo.update,
            run_id,
            {"status": "running", "started_at": datetime.now(UTC).isoformat()},
        )
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(run_id, run.reservation_id))
        try:
            maximum_seconds = int(audit_context.get("maximum_runtime_seconds") or 0)
            if maximum_seconds > 0:
                async with asyncio.timeout(maximum_seconds):
                    return await operation()
            return await operation()
        finally:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task
            runtime_seconds = max(0, int(time.monotonic() - started))
            try:
                await asyncio.shield(self._settle(run_id, runtime_seconds))
            except Exception:
                # Do not replace the runtime operation's result with an
                # accounting transport/database failure. The durable running
                # row and PuppyPay reservation lease are recovered later.
                logger.exception("runtime_settlement_unexpected_failure", extra={"run_id": run_id})

    async def start_session(self, *, audit_context: dict[str, Any]) -> str | None:
        """Reserve before creating a provider resource and meter its full lifetime."""

        if settings.RUNTIME_METERING_MODE == "disabled":
            return None
        org_id = str(audit_context.get("org_id") or "")
        project_id = str(audit_context.get("project_id") or "") or None
        if not org_id and project_id:
            from src.platform.project.repository import ProjectRepositorySupabase

            project = await asyncio.to_thread(ProjectRepositorySupabase().get_by_id, project_id)
            org_id = str(project.org_id if project is not None else "")
        run_id = str(audit_context.get("run_id") or "")
        if not run_id:
            raise ValueError("session Runtime billing requires a stable run_id")
        if not org_id:
            if settings.RUNTIME_METERING_MODE == "required":
                raise AppException(
                    code=ErrorCode.INTERNAL_SERVER_ERROR,
                    status_code=503,
                    message="Hosted runtime is missing organization billing context",
                    details={"code": "runtime_billing_context_missing"},
                )
            logger.warning("runtime_metering_shadow_missing_org", extra={"run_id": run_id})
            return None

        run = await self._reserve(
            run_id=run_id,
            org_id=org_id,
            project_id=project_id,
            actor_id=audit_context.get("user_id"),
            source=_source_kind(str(audit_context.get("source") or "sandbox")),
            maximum_units=int(
                audit_context.get("maximum_runtime_units")
                or settings.RUNTIME_DEFAULT_RESERVATION_UNITS
            ),
        )
        if run is None:
            return None
        if run.status != "running":
            run = await asyncio.to_thread(
                self._repo.update,
                run_id,
                {"status": "running", "started_at": datetime.now(UTC).isoformat()},
            )
        existing = self._heartbeat_tasks.pop(run_id, None)
        if existing is not None:
            existing.cancel()
        self._heartbeat_tasks[run_id] = asyncio.create_task(
            self._heartbeat_loop(run_id, run.reservation_id)
        )
        return run_id

    async def resume_session(self, run_id: str, *, project_id: str, actor_id: str) -> str | None:
        """Reattach an independently fenced execution to its existing meter.

        This does not reserve again or reset started_at. The caller must own
        the Agent run lease; expired/settled reservations fail closed.
        """
        if settings.RUNTIME_METERING_MODE == "disabled":
            return None
        run = await asyncio.to_thread(self._repo.get, run_id)
        if (
            run is None
            or run.project_id != project_id
            or run.metadata.get("actor_id") != actor_id
            or run.status != "running"
            or not run.reservation_id
        ):
            raise ValueError("Runtime reservation cannot be resumed")
        response = await self._gateway.request(
            "POST",
            f"/internal/v1/billing/runtime/reservations/{run.reservation_id}/heartbeat",
            body={"extend_seconds": min(3600, settings.RUNTIME_BILLING_HEARTBEAT_SECONDS * 2)},
        )
        await asyncio.to_thread(
            self._repo.update,
            run_id,
            {
                "heartbeat_at": datetime.now(UTC).isoformat(),
                "expires_at": response["expires_at"],
            },
        )
        await self._stop_heartbeat(run_id)
        self._heartbeat_tasks[run_id] = asyncio.create_task(
            self._heartbeat_loop(run_id, run.reservation_id)
        )
        return run_id

    async def check_session(self, run_id: str) -> None:
        """Fail closed when a durable runtime reservation is no longer live."""
        if settings.RUNTIME_METERING_MODE == "disabled":
            return
        run = await asyncio.to_thread(self._repo.get, run_id)
        if (run is None or run.status != "running" or not run.expires_at
                or run.expires_at <= datetime.now(UTC)):
            raise ValueError("Runtime reservation expired or was exhausted")

    async def detach_session(self, run_id: str) -> None:
        """A fenced owner stops its local heartbeat without settling the run."""
        await self._stop_heartbeat(run_id)

    async def finish_session(self, run_id: str) -> None:
        """Settle a successfully started provider resource exactly once."""

        await self._stop_heartbeat(run_id)
        run = await asyncio.to_thread(self._repo.get, run_id)
        if run is None or run.status in {"settled", "canceled"}:
            return
        runtime_seconds = 0
        if run.started_at:
            started = run.started_at
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            runtime_seconds = max(0, int((datetime.now(UTC) - started).total_seconds()))
        await self._settle(run_id, runtime_seconds)

    async def cancel_session(self, run_id: str) -> None:
        """Compensate a reservation when the provider never started."""

        await self._stop_heartbeat(run_id)
        await self._cancel(run_id)

    async def _stop_heartbeat(self, run_id: str) -> None:
        task = self._heartbeat_tasks.pop(run_id, None)
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _reserve(
        self,
        *,
        run_id: str,
        org_id: str,
        project_id: str | None,
        actor_id: str | None,
        source: str,
        maximum_units: int,
    ) -> RuntimeBillingRun | None:
        idempotency_key = f"runtime-reserve:{run_id}"
        run, created = await asyncio.to_thread(
            self._repo.create_or_get,
            {
                "run_id": run_id,
                "org_id": org_id,
                "project_id": project_id,
                "runtime_kind": source,
                "compute_profile": "standard",
                "status": "pending_reservation",
                "idempotency_key": idempotency_key,
                "estimated_units": maximum_units,
                "metadata": {"schema_version": "1.0", "actor_id": actor_id},
            },
        )
        expected_facts = (org_id, project_id, source, maximum_units)
        actual_facts = (
            run.org_id,
            run.project_id,
            run.runtime_kind,
            run.estimated_units,
        )
        if actual_facts != expected_facts:
            raise AppException(
                code=ErrorCode.BAD_REQUEST,
                status_code=409,
                message="Runtime run identifier conflicts with an existing billing run",
                details={"code": "runtime_run_identity_conflict", "run_id": run_id},
            )
        if not created:
            claim_status: str | None = None
            stale_before: datetime | None = None
            claimed: RuntimeBillingRun | None = None
            if run.status == "reservation_failed" and run.reservation_id is None:
                claim_status = "reservation_failed"
            elif run.status == "pending_reservation" and run.reservation_id is None:
                stale_before = datetime.now(UTC) - timedelta(
                    seconds=settings.RUNTIME_RESERVATION_CLAIM_SECONDS
                )
                updated_at = run.updated_at
                if updated_at is not None:
                    if updated_at.tzinfo is None:
                        updated_at = updated_at.replace(tzinfo=UTC)
                    if updated_at <= stale_before:
                        claim_status = "pending_reservation"
            if claim_status is not None:
                claimed = await asyncio.to_thread(
                    self._repo.claim_reservation_retry,
                    run_id=run_id,
                    expected_status=claim_status,
                    stale_before=stale_before,
                )
                if claimed is not None:
                    run = claimed
                else:
                    run = await asyncio.to_thread(self._repo.get, run_id) or run
            if claim_status is None or claimed is None:
                code = (
                    "runtime_run_already_terminal"
                    if run.status in {"settled", "canceled", "denied", "expired", "unmetered"}
                    else "runtime_run_in_progress"
                )
                raise AppException(
                    code=ErrorCode.BAD_REQUEST,
                    status_code=409,
                    message="Runtime run identifier is already in use",
                    details={"code": code, "run_id": run_id, "status": run.status},
                )
        try:
            response = await self._gateway.request(
                "POST",
                "/internal/v1/billing/runtime/reservations",
                idempotency_key=idempotency_key,
                body={
                    "org_id": org_id,
                    "project_id": project_id,
                    "actor_id": actor_id,
                    "run_id": run_id,
                    "source": source,
                    "compute_profile": "standard",
                    "maximum_units": maximum_units,
                },
            )
        except BillingGatewayError as exc:
            failure_status = (
                "unmetered"
                if settings.RUNTIME_METERING_MODE == "shadow"
                else "denied"
                if exc.status_code < 500
                else "reservation_failed"
            )
            await asyncio.to_thread(
                self._repo.update,
                run_id,
                {
                    "status": failure_status,
                    "attempts": run.attempts + 1,
                    "last_error": _billing_error_code(exc),
                },
            )
            if settings.RUNTIME_METERING_MODE == "required":
                raise _runtime_error(exc) from exc
            logger.warning(
                "runtime_reservation_shadow_failed",
                extra={"run_id": run_id, "reason": _billing_error_code(exc)},
            )
            return None
        return await asyncio.to_thread(
            self._repo.update,
            run_id,
            {
                "status": "reserved",
                "reservation_id": response["reservation_id"],
                "expires_at": response["expires_at"],
                "heartbeat_at": datetime.now(UTC).isoformat(),
                "last_error": None,
            },
        )

    async def _heartbeat_loop(self, run_id: str, reservation_id: str | None) -> None:
        if not reservation_id:
            return
        while True:
            await asyncio.sleep(settings.RUNTIME_BILLING_HEARTBEAT_SECONDS)
            try:
                response = await self._gateway.request(
                    "POST",
                    f"/internal/v1/billing/runtime/reservations/{reservation_id}/heartbeat",
                    body={
                        "extend_seconds": min(
                            3600,
                            settings.RUNTIME_BILLING_HEARTBEAT_SECONDS * 2,
                        )
                    },
                )
                await asyncio.to_thread(
                    self._repo.update,
                    run_id,
                    {
                        "heartbeat_at": datetime.now(UTC).isoformat(),
                        "expires_at": response["expires_at"],
                    },
                )
            except Exception as exc:  # heartbeat failure is repaired/settled by the durable run
                logger.warning(
                    "runtime_heartbeat_failed",
                    extra={"run_id": run_id, "error_type": type(exc).__name__},
                )

    async def _settle(
        self,
        run_id: str,
        runtime_seconds: int,
        *,
        allow_retry: bool = False,
    ) -> bool:
        run = await asyncio.to_thread(self._repo.get, run_id)
        if (
            run is None
            or not run.reservation_id
            or run.status in {"settled", "canceled", "denied", "expired", "unmetered"}
            or run.metadata.get("cancel_requested")
        ):
            return False
        if run.status not in {"reserved", "running", "settling", "failed"}:
            return False
        if run.status in {"settling", "failed"} and not allow_retry:
            return False
        if run.status in {"settling", "failed"} and run.updated_at is None:
            logger.error("runtime_settlement_missing_fencing_timestamp", extra={"run_id": run_id})
            return False
        stable_runtime_seconds = int(run.metadata.get("runtime_seconds", runtime_seconds))
        occurred_at = str(
            run.metadata.get("settlement_occurred_at") or datetime.now(UTC).isoformat()
        )
        metadata = {
            **run.metadata,
            "runtime_seconds": stable_runtime_seconds,
            "settlement_occurred_at": occurred_at,
        }
        claimed = await asyncio.to_thread(
            self._repo.claim_action,
            run_id=run_id,
            expected_status=run.status,
            expected_updated_at=run.updated_at,
            metadata=metadata,
        )
        if claimed is None:
            return False
        run = claimed
        try:
            response = await self._gateway.request(
                "POST",
                f"/internal/v1/billing/runtime/reservations/{run.reservation_id}/settle",
                idempotency_key=f"runtime-settle:{run_id}",
                body={
                    "runtime_seconds": stable_runtime_seconds,
                    "occurred_at": occurred_at,
                },
            )
        except BillingGatewayError as exc:
            await asyncio.to_thread(
                self._repo.update,
                run_id,
                {
                    "status": "failed",
                    "attempts": run.attempts + 1,
                    "last_error": _billing_error_code(exc),
                    "metadata": metadata,
                },
            )
            logger.error(
                "runtime_settlement_deferred",
                extra={"run_id": run_id, "reason": _billing_error_code(exc)},
            )
            return True
        await asyncio.to_thread(
            self._repo.update,
            run_id,
            {
                "status": "settled",
                "actual_units": int(response["runtime_units"]),
                "settled_at": datetime.now(UTC).isoformat(),
                "last_error": None,
                "metadata": metadata,
            },
        )
        return True

    async def _cancel(self, run_id: str, *, allow_retry: bool = False) -> bool:
        run = await asyncio.to_thread(self._repo.get, run_id)
        if (
            run is None
            or not run.reservation_id
            or run.status in {"settled", "canceled", "denied", "expired", "unmetered"}
        ):
            return False
        cancel_requested = bool(run.metadata.get("cancel_requested"))
        if run.status in {"settling", "failed"}:
            if not allow_retry or not cancel_requested:
                return False
            if run.updated_at is None:
                logger.error(
                    "runtime_cancellation_missing_fencing_timestamp",
                    extra={"run_id": run_id},
                )
                return False
        elif run.status not in {"reserved", "running"}:
            return False
        metadata = {**run.metadata, "cancel_requested": True}
        claimed = await asyncio.to_thread(
            self._repo.claim_action,
            run_id=run_id,
            expected_status=run.status,
            expected_updated_at=run.updated_at,
            metadata=metadata,
        )
        if claimed is None:
            return False
        run = claimed
        try:
            await self._gateway.request(
                "POST",
                f"/internal/v1/billing/runtime/reservations/{run.reservation_id}/cancel",
                idempotency_key=f"runtime-cancel:{run_id}",
            )
        except BillingGatewayError as exc:
            await asyncio.to_thread(
                self._repo.update,
                run_id,
                {
                    "status": "failed",
                    "attempts": run.attempts + 1,
                    "last_error": _billing_error_code(exc),
                    "metadata": metadata,
                },
            )
            logger.error(
                "runtime_cancellation_deferred",
                extra={"run_id": run_id, "reason": _billing_error_code(exc)},
            )
            return True
        await asyncio.to_thread(
            self._repo.update,
            run_id,
            {
                "status": "canceled",
                "actual_units": 0,
                "settled_at": datetime.now(UTC).isoformat(),
                "last_error": None,
                "metadata": metadata,
            },
        )
        return True

    async def recover_once(self) -> int:
        recovered = 0
        retry_before = datetime.now(UTC) - timedelta(
            seconds=settings.RUNTIME_BILLING_RECOVERY_RETRY_SECONDS
        )
        runs = await asyncio.to_thread(
            self._repo.recoverable,
            retry_before=retry_before,
            limit=50,
        )
        for run in runs:
            try:
                if not run.reservation_id:
                    continue
                if run.metadata.get("cancel_requested"):
                    if await self._cancel(run.run_id, allow_retry=True):
                        recovered += 1
                    continue
                runtime_seconds = int(run.metadata.get("runtime_seconds") or 0)
                if runtime_seconds <= 0 and run.started_at:
                    started = run.started_at
                    if started.tzinfo is None:
                        started = started.replace(tzinfo=UTC)
                    runtime_seconds = max(
                        0,
                        int((datetime.now(UTC) - started).total_seconds()),
                    )
                if await self._settle(run.run_id, runtime_seconds, allow_retry=True):
                    recovered += 1
            except Exception:
                # One malformed or temporarily unavailable reservation must
                # not starve the remainder of the recovery batch.
                logger.exception("runtime_recovery_item_failed", extra={"run_id": run.run_id})
        return recovered


def _billing_error_code(error: BillingGatewayError) -> str:
    payload = error.payload.get("error") if isinstance(error.payload, dict) else None
    return str(payload.get("code") if isinstance(payload, dict) else "runtime_billing_failed")


def _runtime_error(error: BillingGatewayError) -> AppException:
    payload = error.payload.get("error") if isinstance(error.payload, dict) else None
    payload = payload if isinstance(payload, dict) else {}
    return AppException(
        code=ErrorCode.FORBIDDEN if error.status_code < 500 else ErrorCode.INTERNAL_SERVER_ERROR,
        status_code=error.status_code,
        message=str(payload.get("message") or "Runtime reservation was denied"),
        details={
            "code": str(payload.get("code") or "runtime_reservation_denied"),
            "retryable": bool(payload.get("retryable", error.status_code >= 500)),
        },
    )


_runtime_metering_service: RuntimeMeteringService | None = None


def get_runtime_metering_service() -> RuntimeMeteringService:
    global _runtime_metering_service
    if _runtime_metering_service is None:
        _runtime_metering_service = RuntimeMeteringService()
    return _runtime_metering_service
