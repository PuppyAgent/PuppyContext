"""Service layer for durable one-time imports."""

from __future__ import annotations

from fastapi import HTTPException

from src.platform.authorization.models import ProjectAction
from src.platform.authorization.service import AuthorizationService
from src.platform.imports.arq_client import ImportArqClient
from src.platform.imports.provider import detect_import_provider, suggest_import_name
from src.platform.imports.providers import (
    get_import_provider_registry, import_adapter_name, require_import_provider,
)
from src.provider.registry import ProviderRegistry
from src.platform.imports.repository import ImportJob, ImportJobRepository
from src.platform.imports.schemas import ImportJobCreateRequest, ImportJobStatus


def _infer_source_kind(provider: str) -> str:
    if provider == "github":
        return "repository"
    if provider in {"url", "notion"}:
        return "website"
    if provider in {"google_docs", "google_sheets"}:
        return "document"
    return "other"


class ImportJobService:
    def __init__(
        self,
        *,
        repo: ImportJobRepository,
        authorization: AuthorizationService,
        arq_client: ImportArqClient,
        registry: ProviderRegistry | None = None,
    ):
        self.repo = repo
        self.authorization = authorization
        self.arq_client = arq_client
        self.registry = registry

    async def create(self, request: ImportJobCreateRequest, user_id: str) -> ImportJob:
        grant = self.authorization.authorize(
            request.project_id, user_id, ProjectAction.INGEST_WRITE
        )
        provider = (request.provider or detect_import_provider(request.source_url)).strip().lower()
        try:
            import_adapter_name(provider)
            require_import_provider(self.registry or get_import_provider_registry(), provider)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        config = dict(request.config or {})
        if request.crawl_options:
            config["crawl_options"] = request.crawl_options
        source_ref = dict(request.source_ref or {})
        source_ref.setdefault("url", request.source_url)
        name = request.name or suggest_import_name(provider, request.source_url)

        if request.idempotency_key:
            existing = self.repo.get_by_idempotency_key(
                project_id=request.project_id,
                provider=provider,
                idempotency_key=request.idempotency_key,
            )
            if existing is not None:
                return existing

        job = self.repo.create(
            org_id=grant.org_id,
            project_id=request.project_id,
            created_by=user_id,
            provider=provider,
            source_url=request.source_url,
            source_kind=request.source_kind or _infer_source_kind(provider),
            source_ref=source_ref,
            idempotency_key=request.idempotency_key,
            name=name,
            target_path=request.target_path or "",
            config=config,
        )

        try:
            worker_job_id = await self.arq_client.enqueue_import(job.id)
        except Exception as exc:
            self.repo.mark_failed(job.id, f"Failed to enqueue import worker: {exc}")
            raise HTTPException(
                status_code=503,
                detail="Import worker is unavailable. Try again in a moment.",
            ) from exc

        return self.repo.update(
            job.id,
            worker_job_id=worker_job_id,
            status=ImportJobStatus.QUEUED.value,
            phase="queued",
            progress=0,
            message="Queued",
        ) or job

    def get_for_user(self, job_id: str, user_id: str) -> ImportJob:
        job = self.repo.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Import job not found")
        self.authorization.authorize(
            job.project_id, user_id, ProjectAction.CONTENT_READ
        )
        return job

    def list_for_project(
        self,
        project_id: str,
        user_id: str,
        *,
        active_only: bool = False,
        limit: int = 20,
    ) -> list[ImportJob]:
        self.authorization.authorize(project_id, user_id, ProjectAction.CONTENT_READ)
        return self.repo.list_by_project(
            project_id,
            active_only=active_only,
            limit=limit,
        )

    def cancel(self, job_id: str, user_id: str) -> ImportJob:
        job = self.repo.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Import job not found")
        self.authorization.authorize(
            job.project_id, user_id, ProjectAction.INGEST_WRITE
        )
        if job.status in {
            ImportJobStatus.COMPLETED.value,
            ImportJobStatus.FAILED.value,
            ImportJobStatus.CANCELLED.value,
        }:
            return job
        return self.repo.mark_cancelled(job_id) or job
