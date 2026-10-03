"""
MineRU OCR Provider Adapter

Wraps the existing MineRU client to implement the OCRProvider interface.
"""

import logging
from typing import NoReturn

from src.infra.file_processing.mineru.client import MineRUClient
from src.infra.file_processing.mineru.config import mineru_config
from src.infra.file_processing.mineru.exceptions import (
    MineRUAPIError,
    MineRUAPIKeyError,
    MineRUError,
    MineRUTimeoutError,
)
from src.infra.file_processing.mineru.schemas import (
    MineRUModelVersion,
    MineRUTaskState,
    TaskStatusResponse,
)
from src.infra.file_processing.ocr.base import (
    OCRExternalJob,
    OCRExternalJobCompletion,
    OCRProvider,
    OCRProviderAPIError,
    OCRProviderCleanupResult,
    OCRProviderCleanupState,
    OCRProviderConfigError,
    OCRProviderTimeoutError,
    ParsedDocument,
    parse_document_with_external_lifecycle,
)

logger = logging.getLogger(__name__)


class MineRUProvider(OCRProvider):
    """
    MineRU OCR Provider.

    Wraps the existing MineRUClient to provide a unified OCRProvider interface.
    """

    def __init__(self, api_key: str | None = None):
        """
        Initialize MineRU provider.

        Args:
            api_key: MineRU API key (defaults to env var MINERU_API_KEY)
        """
        self._api_key = api_key or mineru_config.mineru_api_key
        self._client: MineRUClient | None = None

    @property
    def name(self) -> str:
        return "mineru"

    def _get_client(self) -> MineRUClient:
        """Lazy initialization of MineRU client."""
        if self._client is None:
            try:
                self._client = MineRUClient(api_key=self._api_key)
            except MineRUAPIKeyError as e:
                raise OCRProviderConfigError(
                    provider=self.name,
                    message="MineRU API key not configured. Set MINERU_API_KEY environment variable.",
                ) from e
        return self._client

    async def parse_document(
        self,
        file_url: str,
        data_id: str | None = None,
    ) -> ParsedDocument:
        """
        Parse document using MineRU OCR.

        Args:
            file_url: Presigned URL to the document
            data_id: Optional tracking identifier

        Returns:
            ParsedDocument with extracted content
        """
        return await parse_document_with_external_lifecycle(
            self,
            file_url=file_url,
            data_id=data_id,
        )

    async def create_external_job(
        self,
        file_url: str,
        data_id: str | None = None,
    ) -> OCRExternalJob:
        """Create a MineRU task and immediately expose its durable handle."""

        client = self._get_client()
        try:
            created = await client.create_task(
                file_url=file_url,
                model_version=MineRUModelVersion.VLM,
                data_id=data_id,
            )
        except MineRUError as exc:
            self._raise_provider_error(exc)
        return OCRExternalJob(
            provider=self.name,
            task_id=created.task_id,
            metadata={"trace_id": created.trace_id} if created.trace_id else {},
        )

    async def wait_external_job(
        self,
        job: OCRExternalJob,
    ) -> OCRExternalJobCompletion:
        self._validate_job(job)
        try:
            status = await self._get_client().wait_for_completion(job.task_id)
        except MineRUError as exc:
            self._raise_provider_error(exc)
        return OCRExternalJobCompletion(
            job=job,
            metadata={"status": status.model_dump(mode="json")},
        )

    async def materialize_external_job(
        self,
        completion: OCRExternalJobCompletion,
    ) -> ParsedDocument:
        self._validate_job(completion.job)
        try:
            status = TaskStatusResponse.model_validate(completion.metadata["status"])
            result = await self._get_client().materialize_task(
                completion.job.task_id,
                status,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise OCRProviderAPIError(
                provider=self.name,
                message=f"Invalid MineRU completion payload: {exc}",
            ) from exc
        except MineRUError as exc:
            self._raise_provider_error(exc)

        return ParsedDocument(
            task_id=result.task_id,
            markdown_content=result.markdown_content,
            cache_dir=result.cache_dir,
            markdown_path=result.markdown_path,
            metadata={"provider": self.name, "mode": "external"},
        )

    def _validate_job(self, job: OCRExternalJob) -> None:
        if job.provider != self.name or not job.external or not job.task_id:
            raise OCRProviderAPIError(
                provider=self.name,
                message="MineRU external job handle is invalid",
            )

    def _raise_provider_error(self, exc: MineRUError) -> NoReturn:
        if isinstance(exc, MineRUTimeoutError):
            raise OCRProviderTimeoutError(
                provider=self.name,
                message=str(exc),
            ) from exc
        raise OCRProviderAPIError(
            provider=self.name,
            message=str(exc),
            status_code=(
                getattr(exc, "status_code", None) if isinstance(exc, MineRUAPIError) else None
            ),
        ) from exc

    async def cancel_external_job(self, task_id: str) -> OCRProviderCleanupResult:
        """Verify terminal state; MineRU exposes no cancellation operation."""

        try:
            status = await self._get_client().get_task_status(task_id)
        except MineRUAPIError as exc:
            if exc.status_code == 404:
                return OCRProviderCleanupResult(
                    provider=self.name,
                    task_id=task_id,
                    state=OCRProviderCleanupState.COMPLETE,
                    detail="MineRU task is absent",
                )
            return OCRProviderCleanupResult(
                provider=self.name,
                task_id=task_id,
                state=OCRProviderCleanupState.FAILED,
                detail=str(exc),
                retryable=True,
            )
        except MineRUError as exc:
            return OCRProviderCleanupResult(
                provider=self.name,
                task_id=task_id,
                state=OCRProviderCleanupState.FAILED,
                detail=str(exc),
                retryable=True,
            )

        if status.state in {MineRUTaskState.COMPLETED, MineRUTaskState.FAILED}:
            return OCRProviderCleanupResult(
                provider=self.name,
                task_id=task_id,
                state=OCRProviderCleanupState.COMPLETE,
                detail=f"MineRU task is terminal ({status.state.value})",
            )
        return OCRProviderCleanupResult(
            provider=self.name,
            task_id=task_id,
            state=OCRProviderCleanupState.UNSUPPORTED,
            detail=(
                f"MineRU task is {status.state.value}; provider has no verified cancellation API"
            ),
            retryable=True,
        )

    async def health_check(self) -> bool:
        """Check if MineRU is properly configured."""
        try:
            self._get_client()
            return True
        except OCRProviderConfigError:
            return False
