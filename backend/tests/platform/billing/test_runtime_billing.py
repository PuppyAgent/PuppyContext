from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from src.config import settings
from src.exceptions import AppException
from src.platform.billing.gateway import BillingGatewayError
from src.platform.billing.runtime import (
    RuntimeBillingRun,
    RuntimeMeteringService,
    guard_unmetered_hosted_runtime,
)


class _Repository:
    def __init__(self) -> None:
        self.rows: dict[str, RuntimeBillingRun] = {}
        self.recovery_rows: list[RuntimeBillingRun] = []

    def get(self, run_id: str):
        return self.rows.get(run_id)

    def create_or_get(self, values):
        run_id = values["run_id"]
        if run_id in self.rows:
            return self.rows[run_id], False
        self.rows[run_id] = RuntimeBillingRun(**values)
        return self.rows[run_id], True

    def update(self, run_id: str, values):
        self.rows[run_id] = RuntimeBillingRun.model_validate(
            {**self.rows[run_id].model_dump(), **values, "updated_at": datetime.now(UTC)}
        )
        return self.rows[run_id]

    def claim_reservation_retry(
        self,
        *,
        run_id: str,
        expected_status: str,
        stale_before: datetime | None = None,
    ):
        run = self.rows[run_id]
        if run.status != expected_status or run.reservation_id is not None:
            return None
        if stale_before is not None and (run.updated_at is None or run.updated_at > stale_before):
            return None
        return self.update(
            run_id,
            {"status": "pending_reservation", "last_error": None},
        )

    def claim_action(
        self,
        *,
        run_id: str,
        expected_status: str,
        expected_updated_at: datetime | None,
        metadata: dict,
    ):
        run = self.rows[run_id]
        if run.status != expected_status:
            return None
        if expected_updated_at is not None and run.updated_at != expected_updated_at:
            return None
        return self.update(run_id, {"status": "settling", "metadata": metadata})

    def recoverable(self, *, retry_before: datetime, limit: int = 50):
        del retry_before
        return self.recovery_rows[:limit]


class _Gateway:
    def __init__(self, reserve_error: BillingGatewayError | None = None) -> None:
        self.reserve_error = reserve_error
        self.calls: list[tuple[str, str, dict]] = []

    async def request(self, method: str, path: str, **kwargs):
        self.calls.append((method, path, kwargs))
        if path.endswith("/runtime/reservations"):
            if self.reserve_error:
                raise self.reserve_error
            return {
                "reservation_id": "reservation-1",
                "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
            }
        if path.endswith("/settle"):
            return {"runtime_units": 2}
        if path.endswith("/cancel"):
            return {"status": "released", "released_units": 5}
        if path.endswith("/heartbeat"):
            return {"expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat()}
        raise AssertionError(path)


def _context() -> dict:
    return {
        "org_id": "org-1",
        "project_id": "project-1",
        "user_id": "user-1",
        "source": "sandbox_endpoint",
        "session_id": "session-1",
        "run_id": "run-1",
        "maximum_runtime_units": 5,
    }


def test_required_mode_fails_closed_for_unintegrated_compute_surface(monkeypatch) -> None:
    monkeypatch.setattr(settings, "RUNTIME_METERING_MODE", "required")

    with pytest.raises(AppException) as caught:
        guard_unmetered_hosted_runtime("ingest.ocr_parse")

    assert caught.value.status_code == 503
    assert caught.value.details == {
        "code": "runtime_metering_integration_missing",
        "surface": "ingest.ocr_parse",
    }


def test_shadow_mode_observes_unintegrated_compute_surface_without_blocking(
    monkeypatch, caplog
) -> None:
    monkeypatch.setattr(settings, "RUNTIME_METERING_MODE", "shadow")

    guard_unmetered_hosted_runtime("remote_workspace.create")

    assert "runtime_metering_shadow_unintegrated_surface" in caplog.text


@pytest.mark.asyncio
async def test_required_mode_rejects_missing_stable_run_id_before_compute(monkeypatch) -> None:
    monkeypatch.setattr(settings, "RUNTIME_METERING_MODE", "required")
    context = _context()
    context.pop("run_id")
    executed = False

    async def operation():
        nonlocal executed
        executed = True

    with pytest.raises(AppException) as caught:
        await RuntimeMeteringService(gateway=_Gateway(), repository=_Repository()).execute(
            audit_context=context,
            operation=operation,
        )

    assert caught.value.status_code == 503
    assert caught.value.details["code"] == "runtime_billing_run_id_missing"
    assert executed is False


@pytest.mark.asyncio
async def test_required_reservation_denial_prevents_compute(monkeypatch) -> None:
    monkeypatch.setattr(settings, "RUNTIME_METERING_MODE", "required")
    repository = _Repository()
    gateway = _Gateway(
        BillingGatewayError(
            402,
            {"error": {"code": "runtime_credit_insufficient", "message": "No credit"}},
        )
    )
    executed = False

    async def operation():
        nonlocal executed
        executed = True

    with pytest.raises(AppException) as caught:
        await RuntimeMeteringService(gateway=gateway, repository=repository).execute(
            audit_context=_context(),
            operation=operation,
        )

    assert not executed
    assert caught.value.status_code == 402
    assert repository.rows["run-1"].status == "denied"


@pytest.mark.asyncio
async def test_success_reserves_then_settles_once(monkeypatch) -> None:
    monkeypatch.setattr(settings, "RUNTIME_METERING_MODE", "required")
    repository = _Repository()
    gateway = _Gateway()

    async def operation():
        return "done"

    result = await RuntimeMeteringService(gateway=gateway, repository=repository).execute(
        audit_context=_context(),
        operation=operation,
    )

    assert result == "done"
    assert [path for _, path, _ in gateway.calls] == [
        "/internal/v1/billing/runtime/reservations",
        "/internal/v1/billing/runtime/reservations/reservation-1/settle",
    ]
    assert repository.rows["run-1"].status == "settled"
    assert repository.rows["run-1"].actual_units == 2


@pytest.mark.asyncio
async def test_run_id_cannot_be_reused_for_different_billing_facts(monkeypatch) -> None:
    monkeypatch.setattr(settings, "RUNTIME_METERING_MODE", "required")
    repository = _Repository()
    repository.rows["run-1"] = RuntimeBillingRun(
        run_id="run-1",
        org_id="another-org",
        project_id="project-1",
        runtime_kind="sandbox",
        status="reserved",
        idempotency_key="runtime-reserve:run-1",
        reservation_id="reservation-1",
        estimated_units=5,
    )
    gateway = _Gateway()

    with pytest.raises(AppException) as caught:
        await RuntimeMeteringService(gateway=gateway, repository=repository).execute(
            audit_context=_context(),
            operation=lambda: None,
        )

    assert caught.value.status_code == 409
    assert caught.value.details["code"] == "runtime_run_identity_conflict"
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_operation_failure_still_settles_and_preserves_original_error(monkeypatch) -> None:
    monkeypatch.setattr(settings, "RUNTIME_METERING_MODE", "required")
    repository = _Repository()
    gateway = _Gateway()

    async def operation():
        raise ValueError("compute failed")

    with pytest.raises(ValueError, match="compute failed"):
        await RuntimeMeteringService(gateway=gateway, repository=repository).execute(
            audit_context=_context(),
            operation=operation,
        )

    assert repository.rows["run-1"].status == "settled"


@pytest.mark.asyncio
async def test_shadow_reservation_failure_allows_compute(monkeypatch) -> None:
    monkeypatch.setattr(settings, "RUNTIME_METERING_MODE", "shadow")
    repository = _Repository()
    gateway = _Gateway(BillingGatewayError(503, {"error": {"code": "upstream_down"}}))

    async def operation():
        return "shadow-result"

    result = await RuntimeMeteringService(gateway=gateway, repository=repository).execute(
        audit_context=_context(),
        operation=operation,
    )

    assert result == "shadow-result"
    assert repository.rows["run-1"].status == "unmetered"

    with pytest.raises(AppException) as caught:
        await RuntimeMeteringService(gateway=_Gateway(), repository=repository).execute(
            audit_context=_context(),
            operation=operation,
        )
    assert caught.value.details["code"] == "runtime_run_already_terminal"


@pytest.mark.asyncio
async def test_terminal_run_id_cannot_execute_compute_twice(monkeypatch) -> None:
    monkeypatch.setattr(settings, "RUNTIME_METERING_MODE", "required")
    repository = _Repository()
    repository.rows["run-1"] = RuntimeBillingRun(
        run_id="run-1",
        org_id="org-1",
        project_id="project-1",
        runtime_kind="sandbox",
        status="settled",
        idempotency_key="runtime-reserve:run-1",
        reservation_id="reservation-1",
        estimated_units=5,
    )
    executed = False

    async def operation():
        nonlocal executed
        executed = True

    with pytest.raises(AppException) as caught:
        await RuntimeMeteringService(
            gateway=_Gateway(),
            repository=repository,
        ).execute(audit_context=_context(), operation=operation)

    assert caught.value.status_code == 409
    assert caught.value.details["code"] == "runtime_run_already_terminal"
    assert executed is False


@pytest.mark.asyncio
async def test_concurrent_run_id_cannot_execute_compute_twice(monkeypatch) -> None:
    monkeypatch.setattr(settings, "RUNTIME_METERING_MODE", "required")
    repository = _Repository()
    repository.rows["run-1"] = RuntimeBillingRun(
        run_id="run-1",
        org_id="org-1",
        project_id="project-1",
        runtime_kind="sandbox",
        status="running",
        idempotency_key="runtime-reserve:run-1",
        reservation_id="reservation-1",
        estimated_units=5,
    )

    with pytest.raises(AppException) as caught:
        await RuntimeMeteringService(
            gateway=_Gateway(),
            repository=repository,
        ).execute(audit_context=_context(), operation=lambda: None)

    assert caught.value.status_code == 409
    assert caught.value.details["code"] == "runtime_run_in_progress"


@pytest.mark.asyncio
async def test_reservation_transport_failure_can_retry_same_run(monkeypatch) -> None:
    monkeypatch.setattr(settings, "RUNTIME_METERING_MODE", "required")
    repository = _Repository()
    failing = _Gateway(BillingGatewayError(503, {"error": {"code": "upstream_down"}}))

    with pytest.raises(AppException):
        await RuntimeMeteringService(gateway=failing, repository=repository).execute(
            audit_context=_context(),
            operation=lambda: None,
        )

    assert repository.rows["run-1"].status == "reservation_failed"
    executed = False

    async def operation():
        nonlocal executed
        executed = True

    await RuntimeMeteringService(gateway=_Gateway(), repository=repository).execute(
        audit_context=_context(),
        operation=operation,
    )
    assert executed is True
    assert repository.rows["run-1"].status == "settled"


@pytest.mark.asyncio
async def test_stale_pending_reservation_is_claimed_after_crash(monkeypatch) -> None:
    monkeypatch.setattr(settings, "RUNTIME_METERING_MODE", "required")
    monkeypatch.setattr(settings, "RUNTIME_RESERVATION_CLAIM_SECONDS", 120)
    repository = _Repository()
    repository.rows["run-1"] = RuntimeBillingRun(
        run_id="run-1",
        org_id="org-1",
        project_id="project-1",
        runtime_kind="sandbox",
        status="pending_reservation",
        idempotency_key="runtime-reserve:run-1",
        estimated_units=5,
        updated_at=datetime.now(UTC) - timedelta(seconds=121),
    )
    executed = False

    async def operation():
        nonlocal executed
        executed = True

    await RuntimeMeteringService(gateway=_Gateway(), repository=repository).execute(
        audit_context=_context(),
        operation=operation,
    )

    assert executed is True
    assert repository.rows["run-1"].status == "settled"


@pytest.mark.asyncio
async def test_fresh_pending_reservation_cannot_be_stolen(monkeypatch) -> None:
    monkeypatch.setattr(settings, "RUNTIME_METERING_MODE", "required")
    repository = _Repository()
    repository.rows["run-1"] = RuntimeBillingRun(
        run_id="run-1",
        org_id="org-1",
        project_id="project-1",
        runtime_kind="sandbox",
        status="pending_reservation",
        idempotency_key="runtime-reserve:run-1",
        estimated_units=5,
        updated_at=datetime.now(UTC),
    )

    with pytest.raises(AppException) as caught:
        await RuntimeMeteringService(gateway=_Gateway(), repository=repository).execute(
            audit_context=_context(),
            operation=lambda: None,
        )

    assert caught.value.status_code == 409
    assert caught.value.details["code"] == "runtime_run_in_progress"


@pytest.mark.asyncio
async def test_recovery_retries_failed_settlement_idempotently(monkeypatch) -> None:
    monkeypatch.setattr(settings, "RUNTIME_METERING_MODE", "required")
    repository = _Repository()
    run = RuntimeBillingRun(
        run_id="run-recovery",
        org_id="org-1",
        runtime_kind="sandbox",
        status="failed",
        idempotency_key="runtime-reserve:run-recovery",
        reservation_id="reservation-1",
        started_at=datetime.now(UTC) - timedelta(seconds=61),
        metadata={"runtime_seconds": 61},
        updated_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    repository.rows[run.run_id] = run
    repository.recovery_rows = [run]
    gateway = _Gateway()

    recovered = await RuntimeMeteringService(
        gateway=gateway,
        repository=repository,
    ).recover_once()

    assert recovered == 1
    assert repository.rows[run.run_id].status == "settled"
    settle = gateway.calls[0]
    assert settle[2]["idempotency_key"] == "runtime-settle:run-recovery"
    assert settle[2]["body"]["runtime_seconds"] == 61


@pytest.mark.asyncio
async def test_settlement_retry_reuses_identical_payload(monkeypatch) -> None:
    monkeypatch.setattr(settings, "RUNTIME_METERING_MODE", "required")
    repository = _Repository()
    run = RuntimeBillingRun(
        run_id="run-recovery",
        org_id="org-1",
        runtime_kind="sandbox",
        status="running",
        idempotency_key="runtime-reserve:run-recovery",
        reservation_id="reservation-1",
        started_at=datetime.now(UTC) - timedelta(seconds=61),
    )
    repository.rows[run.run_id] = run

    class _FailOnceGateway(_Gateway):
        async def request(self, method: str, path: str, **kwargs):
            self.calls.append((method, path, kwargs))
            if path.endswith("/settle") and len(self.calls) == 1:
                raise BillingGatewayError(503, {"error": {"code": "upstream_down"}})
            if path.endswith("/settle"):
                return {"runtime_units": 2}
            raise AssertionError(path)

    gateway = _FailOnceGateway()
    service = RuntimeMeteringService(gateway=gateway, repository=repository)
    await service._settle(run.run_id, 61)
    await service._settle(run.run_id, 61, allow_retry=True)

    assert repository.rows[run.run_id].status == "settled"
    assert gateway.calls[0][2]["body"] == gateway.calls[1][2]["body"]
    assert gateway.calls[0][2]["idempotency_key"] == "runtime-settle:run-recovery"


@pytest.mark.asyncio
async def test_provider_start_compensation_cancels_without_usage(monkeypatch) -> None:
    monkeypatch.setattr(settings, "RUNTIME_METERING_MODE", "required")
    repository = _Repository()
    gateway = _Gateway()
    service = RuntimeMeteringService(gateway=gateway, repository=repository)

    run_id = await service.start_session(audit_context=_context())
    assert run_id == "run-1"
    assert repository.rows["run-1"].status == "running"

    await service.cancel_session("run-1")

    assert repository.rows["run-1"].status == "canceled"
    assert repository.rows["run-1"].actual_units == 0
    assert gateway.calls[-1][1].endswith("/cancel")
    assert gateway.calls[-1][2]["idempotency_key"] == "runtime-cancel:run-1"


@pytest.mark.asyncio
async def test_cancel_intent_is_durable_before_transport(monkeypatch) -> None:
    monkeypatch.setattr(settings, "RUNTIME_METERING_MODE", "required")
    repository = _Repository()
    run = RuntimeBillingRun(
        run_id="run-cancel",
        org_id="org-1",
        runtime_kind="sandbox",
        status="running",
        idempotency_key="runtime-reserve:run-cancel",
        reservation_id="reservation-1",
    )
    repository.rows[run.run_id] = run

    class _CrashingGateway(_Gateway):
        async def request(self, method: str, path: str, **kwargs):
            raise RuntimeError("process interrupted after durable intent")

    with pytest.raises(RuntimeError, match="process interrupted"):
        await RuntimeMeteringService(
            gateway=_CrashingGateway(),
            repository=repository,
        ).cancel_session(run.run_id)

    persisted = repository.rows[run.run_id]
    assert persisted.status == "settling"
    assert persisted.metadata["cancel_requested"] is True


@pytest.mark.asyncio
async def test_recovery_preserves_cancel_intent(monkeypatch) -> None:
    monkeypatch.setattr(settings, "RUNTIME_METERING_MODE", "required")
    repository = _Repository()
    run = RuntimeBillingRun(
        run_id="run-cancel-recovery",
        org_id="org-1",
        runtime_kind="sandbox",
        status="failed",
        idempotency_key="runtime-reserve:run-cancel-recovery",
        reservation_id="reservation-1",
        metadata={"cancel_requested": True},
        updated_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    repository.rows[run.run_id] = run
    repository.recovery_rows = [run]
    gateway = _Gateway()

    recovered = await RuntimeMeteringService(
        gateway=gateway,
        repository=repository,
    ).recover_once()

    assert recovered == 1
    assert repository.rows[run.run_id].status == "canceled"
    assert gateway.calls[0][1].endswith("/cancel")


@pytest.mark.asyncio
async def test_concurrent_settlement_has_one_fenced_transport_call(monkeypatch) -> None:
    monkeypatch.setattr(settings, "RUNTIME_METERING_MODE", "required")
    repository = _Repository()
    run = RuntimeBillingRun(
        run_id="run-concurrent-settle",
        org_id="org-1",
        runtime_kind="sandbox",
        status="running",
        idempotency_key="runtime-reserve:run-concurrent-settle",
        reservation_id="reservation-1",
        updated_at=datetime.now(UTC),
    )
    repository.rows[run.run_id] = run
    entered = asyncio.Event()
    release = asyncio.Event()

    class _BlockingGateway(_Gateway):
        async def request(self, method: str, path: str, **kwargs):
            self.calls.append((method, path, kwargs))
            entered.set()
            await release.wait()
            return {"runtime_units": 1}

    gateway = _BlockingGateway()
    service = RuntimeMeteringService(gateway=gateway, repository=repository)
    first = asyncio.create_task(service._settle(run.run_id, 1))
    await entered.wait()
    second = await service._settle(run.run_id, 1)
    release.set()
    assert await first is True

    assert second is False
    assert len(gateway.calls) == 1
    assert repository.rows[run.run_id].status == "settled"


@pytest.mark.asyncio
async def test_agent_takeover_reuses_reservation_and_checks_expiry(monkeypatch):
    monkeypatch.setattr(settings, "RUNTIME_METERING_MODE", "required")
    repository, gateway = _Repository(), _Gateway()
    first = RuntimeMeteringService(repository=repository, gateway=gateway)
    run_id = await first.start_session(audit_context=_context())
    started = repository.get(run_id).started_at
    await first.detach_session(run_id)
    second = RuntimeMeteringService(repository=repository, gateway=gateway)
    try:
        assert await second.resume_session(run_id, project_id="project-1", actor_id="user-1") == run_id
        assert repository.get(run_id).started_at == started
        assert len([call for call in gateway.calls if call[1].endswith('/runtime/reservations')]) == 1
        await second.check_session(run_id)
        with pytest.raises(ValueError):
            await second.resume_session(run_id, project_id="project-1", actor_id="other-user")
        repository.update(run_id, {"expires_at": datetime.now(UTC) - timedelta(seconds=1)})
        with pytest.raises(ValueError):
            await second.check_session(run_id)
        await second.finish_session(run_id)
        await second.finish_session(run_id)
        assert len([call for call in gateway.calls if call[1].endswith('/settle')]) == 1
    finally:
        await second.detach_session(run_id)
