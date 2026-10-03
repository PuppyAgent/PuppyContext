"""
OCR Provider Module

Pluggable OCR service abstraction layer.
Supports multiple OCR providers: MineRU, Reducto, DeepSeek, etc.
"""

from src.infra.file_processing.ocr.base import (
    OCRExternalJob,
    OCRExternalJobCompletion,
    OCRProvider,
    OCRProviderCleanupResult,
    OCRProviderCleanupState,
    ParsedDocument,
    parse_document_with_external_lifecycle,
)
from src.infra.file_processing.ocr.external_cleanup import (
    ExternalIngestCleanup,
    ExternalIngestCleanupResult,
    ExternalIngestCleanupSnapshot,
)
from src.infra.file_processing.ocr.factory import OCRProviderFactory, get_ocr_provider
from src.infra.file_processing.ocr.lifecycle import run_ocr_lifecycle_under_project_lease

__all__ = [
    "ExternalIngestCleanup",
    "ExternalIngestCleanupResult",
    "ExternalIngestCleanupSnapshot",
    "OCRExternalJob",
    "OCRExternalJobCompletion",
    "OCRProvider",
    "OCRProviderCleanupResult",
    "OCRProviderCleanupState",
    "OCRProviderFactory",
    "ParsedDocument",
    "get_ocr_provider",
    "parse_document_with_external_lifecycle",
    "run_ocr_lifecycle_under_project_lease",
]
