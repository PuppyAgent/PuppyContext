"""Canonical source boundary over the existing one-time Database Import service."""

from fastapi import APIRouter, Depends, Path, Query

from src.common_schemas import ApiResponse
from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser
from src.platform.imports.database import router as operations
from src.platform.imports.database.dependencies import get_database_import_service
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
from src.platform.query_contract import strict_query

router = APIRouter(prefix="/imports/database/sources", tags=["import-database-sources"])


def _fields(value):
    return value.model_dump() if hasattr(value, "model_dump") else dict(value)


def _source(value):
    return ImportDatabaseSource(**_fields(value))


def _reply(response, project=lambda value: value):
    return ApiResponse(
        code=response.code,
        message="Database Import operation completed",
        data=project(response.data) if response.data is not None else None,
    )


@router.post(
    "",
    status_code=201,
    response_model=ApiResponse[ImportDatabaseSourceCreated],
    dependencies=[strict_query("project_id")],
)
async def create_source(
    body: ImportDatabaseSourceCreate,
    project_id: str = Query(min_length=1),
    user: CurrentUser = Depends(get_current_user),
    service: DatabaseImportService = Depends(get_database_import_service),
):
    return _reply(await operations.create_source(body, project_id, user, service))


@router.get(
    "",
    response_model=ApiResponse[list[ImportDatabaseSource]],
    dependencies=[strict_query("project_id")],
)
async def list_sources(
    project_id: str = Query(min_length=1),
    user: CurrentUser = Depends(get_current_user),
    service: DatabaseImportService = Depends(get_database_import_service),
):
    return _reply(
        await operations.list_sources(project_id, user, service),
        lambda rows: [_source(row) for row in rows],
    )


@router.get(
    "/{import_database_source_id}",
    response_model=ApiResponse[ImportDatabaseSource],
    dependencies=[strict_query()],
)
async def get_source(
    import_database_source_id: str = Path(min_length=1),
    user: CurrentUser = Depends(get_current_user),
    service: DatabaseImportService = Depends(get_database_import_service),
):
    source = service.get_connection(import_database_source_id, user.user_id)
    return ApiResponse.success(data=_source(operations._source_to_response(source)))


@router.delete(
    "/{import_database_source_id}", response_model=ApiResponse, dependencies=[strict_query()]
)
async def delete_source(
    import_database_source_id: str = Path(min_length=1),
    user: CurrentUser = Depends(get_current_user),
    service: DatabaseImportService = Depends(get_database_import_service),
):
    return _reply(await operations.delete_source(import_database_source_id, user, service))


@router.get(
    "/{import_database_source_id}/tables",
    response_model=ApiResponse[list[ImportDatabaseTable]],
    dependencies=[strict_query()],
)
async def list_tables(
    import_database_source_id: str = Path(min_length=1),
    user: CurrentUser = Depends(get_current_user),
    service: DatabaseImportService = Depends(get_database_import_service),
):
    return _reply(
        await operations.list_tables(import_database_source_id, user, service),
        lambda rows: [ImportDatabaseTable(**_fields(row)) for row in rows],
    )


@router.get(
    "/{import_database_source_id}/tables/{table_name}/preview",
    response_model=ApiResponse[ImportDatabasePreview],
    dependencies=[strict_query("limit")],
)
async def preview_table(
    import_database_source_id: str = Path(min_length=1),
    table_name: str = Path(min_length=1),
    limit: int = Query(50, ge=1, le=200),
    user: CurrentUser = Depends(get_current_user),
    service: DatabaseImportService = Depends(get_database_import_service),
):
    result = await operations.preview_table(
        import_database_source_id, table_name, limit, user, service
    )
    return _reply(result, lambda value: ImportDatabasePreview(**_fields(value)))


@router.post(
    "/{import_database_source_id}/save",
    response_model=ApiResponse[ImportDatabaseSaved],
    dependencies=[strict_query("project_id")],
)
async def save_table(
    body: ImportDatabaseSave,
    import_database_source_id: str = Path(min_length=1),
    project_id: str = Query(min_length=1),
    user: CurrentUser = Depends(get_current_user),
    service: DatabaseImportService = Depends(get_database_import_service),
):
    return _reply(await operations.save_table(
        body, import_database_source_id, project_id, user, service,
    ))
