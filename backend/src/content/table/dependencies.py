from collections.abc import Callable

from fastapi import Depends, Path

from src.content.table.models import Table
from src.content.table.repository import TableRepositorySupabase
from src.content.table.service import TableService
from src.exceptions import ErrorCode, NotFoundException
from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser
from src.platform.authorization.dependencies import get_authorization_service
from src.platform.authorization.models import ProjectAction
from src.platform.authorization.service import AuthorizationService

_table_repository = None
_table_service = None


def get_table_repository() -> TableRepositorySupabase:
    global _table_repository
    if _table_repository is None:
        _table_repository = TableRepositorySupabase()
    return _table_repository


def get_table_service() -> TableService:
    from src.version_engine.bootstrap.dependencies import build_worker_version_engine_container

    return TableService(
        repo=get_table_repository(),
        repo_manager=build_worker_version_engine_container().repo_manager,
    )


def require_table_action(action: ProjectAction) -> Callable[..., Table]:
    def dependency(
        table_id: str = Path(..., description="Table ID"),
        table_service: TableService = Depends(get_table_service),
        current_user: CurrentUser = Depends(get_current_user),
        authorization: AuthorizationService = Depends(get_authorization_service),
    ) -> Table:
        table = table_service.get_by_id(table_id)
        if table is None or not table.project_id:
            raise NotFoundException(f"Table not found: {table_id}", code=ErrorCode.NOT_FOUND)
        authorization.authorize(table.project_id, current_user.user_id, action)
        table_service.bind_user(current_user.user_id)
        return table

    return dependency


get_verified_table = require_table_action(ProjectAction.CONTENT_READ)
get_writable_table = require_table_action(ProjectAction.CONTENT_WRITE)
