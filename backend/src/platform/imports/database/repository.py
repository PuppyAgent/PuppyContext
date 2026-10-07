"""Database Import source configuration in its independent, Project-owned store.

Never reads or writes Synchronize bindings. The encrypted config envelope is
preserved for migrated sources; provider/name/ownership come from source columns,
not guesses based on a binding's provider, trigger or nested metadata.
"""

from datetime import datetime, timezone

from src.infra.security.crypto import (
    decrypt_db_connection_config,
    encrypt_db_connection_config,
)
from src.infra.supabase.client import SupabaseClient
from src.platform.imports.database.models import DBConnection
from src.utils.id_generator import generate_uuid_v7


class DBConnectionRepository:
    TABLE = "import_database_sources"

    def __init__(self, supabase_client: SupabaseClient):
        self.client = supabase_client.client

    def _query(self):
        return self.client.table(self.TABLE).select("*")

    def _project_org_id(self, project_id: str) -> str | None:
        response = (
            self.client.table("projects")
            .select("org_id")
            .eq("id", project_id)
            .limit(1)
            .execute()
        )
        rows = response.data or []
        if not rows:
            raise ValueError("Database Import source requires an existing Project")
        return rows[0].get("org_id")

    @staticmethod
    def _row_to_model(row: dict) -> DBConnection:
        config = row["config"].get("db_config") or {}
        return DBConnection(
            id=str(row["id"]),
            created_by=row.get("created_by"),
            project_id=str(row["project_id"]),
            name=row["name"],
            provider=row["provider"],
            config=decrypt_db_connection_config(config) if config else {},
            is_active=row["status"] == "active",
            last_used_at=row.get("last_used_at"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def create(
        self,
        created_by: str,
        project_id: str,
        name: str,
        provider: str,
        config: dict,
    ) -> DBConnection:
        response = self.client.table(self.TABLE).insert({
            "id": generate_uuid_v7(),
            "org_id": self._project_org_id(project_id),
            "project_id": project_id,
            "provider": provider,
            "name": name,
            "status": "active",
            "created_by": created_by,
            "config": {"db_config": encrypt_db_connection_config(config)},
        }).execute()
        if not response.data:
            raise RuntimeError("Database Import source creation returned no row")
        return self._row_to_model(response.data[0])

    def get_by_id(self, source_id: str) -> DBConnection | None:
        rows = self._query().eq("id", source_id).execute().data or []
        return self._row_to_model(rows[0]) if rows else None

    def list_by_project(self, project_id: str) -> list[DBConnection]:
        # Inactive classified sources remain discoverable, not silently hidden.
        rows = (
            self._query()
            .eq("project_id", project_id)
            .order("created_at", desc=True)
            .execute()
        ).data or []
        return [self._row_to_model(row) for row in rows]

    def update_last_used(self, source_id: str) -> None:
        self.client.table(self.TABLE).update({
            "last_used_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", source_id).execute()

    def delete(self, source_id: str) -> bool:
        response = self.client.table(self.TABLE).delete().eq("id", source_id).execute()
        return bool(response.data)
