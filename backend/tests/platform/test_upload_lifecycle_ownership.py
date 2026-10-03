"""Upload lifecycle behavior after extracting it from generic file processing."""
from unittest.mock import AsyncMock, Mock

import pytest

from src.exceptions import NotFoundException
from src.platform.upload.service import ETLService
from src.platform.upload.tasks.models import ETLTask, ETLTaskStatus
from src.platform.upload.state.models import ETLPhase, ETLRuntimeState


def service_for(status=ETLTaskStatus.PENDING, *, cached=True):
    task = ETLTask(task_id="file-1", project_id="project-1", created_by="user-1",
                   filename="notes.pdf", status=status)
    repo = Mock()
    repo.get_task.return_value = task
    state = ETLRuntimeState(task_id=task.task_id, project_id=task.project_id,
        user_id="user-1", filename=task.filename, rule_id=0, status=status, phase=ETLPhase.OCR)
    states = Mock(get=AsyncMock(return_value=state if cached else None),
                  set=AsyncMock(), set_terminal=AsyncMock())
    queue = Mock(enqueue_ocr=AsyncMock(return_value="ocr-1"),
                 enqueue_postprocess=AsyncMock(return_value="postprocess-1"))
    return ETLService(repo, queue, states), task


@pytest.mark.asyncio
@pytest.mark.parametrize("cached", [True, False])
async def test_cancel_records_both_durable_and_runtime_terminal_state(cached):
    service, task = service_for(cached=cached)
    result = await service.cancel_task(task.task_id, "user-1")
    assert result.status == ETLTaskStatus.CANCELLED
    service.task_repository.update_task.assert_called_once_with(task)
    terminal = service.state_repo.set_terminal.call_args.args[0]
    assert terminal.status == ETLTaskStatus.CANCELLED
    assert terminal.phase == ETLPhase.FINALIZE
    service.arq_client.enqueue_ocr.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_preserves_child_ownership_check_and_terminal_protection():
    service, task = service_for()
    with pytest.raises(NotFoundException):
        await service.cancel_task(task.task_id, "other-user")
    service.task_repository.update_task.assert_not_called()
    service.state_repo.set_terminal.assert_not_called()
    task.status = ETLTaskStatus.COMPLETED
    with pytest.raises(ValueError, match="not cancellable"):
        await service.cancel_task(task.task_id, "user-1", force=True)
    service.task_repository.update_task.assert_not_called()


@pytest.mark.asyncio
async def test_retry_restores_ocr_stage_without_changing_lifecycle_owner():
    service, task = service_for(ETLTaskStatus.FAILED)
    result = await service.retry_task(task.task_id, "user-1", "mineru")
    assert result.status == ETLTaskStatus.PENDING
    service.arq_client.enqueue_ocr.assert_awaited_once_with(task.task_id)
    service.arq_client.enqueue_postprocess.assert_not_called()
    runtime = service.state_repo.set.call_args.args[0]
    assert runtime.phase == ETLPhase.OCR
    assert runtime.arq_job_id_ocr == "ocr-1"
    service.task_repository.update_task.assert_called_once_with(task)


@pytest.mark.asyncio
async def test_failed_retry_enqueue_does_not_overwrite_recorded_failure():
    service, task = service_for(ETLTaskStatus.FAILED)
    service.arq_client.enqueue_ocr.side_effect = ConnectionError("offline")
    with pytest.raises(ConnectionError):
        await service.retry_task(task.task_id, "user-1", "mineru")
    assert task.status == ETLTaskStatus.FAILED
    service.task_repository.update_task.assert_not_called()
    service.state_repo.set.assert_not_called()
