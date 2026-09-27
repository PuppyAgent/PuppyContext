"""
Ingest Router - Unified entry point API.

This router provides a unified interface for all data ingestion:
- FILE: Local file upload → File Worker (ETL)
- SAAS: SaaS/URL import → durable ImportJob worker

Dual-layer routing architecture:
- Layer 1: mode (raw | ocr_parse)
- Layer 2: file_type (json | text | ocr_needed | binary)
"""

import json
import logging

from fastapi import (
    APIRouter,
    Depends,
    Form,
    HTTPException,
)


# Import underlying services for file processing
from src.ingest.schemas import (
    IngestStatus,
    IngestSubmitItem,
    IngestSubmitResponse,
    IngestType,
    SourceType,
)
from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser
from src.platform.imports.dependencies import get_import_job_service
from src.platform.imports.provider import detect_import_provider, suggest_import_name
from src.platform.imports.schemas import ImportJobCreateRequest
from src.platform.imports.service import ImportJobService

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/submit/saas", response_model=IngestSubmitResponse, status_code=202)
async def submit_saas_ingest(
    project_id: str = Form(..., description="Target project ID"),
    url: str = Form(..., description="SaaS or Web URL"),
    name: str | None = Form(None, description="Custom name"),
    crawl_options: str | None = Form(None, description="JSON crawl options for generic web URLs"),
    # Dependencies
    import_service: ImportJobService = Depends(get_import_job_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Submit SaaS/URL ingest as a durable one-time ImportJob.

    This compatibility route no longer runs connector fetch/write inside the
    HTTP request. It creates a backend-owned job and lets the ARQ worker
    perform the import.
    """

    try:
        provider = detect_import_provider(url)
        config = {}
        if name:
            config["name"] = name
        if provider == "url" and crawl_options:
            try:
                parsed_crawl_options = json.loads(crawl_options)
            except json.JSONDecodeError as exc:
                raise ValueError("crawl_options must be valid JSON") from exc
            if not isinstance(parsed_crawl_options, dict):
                raise ValueError("crawl_options must be a JSON object")
            config["crawl_options"] = parsed_crawl_options

        job = await import_service.create(
            ImportJobCreateRequest(
                project_id=project_id,
                source_url=url,
                provider=provider,
                name=name or suggest_import_name(provider, url),
                config=config,
            ),
            current_user.user_id,
        )

        return IngestSubmitResponse(
            items=[
                IngestSubmitItem(
                    task_id=job.id,
                    source_type=SourceType.SAAS if provider != "url" else SourceType.URL,
                    ingest_type=_provider_to_ingest_type(provider),
                    status=IngestStatus.PENDING,
                    path=job.result_path,
                )
            ],
            total=1,
        )

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"SaaS submit failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Import failed: {e}")


def _provider_to_ingest_type(provider: str) -> IngestType:
    mapping = {
        "github": IngestType.GITHUB,
        "notion": IngestType.NOTION,
        "gmail": IngestType.GMAIL,
        "google_drive": IngestType.GOOGLE_DRIVE,
        "google_sheets": IngestType.GOOGLE_SHEETS,
        "google_docs": IngestType.GOOGLE_DOCS,
        "google_calendar": IngestType.GOOGLE_CALENDAR,
        "airtable": IngestType.AIRTABLE,
        "linear": IngestType.LINEAR,
        "url": IngestType.WEB_PAGE,
    }
    return mapping.get(provider, IngestType.WEB_PAGE)


# === Task Query Endpoints ===
