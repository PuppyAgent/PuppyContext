"""
ETL Service Dependencies

FastAPI dependency injection for ETL service.
"""

from arq.connections import RedisSettings, create_pool
from fastapi import Depends, Path

from src.infra.file_processing.arq_client import ETLArqClient
from src.infra.file_processing.service import ETLService
from src.infra.file_processing.state.repository import ETLStateRepositoryRedis
from src.infra.file_processing.tasks.models import ETLTask
from src.infra.file_processing.tasks.repository import ETLTaskRepositoryBase, ETLTaskRepositorySupabase
from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser

# Use global variables to store singletons
_etl_task_repository = None
_etl_service = None
_etl_arq_client = None
_etl_arq_pool = None
_etl_state_repo = None


def get_etl_task_repository() -> ETLTaskRepositoryBase:
    """
    Get ETL task repository instance (singleton).

    Returns:
        ETLTaskRepositoryBase instance
    """
    global _etl_task_repository
    if _etl_task_repository is None:
        _etl_task_repository = ETLTaskRepositorySupabase()
    return _etl_task_repository


def get_etl_arq_client() -> ETLArqClient:
    global _etl_arq_client
    if _etl_arq_client is None:
        _etl_arq_client = ETLArqClient()
    return _etl_arq_client


async def get_etl_arq_pool():
    global _etl_arq_pool
    if _etl_arq_pool is None:
        client = get_etl_arq_client()
        settings = RedisSettings.from_dsn(client.redis_url)
        _etl_arq_pool = await create_pool(settings)
    return _etl_arq_pool


async def get_etl_state_repo() -> ETLStateRepositoryRedis:
    global _etl_state_repo
    if _etl_state_repo is None:
        pool = await get_etl_arq_pool()
        _etl_state_repo = ETLStateRepositoryRedis(pool)
    return _etl_state_repo


async def get_etl_service() -> ETLService:
    """
    Get ETL service instance (singleton, async deps ready).
    """
    global _etl_service
    if _etl_service is None:
        arq_client = get_etl_arq_client()
        state_repo = await get_etl_state_repo()
        _etl_service = ETLService(
            task_repository=get_etl_task_repository(),
            arq_client=arq_client,
            state_repo=state_repo,
        )
    return _etl_service


async def get_verified_etl_task(
    task_id: str = Path(..., description="ETL task ID"),
    etl_service: ETLService = Depends(get_etl_service),
    current_user: CurrentUser = Depends(get_current_user),
) -> ETLTask:
    """
    Dependency injection function: get and verify user access to an ETL task.

    This dependency automatically verifies:
    1. Whether the task exists
    2. Whether the task belongs to the current user

    If verification fails, raises NotFoundException.

    Args:
        task_id: ETL task ID (from path parameter)
        etl_service: ETLService instance (via dependency injection)
        current_user: Current user (via dependency injection)

    Returns:
        Verified ETLTask object

    Raises:
        NotFoundException: If task does not exist or user has no permission
    """
    return await etl_service.get_task_status_with_access_check(
        task_id, current_user.user_id
    )
