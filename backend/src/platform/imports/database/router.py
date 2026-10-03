"""Database Import operations; only public_router declares HTTP routes."""

from fastapi import HTTPException

from src.common_schemas import ApiResponse
from src.exceptions import AppException
from src.platform.auth.models import CurrentUser
from src.platform.imports.database.public_schemas import (
    ImportDatabasePreview,
    ImportDatabaseSave,
    ImportDatabaseSaved,
    ImportDatabaseSource,
    ImportDatabaseSourceCreate,
    ImportDatabaseSourceCreated,
    ImportDatabaseTable,
)
from src.platform.imports.database.service import DatabaseImportService


def _source_to_response(source) -> ImportDatabaseSource:
    return ImportDatabaseSource(
        id=source.id,
        name=source.name,
        provider=source.provider,
        project_id=source.project_id,
        is_active=source.is_active,
        last_used_at=source.last_used_at.isoformat() if source.last_used_at else None,
        created_at=(
            source.created_at.isoformat()
            if hasattr(source.created_at, "isoformat")
            else str(source.created_at)
        ),
    )


async def create_source(
    req: ImportDatabaseSourceCreate,
    project_id: str,
    user: CurrentUser,
    service: DatabaseImportService,
):
    try:
        result = await service.create_connection(
            user_id=user.user_id,
            project_id=project_id,
            name=req.name,
            provider=req.provider,
            config={
                "project_url": req.project_url,
                "api_key": req.api_key,
                "key_type": req.key_type,
            },
        )
        return ApiResponse.success(data=ImportDatabaseSourceCreated(
            source=_source_to_response(result["source"]),
            database_info=result["database_info"],
        ))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except AppException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Database source setup failed") from exc


async def list_sources(project_id: str, user: CurrentUser, service: DatabaseImportService):
    sources = service.list_connections(project_id, user.user_id)
    return ApiResponse.success(data=[_source_to_response(source) for source in sources])


async def delete_source(source_id: str, user: CurrentUser, service: DatabaseImportService):
    service.delete_connection(source_id, user.user_id)
    return ApiResponse.success(message="Database source deleted")


async def list_tables(source_id: str, user: CurrentUser, service: DatabaseImportService):
    tables = await service.list_tables(source_id, user.user_id)
    return ApiResponse.success(data=[
        ImportDatabaseTable(name=table.name, type=table.type, columns=table.columns)
        for table in tables
    ])


async def preview_table(
    source_id: str,
    table_name: str,
    limit: int,
    user: CurrentUser,
    service: DatabaseImportService,
):
    try:
        result = await service.preview_table(
            connection_id=source_id, user_id=user.user_id, table=table_name, limit=limit,
        )
        return ApiResponse.success(data=ImportDatabasePreview(
            columns=result.columns,
            rows=result.rows,
            row_count=result.row_count,
            execution_time_ms=result.execution_time_ms,
        ))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except AppException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Preview failed") from exc


async def save_table(
    req: ImportDatabaseSave,
    source_id: str,
    project_id: str,
    user: CurrentUser,
    service: DatabaseImportService,
):
    try:
        result = await service.save_table(
            connection_id=source_id,
            user_id=user.user_id,
            project_id=project_id,
            name=req.name,
            table=req.table,
            limit=req.limit,
        )
        return ApiResponse.success(data=ImportDatabaseSaved(
            import_database_source_id=source_id,
            content_path=result["content_path"],
            row_count=result["row_count"],
        ))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except AppException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Save failed") from exc
