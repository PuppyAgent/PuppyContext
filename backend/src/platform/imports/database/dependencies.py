"""Database Import Dependency Injection"""

from fastapi import Depends
from src.infra.supabase.client import SupabaseClient
from src.platform.imports.database.repository import DBConnectionRepository
from src.platform.imports.database.service import DatabaseImportService
from src.platform.authorization.dependencies import get_authorization_service
from src.platform.authorization.service import AuthorizationService


def _get_supabase_client() -> SupabaseClient:
    return SupabaseClient()


def get_db_connection_repository(
    supabase: SupabaseClient = Depends(_get_supabase_client),
) -> DBConnectionRepository:
    return DBConnectionRepository(supabase)


def get_database_import_service(
    repo: DBConnectionRepository = Depends(get_db_connection_repository),
    authorization: AuthorizationService = Depends(get_authorization_service),
) -> DatabaseImportService:
    return DatabaseImportService(repo, authorization)
