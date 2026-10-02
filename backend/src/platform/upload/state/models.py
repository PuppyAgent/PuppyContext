"""
ETL Runtime State Models

Redis runtime state is the source of truth for in-flight ETL tasks.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from src.platform.upload.tasks.models import ETLTaskStatus


class ETLPhase(StrEnum):
    OCR = "ocr"
    POSTPROCESS = "postprocess"
    FINALIZE = "finalize"


class ETLRuntimeState(BaseModel):
    """Runtime state stored in Redis."""

    task_id: str

    user_id: str
    project_id: str
    filename: str
    rule_id: int | None = None

    status: ETLTaskStatus = Field(default=ETLTaskStatus.PENDING)
    phase: ETLPhase = Field(default=ETLPhase.OCR)
    progress: int = Field(default=0, ge=0, le=100)

    attempt_ocr: int = 0
    attempt_postprocess: int = 0

    arq_job_id_ocr: str | None = None
    arq_job_id_postprocess: str | None = None

    artifact_mineru_markdown_key: str | None = None
    provider_name: str | None = None
    provider_task_id: str | None = None  # e.g. MineRU/Reducto task ID
    provider_task_external: bool | None = None
    provider_task_terminal: bool | None = None

    error_code: str | None = None
    error_message: str | None = None
    error_stage: str | None = None  # "mineru" | "postprocess"

    metadata: dict[str, Any] = Field(default_factory=dict)

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def touch(self) -> None:
        self.updated_at = datetime.now(UTC)
