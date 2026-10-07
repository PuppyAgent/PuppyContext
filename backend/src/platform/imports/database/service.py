"""Database Import Service - Core business logic"""

import logging
from typing import Any

from src.exceptions import ErrorCode, NotFoundException
from src.platform.authorization.models import ProjectAction
from src.platform.authorization.service import AuthorizationService
from src.platform.imports.database.models import DBConnection
from src.platform.imports.database.repository import DBConnectionRepository
from src.provider.database import get_provider
from src.provider.database.base import QueryResult, TableInfo

logger = logging.getLogger(__name__)


class DatabaseImportService:
    """
    Database Import core service.

    Responsibilities:
    - Connection management (CRUD)
    - List tables / preview table data
    - Save entire table as a Version Engine file
    """

    def __init__(
        self,
        repo: DBConnectionRepository,
        authorization: AuthorizationService,
    ):
        self.repo = repo
        self.authorization = authorization

    # === Connection Management ===

    async def create_connection(
        self,
        user_id: str,
        project_id: str,
        name: str,
        provider: str,
        config: dict,
    ) -> dict[str, Any]:
        """Create a connection and test it immediately."""
        self.authorization.authorize(project_id, user_id, ProjectAction.IMPORT_SOURCE_MANAGE)
        db_provider = get_provider(provider)
        test_result = await db_provider.test_connection(config)

        connection = self.repo.create(
            created_by=user_id,
            project_id=project_id,
            name=name,
            provider=provider,
            config=config,
        )

        return {
            "source": connection,
            "database_info": test_result,
        }

    def get_connection(
        self,
        connection_id: str,
        user_id: str,
        action: ProjectAction = ProjectAction.ACCESS_READ,
    ) -> DBConnection:
        conn = self.repo.get_by_id(connection_id)
        if not conn:
            raise NotFoundException("Database connector not found", code=ErrorCode.NOT_FOUND)
        self.authorization.authorize(conn.project_id, user_id, action)
        return conn

    def list_connections(self, project_id: str, user_id: str) -> list[DBConnection]:
        self.authorization.authorize(project_id, user_id, ProjectAction.ACCESS_READ)
        return self.repo.list_by_project(project_id)

    def delete_connection(self, connection_id: str, user_id: str) -> bool:
        conn = self.get_connection(connection_id, user_id, ProjectAction.IMPORT_SOURCE_MANAGE)
        return self.repo.delete(conn.id)

    # === Table Data ===

    async def list_tables(self, connection_id: str, user_id: str) -> list[TableInfo]:
        """List all tables (including column info)."""
        conn = self.get_connection(connection_id, user_id)
        provider = get_provider(conn.provider)
        tables = await provider.list_tables(conn.config)
        self.repo.update_last_used(conn.id)
        return tables

    async def preview_table(
        self,
        connection_id: str,
        user_id: str,
        table: str,
        limit: int = 50,
    ) -> QueryResult:
        """Preview data from a table."""
        conn = self.get_connection(connection_id, user_id)
        provider = get_provider(conn.provider)

        result = await provider.query_table(
            conn.config,
            table=table,
            limit=limit,
        )

        self.repo.update_last_used(conn.id)
        return result

    # === Save Table Data ===

    async def save_table(
        self,
        connection_id: str,
        user_id: str,
        project_id: str,
        name: str,
        table: str,
        limit: int = 1000,
    ) -> dict[str, Any]:
        """Fetch entire table data and save as a Version Engine file."""
        conn = self.get_connection(connection_id, user_id, ProjectAction.CONTENT_WRITE)
        if conn.project_id != project_id:
            raise NotFoundException("Database connector not found", code=ErrorCode.NOT_FOUND)
        provider = get_provider(conn.provider)

        result = await provider.query_table(
            conn.config,
            table=table,
            limit=limit,
        )

        content_data = {
            "columns": result.columns,
            "rows": result.rows,
            "row_count": result.row_count,
        }

        import json

        from src.platform.project.write_lease import build_leased_worker_write_commands

        commands = build_leased_worker_write_commands(project_id=project_id, user_id=user_id)
        content_bytes = json.dumps(content_data, ensure_ascii=False, indent=2).encode("utf-8")
        file_path = f"{name}.json" if not name.endswith(".json") else name
        await commands.write_bytes(
            project_id,
            file_path,
            content_bytes,
            actor=f"db_connector:{connection_id}",
            message=f"Save table '{table}' from DB connector",
        )

        self.repo.update_last_used(conn.id)

        return {
            "content_path": file_path,
            "row_count": result.row_count,
        }
