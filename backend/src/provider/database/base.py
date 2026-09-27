"""Base DB Provider - Abstract interface for all database Providers"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class QueryResult:
    """Unified query result."""
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    execution_time_ms: float


@dataclass
class TableInfo:
    """Basic table information."""
    name: str
    type: str = "table"  # table | view
    columns: list[dict[str, str]] = field(default_factory=list)  # [{"name": "id", "type": "uuid"}, ...]


class BaseDBProvider(ABC):
    """
    Abstract base class for database Providers.

    To add a new database:
    1. Inherit this class and implement the methods below
    2. Register in providers/__init__.py
    """

    @abstractmethod
    async def test_connection(self, config: dict) -> dict[str, Any]:
        """
        Test the connection, return connection info.

        Returns:
            {"ok": True, "tables_count": 23}
        Raises:
            Exception on failure
        """

    @abstractmethod
    async def list_tables(self, config: dict) -> list[TableInfo]:
        """List all tables (including column info)."""

    @abstractmethod
    async def query_table(
        self,
        config: dict,
        table: str,
        select: str = "*",
        filters: list[dict] | None = None,
        order: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> QueryResult:
        """
        Query a single table.

        Args:
            config: Connection configuration
            table: Table name
            select: Columns to query, comma-separated (e.g. "id,name,email")
            filters: PostgREST-style filter list [{"column": "status", "op": "eq", "value": "active"}]
            order: Sort order (e.g. "created_at.desc")
            limit: Maximum number of rows
            offset: Offset
        """
