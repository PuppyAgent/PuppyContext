"""Database Import API Router"""

from typing import List
from fastapi import APIRouter, Depends, HTTPException, Path, Query

from src.platform.auth.models import CurrentUser
from src.platform.auth.dependencies import get_current_user
from src.common_schemas import ApiResponse
from src.exceptions import AppException
from src.platform.imports.database.service import DatabaseImportService
from src.platform.imports.database.dependencies import get_database_import_service
from src.platform.imports.database.schemas import (
    CreateConnectionRequest,
    SaveTableRequest,
    ConnectionResponse,
    ConnectionCreatedResponse,
    TableInfoResponse,
    TablePreviewResponse,
    SaveResultResponse,
)

router = APIRouter(
    prefix="/db-connector",
    tags=["db-connector"],
)


def _conn_to_response(conn) -> ConnectionResponse:
    return ConnectionResponse(
        id=conn.id,
        name=conn.name,
        provider=conn.provider,
        project_id=conn.project_id,
        is_active=conn.is_active,
        last_used_at=conn.last_used_at.isoformat() if conn.last_used_at else None,
        created_at=conn.created_at.isoformat() if hasattr(conn.created_at, 'isoformat') else str(conn.created_at),
    )


# === Access Management ===

@router.post(
    "/access",
    response_model=ApiResponse[ConnectionCreatedResponse],
    summary="Create database access (auto-test)",
)
async def create_connection(
    req: CreateConnectionRequest,
    project_id: str = Query(..., description="Project ID"),
    user: CurrentUser = Depends(get_current_user),
    service: DatabaseImportService = Depends(get_database_import_service),
):
    try:
        result = await service.create_connection(
            user_id=user.user_id,
            project_id=project_id,
            name=req.name,
            provider=req.provider,
            config=req.to_config(),
        )
        return ApiResponse.success(
            data=ConnectionCreatedResponse(
                connection=_conn_to_response(result["connection"]),
                database_info=result["database_info"],
            )
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except AppException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Access setup failed")


@router.get(
    "/access",
    response_model=ApiResponse[List[ConnectionResponse]],
    summary="List database connectors for a project",
)
async def list_connections(
    project_id: str = Query(..., description="Project ID"),
    user: CurrentUser = Depends(get_current_user),
    service: DatabaseImportService = Depends(get_database_import_service),
):
    connections = service.list_connections(project_id, user.user_id)
    return ApiResponse.success(data=[_conn_to_response(c) for c in connections])


@router.delete(
    "/access/{connection_id}",
    response_model=ApiResponse,
    summary="Delete database access",
)
async def delete_connection(
    connection_id: str = Path(...),
    user: CurrentUser = Depends(get_current_user),
    service: DatabaseImportService = Depends(get_database_import_service),
):
    service.delete_connection(connection_id, user.user_id)
    return ApiResponse.success(message="Database connector deleted")


# === Table Data ===

@router.get(
    "/access/{connection_id}/tables",
    response_model=ApiResponse[List[TableInfoResponse]],
    summary="List all tables in the database",
)
async def list_tables(
    connection_id: str = Path(...),
    user: CurrentUser = Depends(get_current_user),
    service: DatabaseImportService = Depends(get_database_import_service),
):
    tables = await service.list_tables(connection_id, user.user_id)
    return ApiResponse.success(
        data=[
            TableInfoResponse(name=t.name, type=t.type, columns=t.columns)
            for t in tables
        ]
    )


@router.get(
    "/access/{connection_id}/tables/{table_name}/preview",
    response_model=ApiResponse[TablePreviewResponse],
    summary="Preview table data (first 50 rows)",
)
async def preview_table(
    connection_id: str = Path(...),
    table_name: str = Path(...),
    limit: int = Query(50, ge=1, le=200),
    user: CurrentUser = Depends(get_current_user),
    service: DatabaseImportService = Depends(get_database_import_service),
):
    try:
        result = await service.preview_table(
            connection_id=connection_id,
            user_id=user.user_id,
            table=table_name,
            limit=limit,
        )
        return ApiResponse.success(
            data=TablePreviewResponse(
                columns=result.columns,
                rows=result.rows,
                row_count=result.row_count,
                execution_time_ms=result.execution_time_ms,
            )
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except AppException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Preview failed")


# === Save ===

@router.post(
    "/access/{connection_id}/save",
    response_model=ApiResponse[SaveResultResponse],
    summary="Save entire table as JSON file in version tree",
)
async def save_table(
    req: SaveTableRequest,
    connection_id: str = Path(...),
    project_id: str = Query(..., description="Project ID"),
    user: CurrentUser = Depends(get_current_user),
    service: DatabaseImportService = Depends(get_database_import_service),
):
    try:
        result = await service.save_table(
            connection_id=connection_id,
            user_id=user.user_id,
            project_id=project_id,
            name=req.name,
            table=req.table,
            limit=req.limit,
        )
        return ApiResponse.success(
            data=SaveResultResponse(
                content_path=result["content_path"],
                row_count=result["row_count"],
            )
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except AppException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Save failed")
