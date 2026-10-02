from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.platform.upload import jobs
from src.infra.file_processing.state.models import ETLPhase, ETLRuntimeState
from src.infra.file_processing.tasks.models import ETLTaskStatus

TASK_ID = "task-1"
PROJECT_ID = "project-1"
USER_ID = "00000000-0000-4000-8000-000000000001"


def _task():
    return SimpleNamespace(
        task_id=TASK_ID,
        created_by=USER_ID,
        project_id=PROJECT_ID,
        filename="document.pdf",
        rule_id=1,
        metadata={},
        status=ETLTaskStatus.RUNNING,
    )


class Repository:
    def __init__(self, first_task) -> None:
        self.first_task = first_task
        self.calls = 0

    def get_task(self, task_id):
        assert task_id == TASK_ID
        self.calls += 1
        return self.first_task if self.calls == 1 else None

    def update_task(self, task):
        return task


class NoopLease:
    def __init__(self, *_args, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class StateRepository:
    def __init__(self, state=None) -> None:
        self.state = state

    async def get(self, task_id):
        assert task_id == TASK_ID
        return self.state

    async def set(self, state):
        self.state = state


class S3:
    def __init__(self) -> None:
        self.uploads = []

    async def generate_presigned_download_url(self, key, expires_in):
        assert expires_in == 3600
        return f"https://storage.invalid/{key}"

    async def download_file(self, key):
        assert key == "artifact.md"
        return b"# parsed"

    async def upload_file(self, **kwargs):
        self.uploads.append(kwargs)
        raise AssertionError("S3 upload crossed a deleted Project write fence")


class OCR:
    name = "fake"

    async def parse_document(self, *, file_url, data_id):
        assert file_url.startswith("https://storage.invalid/")
        assert data_id == TASK_ID
        return SimpleNamespace(task_id="provider-1", markdown_content="# parsed")


@pytest.mark.asyncio
async def test_ocr_external_wait_revalidates_durable_task_before_artifact_upload():
    task = _task()
    repository = Repository(task)
    s3 = S3()
    state_repository = StateRepository()

    result = await jobs.etl_ocr_job(
        {
            "task_repository": repository,
            "s3_service": s3,
            "ocr_provider": OCR(),
            "state_repo": state_repository,
            "arq_queue_name": "etl",
            "redis": SimpleNamespace(),
            "project_write_lease_factory": NoopLease,
        },
        TASK_ID,
    )

    assert result == {"ok": True, "skipped": "task_not_live"}
    assert repository.calls == 2
    assert s3.uploads == []


@pytest.mark.asyncio
async def test_llm_external_wait_revalidates_durable_task_before_output_upload(
    monkeypatch,
):
    task = _task()
    task.metadata["artifact_mineru_markdown_key"] = "artifact.md"
    repository = Repository(task)
    s3 = S3()
    state_repository = StateRepository(
        ETLRuntimeState(
            task_id=TASK_ID,
            user_id=USER_ID,
            project_id=PROJECT_ID,
            filename=task.filename,
            rule_id=task.rule_id,
            status=ETLTaskStatus.LLM_PROCESSING,
            phase=ETLPhase.POSTPROCESS,
            progress=60,
            artifact_mineru_markdown_key="artifact.md",
        )
    )

    class RuleRepository:
        def __init__(self, **_kwargs):
            pass

        def get_rule(self, rule_id):
            assert rule_id == "1"
            return SimpleNamespace(postprocess_mode="skip")

    monkeypatch.setattr(
        jobs,
        "SupabaseClient",
        lambda: SimpleNamespace(client=SimpleNamespace()),
    )
    monkeypatch.setattr(jobs, "RuleRepositorySupabase", RuleRepository)

    result = await jobs.etl_postprocess_job(
        {
            "task_repository": repository,
            "s3_service": s3,
            "llm_service": SimpleNamespace(),
            "state_repo": state_repository,
        },
        TASK_ID,
    )

    assert result == {"ok": True, "skipped": "task_not_live"}
    assert repository.calls == 2
    assert s3.uploads == []
