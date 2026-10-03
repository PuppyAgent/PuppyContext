"""
OCR Provider Base Class

Abstract base class for OCR providers.
All OCR providers (MineRU, Reducto, etc.) must implement this interface.
"""

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


@dataclass
class ParsedDocument:
    """
    Standardized result from OCR processing.

    All OCR providers return this same structure, making it easy
    to swap providers without changing downstream code.
    """

    # Unique task/job ID from the provider (for tracking)
    task_id: str

    # Extracted markdown content (main output)
    markdown_content: str

    # Optional: Local cache directory (if provider downloads files locally)
    cache_dir: str | None = None

    # Optional: Path to the markdown file (if saved locally)
    markdown_path: str | None = None

    # Optional: Provider-specific metadata
    metadata: dict | None = None


@dataclass(frozen=True)
class OCRExternalJob:
    """Durable identity returned as soon as a provider accepts a job.

    ``metadata`` must remain JSON-serializable.  Callers persist this handle
    before waiting so Project deletion can still find an in-flight provider
    job after a worker crash or timeout.
    """

    provider: str
    task_id: str
    external: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def persistence_metadata(self) -> dict[str, Any]:
        """Fields suitable for ETL runtime/task metadata."""

        return {
            "ocr_provider": self.provider,
            "provider_task_id": self.task_id,
            "provider_task_external": self.external,
            "provider_task_terminal": not self.external,
        }


@dataclass(frozen=True)
class OCRExternalJobCompletion:
    """Provider completion payload consumed by materialization."""

    job: OCRExternalJob
    metadata: dict[str, Any] = field(default_factory=dict)


class OCRProviderCleanupState(StrEnum):
    """Verified state of one external provider cleanup attempt."""

    PENDING = "pending"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"
    COMPLETE = "complete"


@dataclass(frozen=True)
class OCRProviderCleanupResult:
    provider: str
    task_id: str
    state: OCRProviderCleanupState
    detail: str | None = None
    retryable: bool = False


@runtime_checkable
class OCRExternalJobProvider(Protocol):
    """Split lifecycle for providers that create durable remote jobs."""

    @property
    def name(self) -> str: ...

    async def create_external_job(
        self,
        file_url: str,
        data_id: str | None = None,
    ) -> OCRExternalJob: ...

    async def wait_external_job(
        self,
        job: OCRExternalJob,
    ) -> OCRExternalJobCompletion: ...

    async def materialize_external_job(
        self,
        completion: OCRExternalJobCompletion,
    ) -> ParsedDocument: ...


ExternalJobCreatedHook = Callable[[OCRExternalJob], Awaitable[None]]
ExternalJobTerminalHook = Callable[[OCRExternalJobCompletion], Awaitable[None]]


class OCRProvider(ABC):
    """
    Abstract base class for OCR providers.

    Each provider implements:
    - parse_document(): Main method to OCR a document from URL

    The provider handles its own:
    - Authentication
    - Task creation/polling
    - Result fetching
    - Error handling
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider name for logging and identification."""

    @abstractmethod
    async def parse_document(
        self,
        file_url: str,
        data_id: str | None = None,
    ) -> ParsedDocument:
        """
        Parse a document and extract text as markdown.

        Args:
            file_url: Presigned URL to the document (PDF, image, etc.)
            data_id: Optional identifier for tracking

        Returns:
            ParsedDocument with extracted markdown content

        Raises:
            OCRProviderError: If parsing fails
        """

    @abstractmethod
    async def health_check(self) -> bool:
        """
        Check if the provider is available and properly configured.

        Returns:
            True if provider is ready, False otherwise
        """

    async def cancel_external_job(self, task_id: str) -> OCRProviderCleanupResult:
        """Cancel and verify a provider job, if this adapter supports it.

        Adapters must only return ``COMPLETE`` after they have verified that
        the remote job is terminal or absent.  The conservative default is
        explicit ``UNSUPPORTED``; it must never be interpreted as success.
        """

        return OCRProviderCleanupResult(
            provider=self.name,
            task_id=task_id,
            state=OCRProviderCleanupState.UNSUPPORTED,
            detail=f"{self.name} adapter has no verified cancellation API",
            retryable=False,
        )


async def parse_document_with_external_lifecycle(
    provider: OCRProvider,
    *,
    file_url: str,
    data_id: str | None = None,
    on_created: ExternalJobCreatedHook | None = None,
    on_terminal: ExternalJobTerminalHook | None = None,
) -> ParsedDocument:
    """Run a split provider lifecycle and persist its handle before waiting.

    Providers without a durable external-job lifecycle retain the legacy
    ``parse_document`` path.  Such providers do not expose a cancellable
    remote handle.
    """

    if not isinstance(provider, OCRExternalJobProvider):
        return await provider.parse_document(file_url=file_url, data_id=data_id)

    job = await provider.create_external_job(file_url=file_url, data_id=data_id)
    if on_created is not None:
        await on_created(job)
    completion = await provider.wait_external_job(job)
    if on_terminal is not None:
        await on_terminal(completion)
    parsed = await provider.materialize_external_job(completion)
    parsed.metadata = {
        **job.persistence_metadata(),
        "provider_task_terminal": True,
        **(parsed.metadata or {}),
    }
    return parsed


class OCRProviderError(Exception):
    """Base exception for OCR provider errors."""

    def __init__(
        self,
        provider: str,
        message: str,
        status_code: int | None = None,
        raw_response: str | None = None,
    ):
        self.provider = provider
        self.message = message
        self.status_code = status_code
        self.raw_response = raw_response
        super().__init__(f"[{provider}] {message}")


class OCRProviderConfigError(OCRProviderError):
    """Raised when provider is not properly configured (e.g., missing API key)."""


class OCRProviderAPIError(OCRProviderError):
    """Raised when provider API returns an error."""


class OCRProviderTimeoutError(OCRProviderError):
    """Raised when provider times out."""
