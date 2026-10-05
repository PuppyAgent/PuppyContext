"""Maintenance-only object inventory; never a current-tree or publication facade."""
from dataclasses import dataclass

from src.version_engine.storage.object_store import ObjectStore


class ObjectGcInventory:
    """Expose retention/quarantine capabilities, not legacy read/write authority.

    Legacy roots remain conservative retention inputs until explicit migration
    retirement. Returning them to GC does not make them native current content.
    """
    def __init__(self, history):
        self._history = history

    def native_ref_authority(self):
        return self._history.native_ref_authority()

    def list_object_gc_roots(self):
        return self._history.list_object_gc_roots()

    def list_version_index_roots(self):
        return self._history.list_version_index_roots()

    def list_pending_outbox_roots(self):
        return self._history.list_pending_outbox_roots()

    def list_pending_conflict_roots(self):
        return self._history.list_pending_conflict_roots()

    def list_version_ref_roots(self):
        return self._history.list_version_ref_roots()

    def list_shadow_snapshot_roots(self):
        return self._history.list_shadow_snapshot_roots()

    def sync_object_gc_candidates(self, *args, **kwargs):
        return self._history.sync_object_gc_candidates(*args, **kwargs)

    def remove_object_gc_candidates(self, *args, **kwargs):
        return self._history.remove_object_gc_candidates(*args, **kwargs)


@dataclass
class ObjectGcRepository:
    _project_id: str
    store: ObjectStore
    history: ObjectGcInventory
