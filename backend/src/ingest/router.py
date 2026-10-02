"""
Ingest Router - Unified entry point API.

This router provides a unified interface for all data ingestion:
- FILE: Local file upload → File Worker (ETL)
- SAAS: SaaS/URL import → durable ImportJob worker

Dual-layer routing architecture:
- Layer 1: mode (raw | ocr_parse)
- Layer 2: file_type (json | text | ocr_needed | binary)
"""

import logging
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Response,
)

from src.ingest.dependencies import get_ingest_service

# Import underlying services for file processing
from src.platform.upload.dependencies import get_etl_service
from src.platform.upload.service import ETLService
from src.ingest.schemas import (
    BatchQueryRequest,
    BatchTaskResponse,
    IngestTaskResponse,
    SourceType,
)
from src.ingest.service import IngestService
from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ingest", tags=["ingest"])


from src.platform.upload.handlers import router as upload_router
from src.platform.imports.legacy_router import router as import_router

router.include_router(upload_router)
router.include_router(import_router)

@router.get("/tasks/{task_id}", response_model=IngestTaskResponse)
async def get_ingest_task(
    task_id: str,
    source_type: SourceType = Query(..., description="Task source type: file, saas, url"),
    service: IngestService = Depends(get_ingest_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    task = await service.get_task(task_id, source_type, current_user.user_id)

    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    return task


@router.post("/tasks/batch", response_model=BatchTaskResponse)
async def batch_get_ingest_tasks(
    request: BatchQueryRequest,
    service: IngestService = Depends(get_ingest_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    tasks = await service.batch_get_tasks(
        tasks=[t.model_dump() for t in request.tasks],
        user_id=current_user.user_id,
    )
    return BatchTaskResponse(tasks=tasks, total=len(tasks))


@router.delete("/tasks/{task_id}")
async def cancel_ingest_task(
    task_id: str,
    source_type: SourceType = Query(..., description="Task source type"),
    service: IngestService = Depends(get_ingest_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    success = await service.cancel_task(task_id, source_type, current_user.user_id)

    if not success:
        raise HTTPException(status_code=404, detail="Task not found or cannot cancel")

    return {"task_id": task_id, "cancelled": True}


# === Health Check ===


@router.get("/health")
async def get_ingest_health(
    response: Response,
    etl_service: ETLService = Depends(get_etl_service),
):
    from src.platform.upload.config import upload_config

    errors: list[str] = []
    file_worker = {
        "status": "ready",
        "queue_size": 0,
        "task_count": 0,
        "worker_count": upload_config.etl_worker_count,
    }

    try:
        file_worker["queue_size"] = etl_service.get_queue_size()
        file_worker["task_count"] = etl_service.get_task_count()
    except Exception as e:
        file_worker["status"] = "unhealthy"
        errors.append(f"file_worker_check_failed: {e}")

    status = "ready" if file_worker["status"] == "ready" else "degraded"
    if status != "ready":
        response.status_code = 503

    return {
        "status": status,
        "file_worker": file_worker,
        "errors": errors,
    }


# === Rules Management Endpoints ===

from src.infra.file_processing.rules.dependencies import get_rule_repository
from src.infra.file_processing.rules.repository_supabase import RuleRepositorySupabase
from src.infra.file_processing.rules.schemas import RuleCreateRequest
from src.platform.upload.processing_schemas import (
    ETLRuleCreateRequest,
    ETLRuleListResponse,
    ETLRuleResponse,
)


@router.get("/rules", response_model=ETLRuleListResponse)
async def list_rules(
    rule_repository: Annotated[RuleRepositorySupabase, Depends(get_rule_repository)],
    limit: int = Query(50, ge=1, le=100, description="Maximum number of rules"),
    offset: int = Query(0, ge=0, description="Number of rules to skip"),
):
    try:
        from src.infra.file_processing.rules.default_rules import get_or_create_default_rule

        get_or_create_default_rule(rule_repository)
    except Exception as e:
        logger.warning(f"Failed to ensure global default rule: {e}")

    rules = rule_repository.list_rules(limit=limit, offset=offset)
    total = rule_repository.count_rules()

    rule_responses = [
        ETLRuleResponse(
            rule_id=rule.rule_id,
            name=rule.name,
            description=rule.description,
            json_schema=rule.json_schema,
            system_prompt=rule.system_prompt,
            postprocess_mode=getattr(rule, "postprocess_mode", None),
            postprocess_strategy=getattr(rule, "postprocess_strategy", None),
            created_at=rule.created_at,
            updated_at=rule.updated_at,
        )
        for rule in rules
    ]

    return ETLRuleListResponse(
        rules=rule_responses,
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/rules", response_model=ETLRuleResponse, status_code=201)
async def create_rule(
    request: ETLRuleCreateRequest,
    rule_repository: Annotated[RuleRepositorySupabase, Depends(get_rule_repository)],
):
    try:
        rule_create = RuleCreateRequest(
            name=request.name,
            description=request.description,
            json_schema=request.json_schema,
            system_prompt=request.system_prompt,
            postprocess_mode=request.postprocess_mode or "llm",
            postprocess_strategy=request.postprocess_strategy,
        )

        rule = rule_repository.create_rule(rule_create)

        logger.info(f"ETL rule created: {rule.rule_id}")

        return ETLRuleResponse(
            rule_id=rule.rule_id,
            name=rule.name,
            description=rule.description,
            json_schema=rule.json_schema,
            system_prompt=rule.system_prompt,
            postprocess_mode=getattr(rule, "postprocess_mode", None),
            postprocess_strategy=getattr(rule, "postprocess_strategy", None),
            created_at=rule.created_at,
            updated_at=rule.updated_at,
        )

    except Exception as e:
        logger.error(f"Error creating rule: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/rules/{rule_id}", response_model=ETLRuleResponse)
async def get_rule(
    rule_id: int,
    rule_repository: Annotated[RuleRepositorySupabase, Depends(get_rule_repository)],
):
    rule = rule_repository.get_rule(str(rule_id))

    if not rule:
        logger.warning(f"Rule not found: {rule_id}")
        raise HTTPException(status_code=404, detail="Rule not found")

    return ETLRuleResponse(
        rule_id=rule.rule_id,
        name=rule.name,
        description=rule.description,
        json_schema=rule.json_schema,
        system_prompt=rule.system_prompt,
        postprocess_mode=getattr(rule, "postprocess_mode", None),
        postprocess_strategy=getattr(rule, "postprocess_strategy", None),
        created_at=rule.created_at,
        updated_at=rule.updated_at,
    )


@router.delete("/rules/{rule_id}", status_code=204)
async def delete_rule(
    rule_id: int,
    rule_repository: Annotated[RuleRepositorySupabase, Depends(get_rule_repository)],
):
    success = rule_repository.delete_rule(str(rule_id))

    if not success:
        logger.warning(f"Rule not found for deletion: {rule_id}")
        raise HTTPException(status_code=404, detail="Rule not found")
