"""Internal inventory over native refs and verified private recovery roots."""

from dataclasses import dataclass

from src.version_engine.infrastructure.supabase.ref_authority_repository import (
    RefAuthorityRepository,
)
from src.version_engine.storage.object_store import ObjectStore


class ObjectGcInventory:
    def __init__(self, client, project_id):
        self._control = RefAuthorityRepository(client)
        self._project_id = project_id

    def native_ref_authority(self):
        return self._control

    def list_object_gc_roots(self):
        return self._control.call("get_version_repository_gc_roots", p_project_id=self._project_id)

    def sync_object_gc_candidates(self, object_ids, *, now, quarantine_seconds):
        rows = (
            self._control.call(
                "sync_version_object_gc_candidates",
                p_project_id=self._project_id,
                p_object_ids=object_ids,
                p_now=now.isoformat(),
                p_quarantine_seconds=max(0, int(quarantine_seconds)),
            )
            or []
        )
        if isinstance(rows, dict):
            rows = [rows]
        return [row["object_id"] for row in rows if row.get("object_id")]

    def remove_object_gc_candidates(self, object_ids):
        if object_ids:
            self._control.client.table("version_object_gc_candidates").delete().eq(
                "project_id", self._project_id
            ).in_("object_id", object_ids).execute()


@dataclass
class ObjectGcRepository:
    _project_id: str
    store: ObjectStore
    history: ObjectGcInventory
