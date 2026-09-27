import asyncio
import io
import json
import os
import zipfile
from types import SimpleNamespace

import pytest

from src.provider._base import (
    AuthRequirement,
    BaseProvider,
    Capability,
    ProviderSpec,
    Credentials,
    FetchResult,
)
from src.provider.github.adapter import GithubProvider
from src.platform.imports.jobs import execute_import_job
from src.platform.imports.repository import ImportJob
from src.platform.imports.runner import ImportRunResult, OneTimeImportRunner
from src.platform.imports.schemas import ImportJobCreateRequest
from src.platform.imports.service import ImportJobService
from tests.authorization_fakes import authorization_for


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return buffer.getvalue()


class MultiFileConnector(BaseProvider):
    def spec(self) -> ProviderSpec:
        return ProviderSpec(
            provider="github",
            display_name="GitHub",
            capabilities=Capability.PULL,
            supported_directions=["inbound"],
            auth=AuthRequirement.OPTIONAL_OAUTH,
            oauth_type="github",
        )

    async def fetch(self, config, credentials):
        return FetchResult(
            content={"manifest": True},
            content_hash="hash-1",
            node_type="folder",
            node_name="repo",
            files={
                "README.md": b"# Repo",
                ".puppyone/import.json": b'{"source":"github"}',
            },
            summary="Import from GitHub acme/repo",
        )


class FakeRegistry:
    connector = MultiFileConnector()

    def get(self, provider):
        return self.connector if provider == "github" else None

    async def resolve_credentials(self, oauth_type, user_id, *, required=True):
        return Credentials()


class SingleConnectorRegistry:
    def __init__(self, connector):
        self.connector = connector

    def get(self, provider):
        return self.connector if provider == "github" else None

    async def resolve_credentials(self, oauth_type, user_id, *, required=True):
        assert oauth_type == "github"
        assert required is False
        return Credentials()


class NoopWriteLease:
    def __init__(self, *_args, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class FakeOps:
    def __init__(self):
        self.bulk_write_call = None

    async def bulk_write(
        self,
        project_id,
        files,
        actor=None,
        who=None,
        scope="",
        deleted=None,
        message="",
        defer_projection=False,
        **_kwargs,
    ):
        self.bulk_write_call = {
            "project_id": project_id,
            "files": files,
            "actor": actor or who,
            "deleted": deleted,
            "message": message,
        }
        return SimpleNamespace(result=SimpleNamespace(commit_id="commit-1"))

    async def write_bytes(self, *args, **kwargs):
        raise AssertionError("multi-file imports must use bulk_write")


@pytest.mark.asyncio
async def test_import_runner_writes_without_creating_sync_binding(monkeypatch):
    fake_ops = FakeOps()

    import src.platform.imports.runner as runner_module
    import src.version_engine.bootstrap.dependencies as version_deps

    monkeypatch.setattr(runner_module, "get_import_provider_registry", lambda: FakeRegistry())
    monkeypatch.setattr(
        version_deps,
        "build_worker_version_engine_container",
        lambda: SimpleNamespace(write_commands=lambda: fake_ops),
    )

    job = ImportJob(
        id="job-1",
        project_id="project-1",
        created_by="user-1",
        provider="github",
        source_url="https://github.com/acme/repo",
        name="repo",
    )

    result = await OneTimeImportRunner(write_lease_factory=NoopWriteLease).run(job)

    assert result.path == "repo"
    assert result.commit_id == "commit-1"
    assert fake_ops.bulk_write_call == {
        "project_id": "project-1",
        "files": {
            "repo/README.md": b"# Repo",
            "repo/.puppyone/import.json": b'{"source":"github"}',
        },
        "actor": "import:github:job-1",
        "deleted": None,
        "message": "Import from GitHub acme/repo",
    }


@pytest.mark.asyncio
async def test_import_runner_uses_real_github_connector_archive_flow(monkeypatch):
    """Hermetic coverage for GitHub zip -> import job -> Version Engine write."""
    fake_ops = FakeOps()
    connector = GithubProvider(github_service=None, s3_service=None)

    import src.provider.github.adapter as github_module
    import src.platform.imports.runner as runner_module
    import src.version_engine.bootstrap.dependencies as version_deps

    async def fake_fetch_repo_metadata(client, headers, repo_ref):
        assert repo_ref.owner == "octo"
        assert repo_ref.repo == "tiny"
        return {
            "full_name": "octo/tiny",
            "description": "Tiny fixture repo",
            "html_url": "https://github.com/octo/tiny",
            "default_branch": "main",
        }

    async def fake_fetch_commit_sha(client, headers, repo_ref, ref):
        assert ref == "main"
        return "abcdef1234567890"

    async def fake_download_zipball(client, headers, repo_ref, ref, config):
        return _zip_bytes({
            "octo-tiny-sha/README.md": b"# Tiny\n",
            "octo-tiny-sha/src/app.py": b"print('ok')\n",
            "octo-tiny-sha/.env": b"SECRET=1",
        })

    monkeypatch.setattr(github_module, "_fetch_repo_metadata", fake_fetch_repo_metadata)
    monkeypatch.setattr(github_module, "_fetch_commit_sha", fake_fetch_commit_sha)
    monkeypatch.setattr(github_module, "_download_zipball", fake_download_zipball)
    monkeypatch.setattr(
        runner_module,
        "get_import_provider_registry",
        lambda: SingleConnectorRegistry(connector),
    )
    monkeypatch.setattr(
        version_deps,
        "build_worker_version_engine_container",
        lambda: SimpleNamespace(write_commands=lambda: fake_ops),
    )

    job = ImportJob(
        id="job-zip",
        project_id="project-1",
        created_by="user-1",
        provider="github",
        source_url="https://github.com/octo/tiny",
        name="tiny",
    )

    result = await OneTimeImportRunner(write_lease_factory=NoopWriteLease).run(job)

    written = fake_ops.bulk_write_call["files"]
    assert result.path == "tiny"
    assert result.commit_id == "commit-1"
    assert written["tiny/README.md"] == b"# Tiny\n"
    assert written["tiny/src/app.py"] == b"print('ok')\n"
    assert "tiny/.env" not in written

    manifest = json.loads(written["tiny/.puppyone/import.json"])
    assert manifest["full_name"] == "octo/tiny"
    assert manifest["commit_sha"] == "abcdef1234567890"
    assert manifest["files_imported"] == 2
    assert manifest["files_skipped"] == 1


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.network
@pytest.mark.skipif(
    os.getenv("RUN_GITHUB_IMPORT_SMOKE") != "1",
    reason="Set RUN_GITHUB_IMPORT_SMOKE=1 to run live GitHub import smoke test.",
)
async def test_live_github_import_smoke_octocat_hello_world(monkeypatch):
    """Manual smoke test against a tiny public GitHub repository.

    This is intentionally skipped by default: it depends on GitHub availability,
    local network access, and anonymous API rate limits. Run it in a non-blocking
    smoke job when external service health should be sampled.
    """
    fake_ops = FakeOps()
    connector = GithubProvider(github_service=None, s3_service=None)

    import src.platform.imports.runner as runner_module
    import src.version_engine.bootstrap.dependencies as version_deps

    monkeypatch.setattr(
        runner_module,
        "get_import_provider_registry",
        lambda: SingleConnectorRegistry(connector),
    )
    monkeypatch.setattr(
        version_deps,
        "build_worker_version_engine_container",
        lambda: SimpleNamespace(write_commands=lambda: fake_ops),
    )

    job = ImportJob(
        id="job-smoke",
        project_id="project-smoke",
        created_by="user-1",
        provider="github",
        source_url=os.getenv(
            "GITHUB_IMPORT_SMOKE_URL",
            "https://github.com/octocat/Hello-World",
        ),
        name="hello-world",
        config={
            "max_files": 20,
            "max_archive_bytes": 2 * 1024 * 1024,
            "max_total_bytes": 1024 * 1024,
        },
    )

    result = await OneTimeImportRunner(write_lease_factory=NoopWriteLease).run(job)

    written = fake_ops.bulk_write_call["files"]
    assert result.path == "hello-world"
    assert any(path.lower().startswith("hello-world/readme") for path in written)
    assert "hello-world/.puppyone/import.json" in written


class FakeImportRepo:
    def __init__(self):
        self.job = ImportJob(
            id="job-1",
            project_id="project-1",
            created_by="user-1",
            provider="github",
            source_url="https://github.com/acme/repo",
            name="repo",
        )
        self.updates = []
        self.completed = None

    def get(self, job_id):
        assert job_id == "job-1"
        return self.job

    def mark_running(self, job_id, *, phase, progress, message):
        self.updates.append((phase, progress, message))
        self.job.status = "running"
        self.job.phase = phase
        self.job.progress = progress
        self.job.message = message
        return self.job

    def update(self, job_id, **fields):
        self.updates.append((fields.get("phase"), fields.get("progress"), fields.get("message")))
        for key, value in fields.items():
            setattr(self.job, key, value)
        return self.job

    def mark_completed(self, job_id, *, result_path, result_commit_id, message):
        self.completed = (result_path, result_commit_id, message)
        self.job.status = "completed"
        return self.job

    def mark_failed(self, job_id, error_message):
        raise AssertionError(f"unexpected failure: {error_message}")


class FakeImportRunner:
    async def run(self, job, *, on_phase=None):
        await on_phase("fetching", 25, "Fetching from GitHub")
        await on_phase("writing", 75, "Writing files into the workspace")
        return ImportRunResult(path="repo", commit_id="commit-1", summary="done")


@pytest.mark.asyncio
async def test_execute_import_job_owns_status_lifecycle():
    repo = FakeImportRepo()

    result = await execute_import_job(
        {
            "import_job_repository": repo,
            "one_time_import_runner": FakeImportRunner(),
        },
        "job-1",
    )

    assert result["status"] == "completed"
    assert repo.updates == [
        ("validating", 5, "Preparing import"),
        ("fetching", 25, "Fetching from GitHub"),
        ("writing", 75, "Writing files into the workspace"),
    ]
    assert repo.completed == ("repo", "commit-1", "done")


class FakeCancellingRunner:
    def __init__(self, repo):
        self.repo = repo

    async def run(self, job, *, on_phase=None):
        await on_phase("fetching", 25, "Fetching from GitHub")
        self.repo.job.status = "cancelled"
        self.repo.job.phase = "cancelled"
        await on_phase("writing", 75, "Writing files into the workspace")
        raise AssertionError("cancelled import must not continue to write result")


@pytest.mark.asyncio
async def test_execute_import_job_does_not_overwrite_cancelled_job():
    repo = FakeImportRepo()

    result = await execute_import_job(
        {
            "import_job_repository": repo,
            "one_time_import_runner": FakeCancellingRunner(repo),
        },
        "job-1",
    )

    assert result == {
        "status": "skipped",
        "job_id": "job-1",
        "job_status": "cancelled",
    }
    assert repo.completed is None
    assert repo.job.status == "cancelled"


class IdempotentImportRepo:
    def __init__(self):
        self.existing = ImportJob(
            id="job-existing",
            org_id="org-1",
            project_id="project-1",
            created_by="user-1",
            provider="github",
            source_url="https://github.com/acme/repo",
            idempotency_key="idem-1",
            name="repo",
        )

    def get_by_idempotency_key(self, *, project_id, provider, idempotency_key):
        assert project_id == "project-1"
        assert provider == "github"
        assert idempotency_key == "idem-1"
        return self.existing

    def create(self, **_kwargs):
        raise AssertionError("idempotent create must not insert a new job")

    def mark_failed(self, *_args, **_kwargs):
        raise AssertionError("idempotent create must not mark failures")


class ExplodingImportArqClient:
    async def enqueue_import(self, _job_id):
        raise AssertionError("idempotent create must not enqueue a worker job")


@pytest.mark.asyncio
async def test_import_job_create_returns_existing_job_for_idempotency_key():
    repo = IdempotentImportRepo()
    service = ImportJobService(
        repo=repo,
        authorization=authorization_for("project-1"),
        arq_client=ExplodingImportArqClient(),
    )

    result = await service.create(
        ImportJobCreateRequest(
            project_id="project-1",
            source_url="https://github.com/acme/repo",
            provider="github",
            idempotency_key="idem-1",
        ),
        user_id="user-1",
    )

    assert result is repo.existing


class _RecordingFailRepo(FakeImportRepo):
    """FakeImportRepo that records mark_failed instead of asserting (the base
    raises, since its happy-path tests never expect a failure)."""

    def __init__(self):
        super().__init__()
        self.failed = None

    def mark_failed(self, job_id, error_message):
        self.failed = (job_id, error_message)
        self.job.status = "failed"
        return self.job


class _TimeoutRunner:
    async def run(self, job, *, on_phase=None):
        await on_phase("fetching", 25, "Fetching from GitHub")
        raise asyncio.CancelledError()


@pytest.mark.asyncio
async def test_execute_import_job_finalizes_on_cancellation():
    """Worker timeout/cancellation must finalize the row (mark failed) AND
    re-raise CancelledError so ARQ sees the cancellation — otherwise a killed
    worker leaves the job stuck `running` forever (validation task 4.3)."""
    repo = _RecordingFailRepo()
    with pytest.raises(asyncio.CancelledError):
        await execute_import_job(
            {"import_job_repository": repo, "one_time_import_runner": _TimeoutRunner()},
            "job-1",
        )
    assert repo.failed is not None and repo.failed[0] == "job-1"
    assert repo.job.status == "failed"
