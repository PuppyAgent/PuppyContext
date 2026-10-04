"""
SupabaseHistoryManager — PostgreSQL implementation of version history

Storage layout
──────────────
* Commit history table — one row per commit, keyed by (project_id, commit_id).
  ``commit_id`` is the 40-hex SHA-1 of the git ``commit`` object body
  (built by :func:`src.version_engine.write_engine.git_object_format.encode_commit` and stored
  in the project's ``ObjectStore``). PuppyOne and any standard git tool
  derive the same id from the same commit body byte-for-byte.

* Scope state table — per-scope pointer. Holds the latest
  ``scope_hash`` (content fingerprint, CAS target) and
  ``head_commit_id`` (commit pointer derived from the hash). The old
  per-scope integer ``version`` column no longer exists.

* ``projects`` — keeps the materialized project root column only. The old
  per-project integer version column is gone.

Linear ordering
───────────────
Without an integer counter, history is ordered by
``(created_at ASC, commit_id ASC)``. ``commit_id`` acts as a
deterministic tie-breaker when two commits land in the same
microsecond (extremely rare but possible under heavy concurrency).

scope_path canonical form
─────────────────────────
Every public method that accepts a ``scope_path`` normalizes the
value on entry via :func:`_normalize`. The DB-level trigger in
``20260416100000_scope_path_canonical.sql`` enforces the same shape
as a second line of defense.

Interface compatibility
───────────────────────
This class matches the ``HistoryBackend`` surface expected by
the version repository adapter.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from src.infra.supabase.client import SupabaseClient
from src.utils.logger import log_error, log_info, log_warning
from src.version_engine.infrastructure.supabase import safe_data as _safe_data
from src.version_engine.infrastructure.supabase.db_names import (
    COMMIT_HISTORY_TABLE,
    PROJECT_ROOT_HASH_COLUMN,
    PUBLISH_PROJECT_UPDATE_RPC,
    VERSION_INDEX_TABLE,
    VERSION_OUTBOX_TABLE,
)
from src.version_engine.infrastructure.supabase.db_names import (
    SCOPE_STATE_TABLE as DB_SCOPE_STATE_TABLE,
)


class SupabaseHistoryManager:
    """Supabase/PostgreSQL history backend keyed by commit_id."""

    TABLE = COMMIT_HISTORY_TABLE
    SCOPE_STATE_TABLE = DB_SCOPE_STATE_TABLE

    def __init__(self, supabase: SupabaseClient, project_id: str):
        self._client = supabase.client
        self._project_id = project_id

    def native_ref_authority(self):
        from src.version_engine.infrastructure.supabase.ref_authority_repository import (
            RefAuthorityRepository,
        )
        return RefAuthorityRepository(self._client)

    # ── Global Head ──
    #
    # There is no dedicated "global head" column on the projects
    # table — head is always tracked per-scope in the scope-state table.
    # For compatibility with ``ServerRepo`` (which still exposes a
    # project-level head), we return the commit_id of the most
    # recently recorded commit across all scopes.

    def get_head_commit_id(self) -> str:
        # 2-second cache: prevents repeated ORDER BY DESC scans within same request
        import time as _time

        now = _time.monotonic()
        if hasattr(self, "_head_cid_cache") and now - self._head_cid_ts < 2.0:
            return self._head_cid_val
        resp = (
            self._client.table(self.TABLE)
            .select("commit_id")
            .eq("project_id", self._project_id)
            .order("created_at", desc=True)
            .order("commit_id", desc=True)
            .limit(1)
            .execute()
        )
        rows = _safe_data(resp) or []
        val = rows[0]["commit_id"] if rows else ""
        self._head_cid_val = val
        self._head_cid_ts = now
        self._head_cid_cache = True
        return val

    def set_head_commit_id(self, _cid: str) -> None:
        """No-op: project-level head is derived from commit history.

        Kept on the interface because ``ServerRepo`` calls it; but
        the source of truth is the per-scope ``head_commit_id``
        (per-scope). Persisting a second copy on ``projects`` would
        create a global contention point we explicitly want to avoid.
        """

    # ── Global Root Hash (used by root-hash CAS path) ──

    def get_root_hash(self) -> str:
        import time as _time

        now = _time.monotonic()
        if hasattr(self, "_root_hash_cache") and now - self._root_hash_ts < 0.1:
            return self._root_hash_val
        resp = (
            self._client.table("projects")
            .select(PROJECT_ROOT_HASH_COLUMN)
            .eq("id", self._project_id)
            .maybe_single()
            .execute()
        )
        data = _safe_data(resp)
        val = data.get(PROJECT_ROOT_HASH_COLUMN, "") if data else ""
        self._root_hash_val = val
        self._root_hash_ts = now
        self._root_hash_cache = True
        return val

    def initialize_root_hash(self) -> str:
        from src.platform.project.write_lease import active_project_write_lease

        lease = active_project_write_lease(self._project_id)
        result = self._client.rpc('initialize_legacy_version_project_root', {
            'p_project_id': self._project_id,
            'p_lease_id': lease.lease_id if lease is not None else None,
            'p_holder_id': lease.holder_id if lease is not None else None,
        }).execute().data
        if (not isinstance(result, str) or len(result) != 40 or result == '0'*40
                or not set(result) <= set('0123456789abcdef')):
            raise RuntimeError('invalid checked root initialization result')
        if hasattr(self, '_root_hash_cache'):
            del self._root_hash_cache
        return result

    def set_root_hash(self, h: str) -> None:
        self._client.table("projects").update({PROJECT_ROOT_HASH_COLUMN: h}).eq(
            "id", self._project_id
        ).execute()
        # Invalidate cache after write
        if hasattr(self, "_root_hash_cache"):
            del self._root_hash_cache

    # ── Per-Scope Head & Hash ──

    def get_scope_head_commit_id(self, scope_path: str) -> str:
        scope_path = _normalize(scope_path)
        resp = (
            self._client.table(self.SCOPE_STATE_TABLE)
            .select("head_commit_id")
            .eq("project_id", self._project_id)
            .eq("scope_path", scope_path)
            .maybe_single()
            .execute()
        )
        data = _safe_data(resp)
        return data.get("head_commit_id", "") if data else ""

    def set_scope_head_commit_id(self, scope_path: str, cid: str) -> None:
        scope_path = _normalize(scope_path)
        self._upsert_scope_state(scope_path, head_commit_id=cid)

    def get_scope_hash(self, scope_path: str) -> str:
        scope_path = _normalize(scope_path)
        resp = (
            self._client.table(self.SCOPE_STATE_TABLE)
            .select("scope_hash")
            .eq("project_id", self._project_id)
            .eq("scope_path", scope_path)
            .maybe_single()
            .execute()
        )
        data = _safe_data(resp)
        return data.get("scope_hash", "") if data else ""

    def get_scope_state(self, scope_path: str) -> tuple[str, str]:
        """Return ``(scope_hash, head_commit_id)`` with one DB round trip."""
        scope_path = _normalize(scope_path)
        resp = (
            self._client.table(self.SCOPE_STATE_TABLE)
            .select("scope_hash, head_commit_id")
            .eq("project_id", self._project_id)
            .eq("scope_path", scope_path)
            .maybe_single()
            .execute()
        )
        data = _safe_data(resp)
        if not data:
            return "", ""
        return data.get("scope_hash", "") or "", data.get("head_commit_id", "") or ""

    def set_scope_hash(self, scope_path: str, h: str) -> None:
        scope_path = _normalize(scope_path)
        self._upsert_scope_state(scope_path, scope_hash=h)

    def get_all_scope_hashes(self) -> dict[str, str]:
        """Return persisted scope-cache rows for protocol/read views.

        The project root hash is the only write authority. These rows are
        materialized scope views maintained for Git/AP clients and legacy
        compatibility; callers must not treat them as an independent source
        of truth.
        """
        resp = (
            self._client.table(self.SCOPE_STATE_TABLE)
            .select("scope_path, scope_hash")
            .eq("project_id", self._project_id)
            .execute()
        )
        rows = _safe_data(resp) or []
        return {row["scope_path"]: row["scope_hash"] for row in rows if row.get("scope_hash")}

    def _upsert_scope_state(
        self, scope_path: str, *, scope_hash: str | None = None, head_commit_id: str | None = None
    ) -> None:
        """Insert or update specific fields of the scope state row.

        ``scope_path`` is assumed to be already normalized by the caller.
        """
        data: dict = {
            "project_id": self._project_id,
            "scope_path": scope_path,
        }
        if scope_hash is not None:
            data["scope_hash"] = scope_hash
        if head_commit_id is not None:
            data["head_commit_id"] = head_commit_id

        self._client.table(self.SCOPE_STATE_TABLE).upsert(
            data, on_conflict="project_id,scope_path"
        ).execute()

    def cas_update_scope_hash(
        self, scope_path: str, old_hash: str, new_hash: str, head_commit_id: str = ""
    ) -> bool:
        """Compare-and-swap on (scope_hash, head_commit_id).

        Calls the ``cas_update_scope_state`` PL/pgSQL function which
        atomically updates both fields when the pre-image
        ``scope_hash`` matches. Returns ``True`` on success,
        ``False`` on concurrent conflict.
        """
        scope_path = _normalize(scope_path)

        try:
            resp = self._client.rpc(
                "cas_update_scope_state",
                {
                    "p_project_id": self._project_id,
                    "p_scope_path": scope_path,
                    "p_old_hash": old_hash or "",
                    "p_new_hash": new_hash,
                    "p_head_commit_id": head_commit_id or "",
                },
            ).execute()
            data = resp.data
            if isinstance(data, bool):
                return data
            if isinstance(data, list) and len(data) > 0:
                return bool(data[0])
            return False
        except Exception as e:
            log_error(
                f"[CAS] cas_update_scope_state RPC failed for "
                f"scope='{scope_path}': {e}. Deploy the SQL migration first."
            )
            raise RuntimeError(
                "CAS RPC not available — concurrency control requires "
                "the cas_update_scope_state function. Original error: "
                f"{e}"
            ) from e

    def cas_update_root_hash(self, old_hash: str, new_hash: str) -> bool:
        """CAS update the global root hash on the projects table."""
        try:
            resp = self._client.rpc(
                "cas_update_root_hash",
                {
                    "p_project_id": self._project_id,
                    "p_old_hash": old_hash,
                    "p_new_hash": new_hash,
                },
            ).execute()
            data = resp.data
            success = False
            if isinstance(data, bool):
                success = data
            elif isinstance(data, list) and len(data) > 0:
                success = bool(data[0])
            if success and hasattr(self, "_root_hash_cache"):
                del self._root_hash_cache  # Invalidate cache after CAS write
            return success
        except Exception as e:
            log_error(
                f"[CAS] cas_update_root_hash RPC failed: {e}. Deploy the SQL migration first."
            )
            raise RuntimeError(
                "CAS RPC not available — concurrency control requires "
                "the cas_update_root_hash function. Original error: "
                f"{e}"
            ) from e

    def publish_project_update(
        self,
        *,
        old_root_hash: str,
        new_root_hash: str,
        commit_id: str,
        who: str,
        message: str,
        changes: list,
        conflicts: list | None,
        created_at_iso: str,
        audit_event_type: str,
        audit_agent_id: str,
        audit_detail: dict,
        source_channel: str = "",
        policy: str = "",
        base_commit_id: str = "",
        client_commit_id: str = "",
        proposed_tree_id: str = "",
        intent_type: str = "operation",
        scope_path: str = "",
        scope_hash: str = "",
        scope_head_commit_id: str = "",
        expected_scope_head_commit_id: str | None = None,
        storage_measurement: dict | None = None,
    ) -> tuple[bool, int | None]:
        """Atomically publish a root-authoritative transaction.

        ``scope_path``/``scope_hash`` annotate the user-facing access scope
        that produced the commit. The root hash remains the CAS authority;
        the scope row is a cache for Git/AP views.
        """

        rpc_args = {
            "p_project_id": self._project_id,
            "p_old_root_hash": old_root_hash or "",
            "p_new_root_hash": new_root_hash,
            "p_scope_path": _normalize(scope_path),
            "p_scope_hash": scope_hash or new_root_hash,
            "p_scope_head_commit_id": scope_head_commit_id or "",
            "p_expected_scope_head_commit_id": expected_scope_head_commit_id,
            "p_head_commit_id": commit_id,
            "p_who": who,
            "p_message": message or "",
            "p_event_type": audit_event_type,
            "p_changes": changes or [],
            "p_conflicts": _serialize_conflicts(conflicts) if conflicts else None,
            "p_created_at": created_at_iso or "",
            "p_audit_agent_id": audit_agent_id,
            "p_audit_detail": audit_detail or {},
            "p_source_channel": source_channel or "",
            "p_policy": policy or "",
            "p_base_commit_id": base_commit_id or "",
            "p_client_commit_id": client_commit_id or "",
            "p_proposed_tree_id": proposed_tree_id or "",
            "p_intent_type": intent_type or "operation",
        }
        rpc_name = PUBLISH_PROJECT_UPDATE_RPC
        if storage_measurement is not None:
            rpc_name = "publish_version_project_update_with_usage"
            rpc_args.update(
                {
                    "p_org_id": storage_measurement["org_id"],
                    "p_storage_old_value": int(storage_measurement["old_value"]),
                    "p_storage_delta": int(storage_measurement["delta"]),
                    "p_storage_limit": storage_measurement.get("limit"),
                    "p_storage_enforce": bool(storage_measurement.get("enforce")),
                    "p_entitlement_source_revision": storage_measurement.get(
                        "entitlement_source_revision"
                    ),
                }
            )

        if expected_scope_head_commit_id is not None:
            # The pre-repair RPC accepts but ignores this argument for root
            # writes. Require the versioned entrypoint; never fall back to an
            # older schema and silently lose head-CAS protection.
            rpc_name += "_checked"

        try:
            resp = self._client.rpc(rpc_name, rpc_args).execute()
            data = resp.data
            ok, txn_id = _decode_publish_table_result(data)
            if ok:
                for attr in ("_head_cid_cache", "_root_hash_cache"):
                    if hasattr(self, attr):
                        delattr(self, attr)
            return ok, txn_id
        except Exception as e:
            log_error(f"[Publish] {rpc_name} RPC failed: {e}. Deploy the SQL migration first.")
            raise RuntimeError(
                "atomic publish RPC not available — product-root writes "
                f"require {rpc_name}. Original error: "
                f"{e}"
            ) from e

    def record_version_index(
        self,
        *,
        scope_path: str,
        source_commit_id: str,
        source_scope_hash: str,
        project_root_hash: str,
        project_view_commit_id: str,
    ) -> None:
        """Persist the scope-commit → project-view-commit graft mapping."""

        if not source_commit_id or not project_view_commit_id:
            return
        data = {
            "project_id": self._project_id,
            "scope_path": _normalize(scope_path),
            "source_commit_id": source_commit_id,
            "source_scope_hash": source_scope_hash or "",
            "project_root_hash": project_root_hash or "",
            "project_view_commit_id": project_view_commit_id,
        }
        self._client.table(VERSION_INDEX_TABLE).upsert(
            data,
            on_conflict="project_id,source_commit_id",
        ).execute()

    def get_latest_project_view_commit_id(self) -> str:
        resp = (
            self._client.table(VERSION_INDEX_TABLE)
            .select("project_view_commit_id")
            .eq("project_id", self._project_id)
            .order("created_at", desc=True)
            .order("id", desc=True)
            .limit(1)
            .execute()
        )
        rows = _safe_data(resp) or []
        return rows[0].get("project_view_commit_id", "") if rows else ""

    def list_object_gc_roots(self) -> list[str]:
        """Return durable roots that make Git objects reachable.

        Used by the object GC mark phase. The method deliberately gathers
        roots from DB facts rather than from the object store itself: current
        scope refs, the project root, and all recorded commit rows are the
        authoritative publish surface.
        """

        roots: list[str] = []
        data = (
            self._client.table("projects")
            .select(PROJECT_ROOT_HASH_COLUMN)
            .eq("id", self._project_id)
            .maybe_single()
            .execute()
        )
        project = _safe_data(data) or {}
        roots.append(project.get(PROJECT_ROOT_HASH_COLUMN, ""))

        for row in _select_all(
            self._client,
            self.SCOPE_STATE_TABLE,
            "scope_hash, head_commit_id",
            project_id=self._project_id,
        ):
            roots.extend([row.get("scope_hash", ""), row.get("head_commit_id", "")])

        for row in _select_all(
            self._client,
            self.TABLE,
            "commit_id, root_hash, scope_hash",
            project_id=self._project_id,
        ):
            roots.extend(
                [
                    row.get("commit_id", ""),
                    row.get("root_hash", ""),
                    row.get("scope_hash", ""),
                ]
            )

        return roots

    def list_version_index_roots(self) -> list[dict]:
        """Return persistent subtree/history graft roots for object GC."""

        return _select_all(
            self._client,
            VERSION_INDEX_TABLE,
            ("source_commit_id, source_scope_hash, project_root_hash, project_view_commit_id"),
            project_id=self._project_id,
        )

    def list_pending_outbox_roots(self) -> list[dict]:
        """Return unprocessed durable side-effect rows that must pin objects."""

        return _select_all_query(
            lambda: (
                self._client.table(VERSION_OUTBOX_TABLE)
                .select("commit_id, payload")
                .eq("project_id", self._project_id)
                .is_("processed_at", "null")
            )
        )

    def list_pending_conflict_roots(self) -> list[dict]:
        """Return pending conflict metadata that may reference promoted roots."""

        return [
            row.get("metadata") or {}
            for row in _select_all_query(
                lambda: (
                    self._client.table("audit_logs")
                    .select("metadata")
                    .eq("project_id", self._project_id)
                    .like("action", "%conflict_pending%")
                )
            )
        ]

    def list_version_ref_roots(self) -> list[str]:
        rows = _select_all(
            self._client,
            "version_refs",
            "commit_id",
            project_id=self._project_id,
        )
        return [row.get("commit_id", "") for row in rows]

    def list_shadow_snapshot_roots(self) -> list[str]:
        rows = _select_all(
            self._client,
            "local_shadow_snapshots",
            "tree_hash",
            project_id=self._project_id,
        )
        return [row.get("tree_hash", "") for row in rows]

    def sync_object_gc_candidates(
        self,
        object_ids: list[str],
        *,
        now,
        quarantine_seconds: int,
    ) -> list[str]:
        """Atomically reconcile quarantine candidates and return matured ids.

        Objects that became reachable disappear from the registry and are
        therefore automatically recovered before any physical deletion.
        """

        response = self._client.rpc(
            "sync_version_object_gc_candidates",
            {
                "p_project_id": self._project_id,
                "p_object_ids": object_ids,
                "p_now": now.isoformat(),
                "p_quarantine_seconds": max(0, int(quarantine_seconds)),
            },
        ).execute()
        rows = _safe_data(response) or []
        if isinstance(rows, dict):
            rows = [rows]
        return [row.get("object_id", "") for row in rows if row.get("object_id")]

    def register_object_gc_candidates(self, object_ids: list[str]) -> None:
        """Record freshly flushed objects before the publishing CAS.

        Successful objects are removed by the next authoritative mark/sync;
        objects abandoned by a failed CAS retain their original first-seen time.
        """
        if not object_ids:
            return
        self._client.rpc(
            "register_version_object_gc_candidates",
            {
                "p_project_id": self._project_id,
                "p_object_ids": object_ids,
                "p_now": datetime.now(UTC).isoformat(),
            },
        ).execute()

    def remove_object_gc_candidates(self, object_ids: list[str]) -> None:
        if not object_ids:
            return
        self._client.table("version_object_gc_candidates").delete().eq(
            "project_id", self._project_id
        ).in_("object_id", object_ids).execute()

    # ── Scope History Queries ──

    def get_previous_scope_hash(self, scope_path: str, before_commit_id: str = "") -> str:
        """Return the ``scope_hash`` of the commit immediately preceding
        ``before_commit_id`` within this scope.

        When ``before_commit_id`` is empty, returns the current latest
        commit's scope_hash (i.e. "most recent known" for this scope).
        Used by graft conflict detection — we compare this fingerprint
        against the subtree under the global root to decide whether a
        sibling scope concurrently modified our path.
        """
        scope_path = _normalize(scope_path)

        # Locate the reference commit to anchor "before".
        before_entry = None
        if before_commit_id:
            before_entry = self.get_entry(before_commit_id)

        query = (
            self._client.table(self.TABLE)
            .select("scope_hash, created_at, commit_id")
            .eq("project_id", self._project_id)
            .eq("scope_path", scope_path)
            .order("created_at", desc=True)
            .order("commit_id", desc=True)
            .limit(1)
        )

        if before_entry and before_entry.get("created_at"):
            query = query.lt("created_at", before_entry["created_at"])

        try:
            resp = query.execute()
            rows = _safe_data(resp) or []
            if rows and rows[0].get("scope_hash"):
                return rows[0]["scope_hash"]
        except Exception as e:
            log_error(
                f"[VersionHistory] get_previous_scope_hash failed for scope='{scope_path}': {e}"
            )
        return ""

    # ── Record ──

    def record(
        self,
        commit_id: str,
        who: str,
        message: str,
        scope_path: str,
        changes: list,
        conflicts: list | None = None,
        root_hash: str = "",
        scope_hash: str = "",
        created_at_iso: str = "",
    ) -> None:
        """Persist a commit.

        ``commit_id`` is the 40-hex SHA-1 of the git ``commit`` object
        body. The caller is expected to have computed it already
        (handlers / direct_writer do this right before calling us, so
        the value ends up in the audit log too).
        """
        if not commit_id:
            raise ValueError("commit_id is required")

        scope_path = _normalize(scope_path)
        data: dict = {
            "project_id": self._project_id,
            "commit_id": commit_id,
            "root_hash": root_hash,
            "scope_path": scope_path,
            "scope_hash": scope_hash,
            "who": who,
            "message": message or "",
            "changes": (json.dumps(changes) if isinstance(changes, list) else changes),
        }
        if created_at_iso:
            data["created_at"] = created_at_iso
        if conflicts:
            from dataclasses import asdict

            serializable = [
                asdict(c) if hasattr(c, "__dataclass_fields__") else c for c in conflicts
            ]
            data["conflicts"] = json.dumps(serializable)

        self._client.table(self.TABLE).insert(data).execute()
        log_info(f"[VersionHistory] Recorded commit {commit_id[:8]} for project {self._project_id}")

    def record_scope_sync(
        self,
        *,
        scope_path: str,
        committed_commit_id: str,
        current_head_at_start: str,
        source_commit_id: str,
        actor: str,
        source_channel: str = "scope-sync",
    ) -> None:
        """Record a derived scope-view sync as an auditable ledger + audit row.

        When an accepted project-root commit is projected into a NON-source
        scope's head (a derived sync, not a user write), the scope head
        advances but no transaction/audit row is written for it — the change
        is invisible in that scope's audit/transaction stream. This writes one
        ``version_transactions`` row plus one ``audit_logs`` row on the synced
        scope so the propagation is auditable, matching the column shape the
        publish RPC uses for the source scope.

        Attribution uses a NEUTRAL system identity (``actor``, e.g.
        ``puppyone-scope-view``) — never another scope's auth. ``intent_type``
        is recorded as ``operation`` because the version_transactions CHECK has
        no dedicated ``scope_sync`` value; the sync is identified instead by
        ``source_channel='scope-sync'`` + the system actor + ``metadata.kind``.

        Best effort: the scope head has already advanced, so a failure here
        must not break the projection — it is logged and swallowed.
        """
        scope_path = _normalize(scope_path)
        audit_detail = {"kind": "scope_sync", "source_commit_id": source_commit_id}

        txn_id = None
        try:
            from src.version_engine.infrastructure.supabase.transaction_ledger import (
                SupabaseVersionTransactionLedger,
            )

            txn_id = SupabaseVersionTransactionLedger(self._client).insert_version_transaction(
                project_id=self._project_id,
                scope_path=scope_path,
                source_channel=source_channel,
                actor=actor,
                intent_type="operation",
                status="committed",
                current_head_at_start=current_head_at_start or "",
                committed_commit_id=committed_commit_id,
                message=f"Scope-view sync for {source_commit_id}",
                audit_detail=audit_detail,
            )
        except Exception as exc:
            log_warning(
                f"[PostCommit] scope-sync version_transactions insert failed "
                f"for {scope_path!r}: {exc}"
            )

        try:
            row: dict = {
                "action": "scope_sync",
                "operator_type": "system",
                "operator_id": actor,
                "project_id": self._project_id,
                "metadata": audit_detail,
                "canonical_commit_id": committed_commit_id,
                "scope_path": scope_path,
                "path": scope_path,
                "source_channel": source_channel,
                "status": "committed",
            }
            if txn_id is not None:
                row["transaction_id"] = txn_id
            self._client.table("audit_logs").insert(row).execute()
        except Exception as exc:
            log_warning(
                f"[PostCommit] scope-sync audit_logs insert failed for {scope_path!r}: {exc}"
            )

    # ── Query ──

    def get_since(
        self,
        since_commit_id: str,
        scope_path: str | None = None,
        limit: int = 0,
    ) -> list[dict]:
        """Return commits strictly after ``since_commit_id`` in linear
        order.

        Empty ``since_commit_id`` means "from the very beginning".
        The final result is always ordered
        ``(created_at ASC, commit_id ASC)`` — identical to the
        version filesystem backend, so both backends
        yield the same linear view.

        With no anchor, a supplied ``limit`` returns the newest commits (the
        history-page contract). With an anchor, it returns the *oldest next*
        page after that anchor (the catch-up contract). The distinction is
        essential: tail-limiting an anchored query silently loses commits
        when a client was offline for more than one page.
        """
        since_entry = None
        if since_commit_id:
            since_entry = self.get_entry(since_commit_id)
            if since_entry is None:
                # Unknown anchor → safer to return nothing than to
                # leak the full history.
                return []

        query = self._client.table(self.TABLE).select("*").eq("project_id", self._project_id)
        if since_entry:
            query = query.order("created_at").order("commit_id")
        else:
            # Fetch newest-first so LIMIT keeps the tail; we reverse below to
            # present the caller with ASC order.
            query = query.order("created_at", desc=True).order("commit_id", desc=True)

        if since_entry:
            anchor_time = since_entry.get("created_at", "")
            anchor_cid = since_entry.get("commit_id", "")
            # Emulate `(created_at, commit_id) > (anchor_time, anchor_cid)`
            # via the PostgREST `or=` filter.
            if anchor_time:
                query = query.or_(
                    f"created_at.gt.{anchor_time},"
                    f"and(created_at.eq.{anchor_time},"
                    f"commit_id.gt.{anchor_cid})"
                )

        if scope_path:
            query = query.eq("scope_path", _normalize(scope_path))
        if limit > 0:
            query = query.limit(limit)

        resp = query.execute()
        entries = _safe_data(resp) or []
        if not since_entry:
            entries.reverse()
        for entry in entries:
            _parse_json_fields(entry)
        return entries

    def get_entry(self, commit_id: str) -> dict | None:
        if not commit_id:
            return None
        resp = (
            self._client.table(self.TABLE)
            .select("*")
            .eq("project_id", self._project_id)
            .eq("commit_id", commit_id)
            .limit(1)
            .execute()
        )
        rows = _safe_data(resp)
        entry = rows[0] if rows else None
        if entry:
            _parse_json_fields(entry)
        return entry

    def get_entries(self, commit_ids: list[str]) -> list[dict]:
        """Return history metadata for a bounded set of Git commit ids.

        Topological history pages may contain commits that only exist behind
        a named Git ref and therefore have no transaction-history row.  A
        single ``IN`` query lets the read model enrich the rows that do exist
        without issuing one PostgREST request per commit.
        """

        unique_ids = list(dict.fromkeys(commit_id for commit_id in commit_ids if commit_id))
        if not unique_ids:
            return []
        resp = (
            self._client.table(self.TABLE)
            .select("*")
            .eq("project_id", self._project_id)
            .in_("commit_id", unique_ids)
            .execute()
        )
        entries = _safe_data(resp) or []
        for entry in entries:
            _parse_json_fields(entry)
        return entries


def _normalize(scope_path: str) -> str:
    """Canonical scope_path form: strip surrounding ``/``, map None → ``""``.

    Single source of truth for scope_path normalization on the
    application side. The database-level trigger in
    ``20260416100000_scope_path_canonical.sql`` enforces the same
    rule as a second layer of defense.
    """
    return scope_path.strip("/") if scope_path else ""


def _select_all(
    client,
    table: str,
    columns: str,
    *,
    project_id: str,
    page_size: int = 1000,
) -> list[dict]:
    return _select_all_query(
        lambda: client.table(table).select(columns).eq("project_id", project_id),
        page_size=page_size,
    )


def _select_all_query(query_factory, *, page_size: int = 1000) -> list[dict]:
    rows: list[dict] = []
    start = 0
    page_size = max(1, min(int(page_size), 1000))
    while True:
        query = query_factory()
        resp = query.range(start, start + page_size - 1).execute()
        batch = _safe_data(resp) or []
        rows.extend(batch)
        if len(batch) < page_size:
            return rows
        start += page_size


def _serialize_conflicts(conflicts: list | None) -> list:
    from dataclasses import asdict

    return [asdict(c) if hasattr(c, "__dataclass_fields__") else c for c in (conflicts or [])]


def _decode_publish_table_result(data) -> tuple[bool, int | None]:
    """Decode the required TABLE(published BOOLEAN, txn_id BIGINT) RPC shape."""

    if isinstance(data, dict):
        if "published" not in data:
            raise RuntimeError(f"publish RPC returned invalid shape: {data!r}")
        return bool(data.get("published")), data.get("txn_id")
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            if "published" not in first:
                raise RuntimeError(f"publish RPC returned invalid shape: {data!r}")
            return bool(first.get("published")), first.get("txn_id")
    if data in ([], None):
        return False, None
    raise RuntimeError(f"publish RPC returned invalid shape: {data!r}")


def _parse_json_fields(entry: dict) -> None:
    """Parse JSON string fields in a history entry and expose
    ``root`` as an alias of ``root_hash`` for repository compatibility."""
    if isinstance(entry.get("changes"), str):
        entry["changes"] = json.loads(entry["changes"])
    if isinstance(entry.get("conflicts"), str):
        entry["conflicts"] = json.loads(entry["conflicts"])
    # ``audit_detail`` is JSONB on Postgres but PostgREST sometimes
    # hands it back as a string when nested inside a row payload;
    # decode here so the response layer can render it without a
    # type-check.
    if isinstance(entry.get("audit_detail"), str):
        try:
            entry["audit_detail"] = json.loads(entry["audit_detail"])
        except json.JSONDecodeError:
            entry["audit_detail"] = {}
    entry["root"] = entry.get("root_hash", "")
    entry["time"] = entry.get("created_at", "")
