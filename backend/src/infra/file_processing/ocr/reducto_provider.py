"""
Reducto OCR Provider

Reducto is a document parsing service that extracts text from PDFs and images.
API Documentation: https://docs.reducto.ai

Key features:
- High-quality PDF parsing with layout preservation
- Table extraction
- Markdown output
"""

import asyncio
import logging

import httpx
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

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


class ReductoConfig(BaseSettings):
    """Configuration for Reducto API."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,
    )

    reducto_api_key: str | None = Field(
        default=None,
        description="Reducto API Key",
    )

    reducto_api_base_url: str = Field(
        default="https://platform.reducto.ai",
        description="Reducto API base URL",
    )

    reducto_poll_interval: int = Field(
        default=3,
        description="Polling interval in seconds",
    )

    reducto_max_wait_time: int = Field(
        default=600,
        description="Maximum wait time for task completion (10 minutes)",
    )


# Global config instance
reducto_config = ReductoConfig()


class ReductoProvider(OCRProvider):
    """
    Reducto OCR Provider.

    Uses Reducto's API to parse documents and extract text as markdown.

    API Flow:
    1. POST /parse - Create a parsing job with document URL
    2. Poll job status until completion
    3. Get markdown result
    """

    def __init__(self, api_key: str | None = None):
        """
        Initialize Reducto provider.

        Args:
            api_key: Reducto API key (defaults to env var REDUCTO_API_KEY)
        """
        self._api_key = api_key or reducto_config.reducto_api_key
        self._base_url = reducto_config.reducto_api_base_url
        self._poll_interval = reducto_config.reducto_poll_interval
        self._max_wait_time = reducto_config.reducto_max_wait_time

        if not self._api_key:
            logger.warning("Reducto API key not configured")

    @property
    def name(self) -> str:
        return "reducto"

    def _get_headers(self) -> dict:
        """Get request headers with authentication."""
        if not self._api_key:
            raise OCRProviderConfigError(
                provider=self.name,
                message="Reducto API key not configured. Set REDUCTO_API_KEY environment variable.",
            )
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    async def parse_document(
        self,
        file_url: str,
        data_id: str | None = None,
    ) -> ParsedDocument:
        """
        Parse document using Reducto API.

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
        """Submit a Reducto parse and expose an async job ID immediately."""

        headers = self._get_headers()
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                logger.info(f"[Reducto] Creating parse job for: {file_url[:50]}...")
                response = await client.post(
                    f"{self._base_url}/parse",
                    headers=headers,
                    json={
                        "document_url": file_url,
                        "options": {
                            "output_mode": "markdown",
                            "table_output_mode": "markdown",
                        },
                    },
                )
        except httpx.HTTPError as exc:
            raise OCRProviderAPIError(
                provider=self.name,
                message=f"HTTP error creating job: {exc}",
            ) from exc

        if response.status_code == 401:
            raise OCRProviderAPIError(
                provider=self.name,
                message="Authentication failed. Check your REDUCTO_API_KEY.",
                status_code=401,
                raw_response=response.text,
            )
        if response.status_code != 200:
            raise OCRProviderAPIError(
                provider=self.name,
                message=f"Failed to create parse job: {response.text}",
                status_code=response.status_code,
                raw_response=response.text,
            )

        payload = response.json()
        if "result" in payload and payload.get("status") == "completed":
            # A synchronous response has no live remote job to cancel.
            return OCRExternalJob(
                provider=self.name,
                task_id=str(payload.get("job_id") or data_id or "sync"),
                external=False,
                metadata={"mode": "sync", "result": payload["result"]},
            )

        job_id = payload.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise OCRProviderAPIError(
                provider=self.name,
                message="No job_id in response",
                raw_response=str(payload),
            )
        logger.info(f"[Reducto] Job created: {job_id}")
        return OCRExternalJob(
            provider=self.name,
            task_id=job_id,
            metadata={"mode": "async"},
        )

    async def wait_external_job(
        self,
        job: OCRExternalJob,
    ) -> OCRExternalJobCompletion:
        self._validate_job(job)
        if not job.external:
            result = job.metadata.get("result")
            if not isinstance(result, dict):
                raise OCRProviderAPIError(
                    provider=self.name,
                    message="Synchronous Reducto result is missing",
                )
            return OCRExternalJobCompletion(
                job=job,
                metadata={"mode": "sync", "result": result},
            )

        async with httpx.AsyncClient(timeout=60.0) as client:
            result = await self._wait_job_result(
                client,
                job.task_id,
                self._get_headers(),
            )
        return OCRExternalJobCompletion(
            job=job,
            metadata={"mode": "async", "result": result},
        )

    async def materialize_external_job(
        self,
        completion: OCRExternalJobCompletion,
    ) -> ParsedDocument:
        self._validate_job(completion.job)
        result = completion.metadata.get("result")
        if not isinstance(result, dict):
            raise OCRProviderAPIError(
                provider=self.name,
                message="Reducto completion result is missing",
            )
        mode = "async" if completion.job.external else "sync"
        return ParsedDocument(
            task_id=completion.job.task_id,
            markdown_content=self._extract_markdown(result),
            metadata={"provider": self.name, "mode": mode},
        )

    def _validate_job(self, job: OCRExternalJob) -> None:
        if job.provider != self.name or not job.task_id:
            raise OCRProviderAPIError(
                provider=self.name,
                message="Reducto external job handle is invalid",
            )

    async def cancel_external_job(self, task_id: str) -> OCRProviderCleanupResult:
        """Verify terminal state; this adapter has no Reducto cancel endpoint."""

        try:
            headers = self._get_headers()
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    f"{self._base_url}/parse/{task_id}",
                    headers=headers,
                )
        except (httpx.HTTPError, OCRProviderConfigError) as exc:
            return OCRProviderCleanupResult(
                provider=self.name,
                task_id=task_id,
                state=OCRProviderCleanupState.FAILED,
                detail=str(exc),
                retryable=isinstance(exc, httpx.HTTPError),
            )

        if response.status_code == 404:
            return OCRProviderCleanupResult(
                provider=self.name,
                task_id=task_id,
                state=OCRProviderCleanupState.COMPLETE,
                detail="Reducto job is absent",
            )
        if response.status_code != 200:
            return OCRProviderCleanupResult(
                provider=self.name,
                task_id=task_id,
                state=OCRProviderCleanupState.FAILED,
                detail=f"Reducto status query failed ({response.status_code})",
                retryable=response.status_code >= 500,
            )
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("response is not an object")
            status = str(payload.get("status") or "").lower()
        except (TypeError, ValueError) as exc:
            return OCRProviderCleanupResult(
                provider=self.name,
                task_id=task_id,
                state=OCRProviderCleanupState.FAILED,
                detail=f"Invalid Reducto status response: {exc}",
                retryable=True,
            )
        if status in {"completed", "failed", "error", "cancelled", "canceled"}:
            return OCRProviderCleanupResult(
                provider=self.name,
                task_id=task_id,
                state=OCRProviderCleanupState.COMPLETE,
                detail=f"Reducto job is terminal ({status})",
            )
        return OCRProviderCleanupResult(
            provider=self.name,
            task_id=task_id,
            state=OCRProviderCleanupState.UNSUPPORTED,
            detail=(
                f"Reducto job is {status or 'non-terminal'}; adapter has no "
                "verified cancellation API"
            ),
            retryable=True,
        )

    async def _poll_job(
        self,
        client: httpx.AsyncClient,
        job_id: str,
        headers: dict,
    ) -> str:
        """
        Poll for job completion.

        Args:
            client: HTTP client
            job_id: Job ID to poll
            headers: Request headers

        Returns:
            Extracted markdown content
        """
        result = await self._wait_job_result(client, job_id, headers)
        return self._extract_markdown(result)

    async def _wait_job_result(
        self,
        client: httpx.AsyncClient,
        job_id: str,
        headers: dict,
    ) -> dict:
        """Poll until Reducto returns a materializable result payload."""

        elapsed = 0

        while elapsed < self._max_wait_time:
            try:
                response = await client.get(
                    f"{self._base_url}/parse/{job_id}",
                    headers=headers,
                )

                if response.status_code != 200:
                    raise OCRProviderAPIError(
                        provider=self.name,
                        message=f"Failed to get job status: {response.text}",
                        status_code=response.status_code,
                    )

                result = response.json()
                status = result.get("status", "").lower()

                if status == "completed":
                    logger.info(f"[Reducto] Job {job_id} completed")
                    completed = result.get("result", {})
                    if not isinstance(completed, dict):
                        raise OCRProviderAPIError(
                            provider=self.name,
                            message="Completed Reducto job has an invalid result",
                            raw_response=str(result),
                        )
                    return completed

                if status in ("failed", "error"):
                    error_msg = result.get("error", "Unknown error")
                    raise OCRProviderAPIError(
                        provider=self.name,
                        message=f"Job failed: {error_msg}",
                        raw_response=str(result),
                    )

                # Still processing
                logger.debug(f"[Reducto] Job {job_id} status: {status}")

            except httpx.HTTPError as e:
                logger.warning(f"[Reducto] Poll error (will retry): {e}")

            await asyncio.sleep(self._poll_interval)
            elapsed += self._poll_interval

        raise OCRProviderTimeoutError(
            provider=self.name,
            message=f"Job {job_id} timed out after {self._max_wait_time}s",
        )

    @staticmethod
    def _format_element(elem: dict) -> str:
        """Format a single Reducto element into markdown."""
        elem_type = elem.get("type", "")
        content = elem.get("content", "") or elem.get("text", "")
        if elem_type == "heading":
            level = elem.get("level", 1)
            return f"{'#' * level} {content}"
        if elem_type == "table":
            return elem.get("markdown", content)
        return content

    def _extract_markdown(self, result: dict) -> str:
        """
        Extract markdown content from Reducto result.

        Reducto returns structured result with different sections.
        We combine them into a single markdown string.
        """
        # Direct markdown field
        if "markdown" in result:
            return result["markdown"]

        # Chunks/pages structure
        if "chunks" in result and isinstance(result["chunks"], list):
            return "\n\n".join(
                chunk.get("text", "") or chunk.get("markdown", "") for chunk in result["chunks"]
            )

        # Pages structure
        if "pages" in result and isinstance(result["pages"], list):
            return "\n\n---\n\n".join(
                page.get("markdown", "") or page.get("text", "") for page in result["pages"]
            )

        # Elements structure
        if "elements" in result and isinstance(result["elements"], list):
            return "\n\n".join(self._format_element(elem) for elem in result["elements"])

        # Text field as fallback
        if "text" in result:
            return result["text"]

        # Last resort: stringify the result
        logger.warning(f"[Reducto] Unexpected result structure: {list(result.keys())}")
        return str(result)

    async def health_check(self) -> bool:
        """Check if Reducto is properly configured."""
        if not self._api_key:
            return False

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                # Try to hit a health or info endpoint
                response = await client.get(
                    f"{self._base_url}/health",
                    headers=self._get_headers(),
                )
                return response.status_code in (
                    200,
                    404,
                )  # 404 means API is up but endpoint doesn't exist
        except Exception as e:
            logger.warning(f"[Reducto] Health check failed: {e}")
            return False
