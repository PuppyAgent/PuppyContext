"""Conservative mark-and-sweep GC for Git-native object storage.

Accepted Git pushes can promote immutable objects before the final SQL CAS
publish point. If another writer wins that CAS race, those promoted objects are
safe but unreachable. This module cleans that class of orphan without changing
the concurrency model: live writes still use optimistic CAS; GC runs later from
database-authoritative roots and only sweeps objects outside a retention window.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from src.version_engine.write_engine.git_object_format import decode_object, hash_object
from src.version_engine.write_engine.git_object_graph import object_edges

from src.version_engine.adapters.git.protocol import ZERO_ID


def is_object_id(value: str) -> bool:
    return (isinstance(value, str) and len(value) in (40, 64)
            and set(value) <= set("0123456789abcdef") and set(value) != {"0"})


DEFAULT_RETENTION_SECONDS = 7 * 24 * 60 * 60
_SAMPLE_LIMIT = 20


@dataclass(frozen=True)
class GitObjectGcResult:
    project_id: str
    dry_run: bool
    total_objects: int
    root_count: int
    reachable_count: int
    unreachable_count: int
    eligible_count: int
    deleted_count: int
    kept_young_count: int
    kept_unknown_age_count: int
    kept_protected_descendant_count: int
    quarantined_count: int = 0
    unreachable_bytes: int = 0
    eligible_bytes: int = 0
    deleted_bytes: int = 0
    errors: list[str] = field(default_factory=list)
    deleted_sample: list[str] = field(default_factory=list)
    unreachable_sample: list[str] = field(default_factory=list)
    # Fail-safe flag: True when the reachability closure could not be computed
    # cleanly (a root source or tree object was unreadable), so we refused to
    # delete anything for this project even though ``dry_run`` was off. See the
    # gate in ``run_git_object_gc``.
    sweep_skipped_for_safety: bool = False


def run_git_object_gc(repo, **options) -> GitObjectGcResult:
    """Select coordination from the authoritative DB, never a cache/feature flag."""
    from dataclasses import replace

    control_factory = getattr(getattr(repo, "history", None), "native_ref_authority", None)
    if callable(control_factory):
        try:
            control = control_factory()
            snapshot = control.snapshot(repo._project_id)
            if snapshot is not None:
                if snapshot["authority"] != "native":
                    diagnostic = _run_git_object_gc(repo, **(options | {"dry_run": True}))
                    return replace(diagnostic, dry_run=options.get("dry_run", True),
                                   eligible_count=0, eligible_bytes=0, sweep_skipped_for_safety=True,
                                   errors=[*diagnostic.errors, "shadow repository: collection fenced during migration"])
                from src.version_engine.derived.repository_gc import RepositoryCollector
                return RepositoryCollector(control).run(repo, **options)
        except Exception as exc:
            return GitObjectGcResult(
                getattr(repo, "_project_id", ""), options.get("dry_run", True),
                0, 0, 0, 0, 0, 0, 0, 0, 0,
                errors=[f"repository GC coordination failed: {exc}"], sweep_skipped_for_safety=True,
            )
    return _run_git_object_gc(repo, **options)


def _run_git_object_gc(
    repo,
    *,
    dry_run: bool = True,
    retention_seconds: int = DEFAULT_RETENTION_SECONDS,
    max_delete: int | None = None,
    quarantine_seconds: int = 0,
    now: datetime | None = None,
    additional_roots: tuple[str, ...] = (),
) -> GitObjectGcResult:
    """Collect unreachable objects for one repo and optionally delete them.

    ``retention_seconds`` is the safety valve. Pass ``0`` in tests or manual
    emergency cleanup when the caller deliberately wants immediate sweeping.
    With a positive retention window, objects whose age cannot be determined
    are kept rather than guessed.
    """

    project_id = getattr(repo, "_project_id", "") or ""
    errors: list[str] = []
    # Root discovery, reachability walking, inventory and age metadata are all
    # proof inputs. Any incomplete input makes "unreachable" unprovable and
    # therefore gates both dry-run actionability and destructive sweeping.
    root_errors: list[str] = []
    walk_errors: list[str] = []
    inventory_errors: list[str] = []
    roots = collect_object_gc_roots(repo, errors=root_errors)
    for object_id in additional_roots:
        if not is_object_id(object_id):
            root_errors.append("invalid additional GC root")
        else:
            roots.add(object_id)
    reachable = mark_reachable_objects(repo, roots, errors=walk_errors)

    all_objects = _all_object_ids(repo, errors=inventory_errors)
    metadata = _object_metadata(repo, errors=inventory_errors)
    now = _aware_now(now)

    unreachable = sorted(
        object_id for object_id in all_objects
        if object_id not in reachable
    )

    eligible: list[str] = []
    protected_roots: set[str] = set()
    kept_young = 0
    kept_unknown_age = 0
    for object_id in unreachable:
        # Native capacity inventory can retain a reservation after a failed PUT
        # or post-DELETE SQL outage. Only a fresh canonical absence proof adds
        # this marker; live/unsettled roots are still protected by the walk.
        if ((metadata.get(object_id) or {}).get("capacity_missing") is True or retention_seconds <= 0
                or _object_is_old_enough(object_id, metadata, retention_seconds=retention_seconds, now=now)):
            eligible.append(object_id)
        elif object_id not in metadata:
            kept_unknown_age += 1
            protected_roots.add(object_id)
        else:
            kept_young += 1
            protected_roots.add(object_id)

    protected = mark_reachable_objects(repo, protected_roots, errors=walk_errors)
    protected_descendants = set(eligible).intersection(protected)
    if protected_descendants:
        eligible = [
            object_id for object_id in eligible
            if object_id not in protected_descendants
        ]

    if max_delete is not None:
        eligible = eligible[:max(0, int(max_delete))]

    proof_incomplete = bool(root_errors or walk_errors or inventory_errors)
    errors = root_errors + walk_errors + inventory_errors
    diagnostic_eligible = list(eligible)
    if proof_incomplete:
        # Never expose an incomplete candidate set as executable. Operators can
        # still inspect unreachable_sample and the source-specific errors.
        eligible = []
        errors.append(
            "sweep skipped for safety: reachability closure incomplete; "
            "GC proof inputs incomplete "
            f"({len(diagnostic_eligible)} untrusted candidate(s))"
        )

    candidate_count = len(eligible)
    unreachable_bytes = sum(
        int((metadata.get(object_id) or {}).get("size") or 0)
        for object_id in unreachable
    )
    eligible_bytes = sum(
        int((metadata.get(object_id) or {}).get("size") or 0)
        for object_id in eligible
    )
    if not dry_run and not proof_incomplete and quarantine_seconds > 0:
        sync_candidates = getattr(
            getattr(repo, "history", None),
            "sync_object_gc_candidates",
            None,
        )
        if not callable(sync_candidates):
            proof_incomplete = True
            eligible = []
            errors.append(
                "sweep skipped for safety: durable GC quarantine registry unavailable"
            )
        else:
            try:
                eligible = list(sync_candidates(
                    eligible,
                    now=now,
                    quarantine_seconds=quarantine_seconds,
                ))
            except Exception as exc:  # noqa: BLE001 - deletion must fail closed.
                proof_incomplete = True
                eligible = []
                errors.append(f"sweep skipped for safety: GC quarantine sync failed: {exc}")

    deleted: list[str] = []
    if not dry_run and not proof_incomplete:
        deleted = _delete_eligible_objects(repo, eligible, errors=errors)
        remove_candidates = getattr(
            getattr(repo, "history", None),
            "remove_object_gc_candidates",
            None,
        )
        if deleted and callable(remove_candidates):
            try:
                remove_candidates(deleted)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"remove GC candidates after delete: {exc}")

    return GitObjectGcResult(
        project_id=project_id,
        dry_run=dry_run,
        total_objects=len(all_objects),
        root_count=len(roots),
        reachable_count=len(reachable),
        unreachable_count=len(unreachable),
        eligible_count=candidate_count,
        deleted_count=len(deleted),
        kept_young_count=kept_young,
        kept_unknown_age_count=kept_unknown_age,
        kept_protected_descendant_count=len(protected_descendants),
        quarantined_count=max(0, candidate_count - len(eligible)),
        unreachable_bytes=unreachable_bytes,
        eligible_bytes=eligible_bytes,
        deleted_bytes=sum(
            int((metadata.get(object_id) or {}).get("size") or 0)
            for object_id in deleted
        ),
        errors=errors,
        deleted_sample=deleted[:_SAMPLE_LIMIT],
        unreachable_sample=unreachable[:_SAMPLE_LIMIT],
        sweep_skipped_for_safety=proof_incomplete,
    )


def collect_object_gc_roots(repo, *, errors: list[str] | None = None) -> set[str]:
    """Return DB-authoritative object roots for this repo."""

    out_errors = errors if errors is not None else []
    roots: set[str] = set()

    def add(value: Any) -> None:
        if value in (None, "", ZERO_ID):
            return
        if not isinstance(value, str) or not is_object_id(value):
            out_errors.append(f"invalid persisted GC root value: {value!r}")
            return
        roots.add(value)

    for getter_name in (
        "get_head_commit_id",
        "get_root_hash",
    ):
        try:
            getter = getattr(repo, getter_name, None)
            if callable(getter):
                add(getter())
        except Exception as exc:  # noqa: BLE001
            out_errors.append(f"{getter_name}: {exc}")

    try:
        for scope_path, scope_hash in (repo.get_all_scope_hashes() or {}).items():
            add(scope_hash)
            try:
                add(repo.get_scope_head_commit_id(scope_path))
            except Exception as exc:  # noqa: BLE001
                out_errors.append(f"scope head {scope_path!r}: {exc}")
    except Exception as exc:  # noqa: BLE001
        out_errors.append(f"get_all_scope_hashes: {exc}")

    _add_history_roots(repo, add, out_errors)
    _add_version_index_roots(repo, add, out_errors)
    _add_outbox_roots(repo, add, out_errors)
    _add_pending_conflict_roots(repo, add, out_errors)
    _add_version_ref_roots(repo, add, out_errors)
    _add_shadow_snapshot_roots(repo, add, out_errors)
    return roots


def _add_version_ref_roots(repo, add, errors: list[str]) -> None:
    """Protect commits reachable only from stored branch/tag refs (GAP-3).

    A version_refs row is a durable, fetchable pointer to a promoted
    commit that never advances the scope head, so it is invisible to the
    head/scope/history root sources above. Without this, GC reclaims a
    branch/tag's objects after the retention window and serving that ref
    breaks.
    """
    history = getattr(repo, "history", None)
    getter = getattr(history, "list_version_ref_roots", None)
    if callable(getter):
        try:
            for commit_id in getter():
                add(commit_id)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"version_ref roots: {exc}")


def _add_shadow_snapshot_roots(repo, add, errors: list[str]) -> None:
    """Protect objects referenced by un-promoted shadow snapshots (ISSUE-012).

    Shadow snapshots upload their referenced blobs into the canonical object
    store *before* promotion and may sit un-promoted indefinitely. A pending
    snapshot's ``tree_hash`` is not reachable from any ref, so without adding it
    to the root set GC would reclaim the snapshot's objects once they age past
    the retention window and break ``/promote``. Marking the tree then protects
    the blobs it references transitively.

    DB-only by design: the ``tree_hash`` lives on the ``local_shadow_snapshots``
    row, so this stays synchronous. Reading the S3 manifest's ``blob_hashes``
    from the sync GC root pass is intentionally avoided.
    """
    history = getattr(repo, "history", None)
    getter = getattr(history, "list_shadow_snapshot_roots", None)
    if callable(getter):
        try:
            for tree_hash in getter():
                add(tree_hash)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"shadow_snapshot roots: {exc}")


def mark_reachable_objects(
    repo,
    roots: set[str] | list[str],
    *,
    errors: list[str] | None = None,
) -> set[str]:
    """Walk and verify canonical Git graphs, including nested annotated tags.

    Proven legacy raw roots remain opaque leaves. A damaged Git object must
    never be mistaken for such a leaf: any unproven closure stops sweeping.
    """
    out_errors = errors if errors is not None else []
    reachable: set[str] = set()
    kinds: dict[str, str] = {}
    stack = [
        (object_id, None) for object_id in roots
        if is_object_id(object_id) and object_id != ZERO_ID
    ]

    while stack:
        object_id, expected_type = stack.pop()
        if object_id in reachable:
            if expected_type is not None and kinds.get(object_id) != expected_type:
                out_errors.append(f"walk {object_id}: unexpected object type")
            continue
        reachable.add(object_id)
        try:
            loose = repo.store.get_loose(object_id)
        except Exception as exc:  # noqa: BLE001 - unreadable closure gates sweep.
            out_errors.append(f"read {object_id}: {exc}")
            continue

        try:
            obj_type, body = decode_object(loose)
        except Exception as exc:  # noqa: BLE001
            if expected_type is not None or hashlib.sha1(loose).hexdigest() != object_id:
                out_errors.append(f"decode {object_id}: {exc}")
            # A legacy raw object's id is the hash of its unframed bytes.
            continue

        kinds[object_id] = obj_type
        try:
            object_format = "sha256" if len(object_id) == 64 else "sha1"
            if hash_object(obj_type, body, object_format=object_format) != object_id:
                raise ValueError("object hash mismatch")
            if expected_type is not None and obj_type != expected_type:
                raise ValueError("unexpected object type")
            stack.extend(object_edges(obj_type, body, object_format=object_format))
        except Exception as exc:  # noqa: BLE001
            out_errors.append(f"walk {object_id}: {exc}")

    return reachable


def _child_object_ids(obj_type: str, body: bytes) -> list[str]:
    """Compatibility helper; all graph semantics live in the typed edge parser."""
    return [edge.oid for edge in object_edges(obj_type, body)]


def _add_history_roots(repo, add, errors: list[str]) -> None:
    listed = False
    history = getattr(repo, "history", None)
    list_roots = getattr(history, "list_object_gc_roots", None)
    if callable(list_roots):
        try:
            for value in list_roots():
                add(value)
            listed = True
        except Exception as exc:  # noqa: BLE001
            errors.append(f"list_object_gc_roots: {exc}")

    entries = getattr(history, "_entries", None)
    if entries is not None:
        listed = True
        for entry in list(entries):
            _add_entry_roots(entry, add)

    if listed:
        return

    get_since = getattr(repo, "get_history_since", None)
    if callable(get_since):
        try:
            for entry in get_since("", limit=0):
                _add_entry_roots(entry, add)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"get_history_since: {exc}")


def _add_entry_roots(entry: dict, add) -> None:
    for key in (
        "commit_id",
        "root",
        "root_hash",
        "scope_hash",
        "head_commit_id",
    ):
        add(entry.get(key))


def _add_version_index_roots(repo, add, errors: list[str]) -> None:
    history = getattr(repo, "history", None)
    rows = getattr(history, "_version_index", None)
    if rows is not None:
        for row in list(rows):
            _add_version_index_row(row, add)
        return

    list_rows = getattr(history, "list_version_index_roots", None)
    if callable(list_rows):
        try:
            for row in list_rows():
                if isinstance(row, dict):
                    _add_version_index_row(row, add)
                else:
                    add(row)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"list_version_index_roots: {exc}")


def _add_version_index_row(row: dict, add) -> None:
    for key in (
        "source_commit_id",
        "source_scope_hash",
        "project_root_hash",
        "project_view_commit_id",
    ):
        add(row.get(key))


def _add_outbox_roots(repo, add, errors: list[str]) -> None:
    history = getattr(repo, "history", None)
    list_rows = getattr(history, "list_pending_outbox_roots", None)
    if not callable(list_rows):
        return
    try:
        for row in list_rows():
            if isinstance(row, dict):
                add(row.get("commit_id"))
                payload = row.get("payload") or {}
                if isinstance(payload, dict):
                    _add_nested_roots(payload, add)
            else:
                add(row)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"list_pending_outbox_roots: {exc}")


_PENDING_CONFLICT_EVENT_TYPES = frozenset({
    "conflict_pending",
    "pending_conflict_created",
})


def _add_pending_conflict_roots(repo, add, errors: list[str]) -> None:
    audit = getattr(repo, "audit", None)
    events = getattr(audit, "events", None)
    if events is not None:
        for event in list(events):
            event_type = str(event.get("type", ""))
            # Match canonical event-type tokens, not substrings, so renames
            # like ``conflict_pending_v2`` or ``…_pending_conflict_…`` don't
            # silently flip protection on/off. Add new tokens to the set
            # above when a new pending-conflict event type lands.
            if not any(t in event_type for t in _PENDING_CONFLICT_EVENT_TYPES):
                continue
            detail = event.get("detail") or {}
            if isinstance(detail, dict):
                _add_nested_roots(detail, add)
        return

    history = getattr(repo, "history", None)
    list_rows = getattr(history, "list_pending_conflict_roots", None)
    if callable(list_rows):
        try:
            for row in list_rows():
                if isinstance(row, dict):
                    _add_nested_roots(row, add)
                else:
                    add(row)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"list_pending_conflict_roots: {exc}")


def _add_nested_roots(value: Any, add) -> None:
    if isinstance(value, str):
        # Conflict/outbox payloads contain ordinary strings as well as hashes;
        # only hash-shaped values are declared roots by this recursive scan.
        if is_object_id(value):
            add(value)
    elif isinstance(value, dict):
        for child in value.values():
            _add_nested_roots(child, add)
    elif isinstance(value, list):
        for child in value:
            _add_nested_roots(child, add)


def _all_object_ids(repo, *, errors: list[str]) -> set[str]:
    try:
        return {
            object_id for object_id in repo.store.all_hashes()
            if is_object_id(object_id) and object_id != ZERO_ID
        }
    except Exception as exc:  # noqa: BLE001
        errors.append(f"all_hashes: {exc}")
        return set()


def _object_metadata(repo, *, errors: list[str]) -> dict[str, dict]:
    backend = getattr(repo.store, "_backend", None)
    getter = getattr(backend, "all_hashes_with_metadata", None)
    if not callable(getter):
        return {}
    try:
        return {
            object_id: meta
            for object_id, meta in getter().items()
            if is_object_id(object_id)
        }
    except Exception as exc:  # noqa: BLE001
        errors.append(f"all_hashes_with_metadata: {exc}")
        return {}


def _object_is_old_enough(
    object_id: str,
    metadata: dict[str, dict],
    *,
    retention_seconds: int,
    now: datetime,
) -> bool:
    if retention_seconds <= 0:
        return True
    meta = metadata.get(object_id) or {}
    last_modified = meta.get("last_modified")
    if last_modified is None:
        return False
    if isinstance(last_modified, str):
        try:
            last_modified = datetime.fromisoformat(
                last_modified.replace("Z", "+00:00"),
            )
        except ValueError:
            return False
    if not isinstance(last_modified, datetime):
        return False
    if last_modified.tzinfo is None:
        last_modified = last_modified.replace(tzinfo=timezone.utc)
    return (now - last_modified).total_seconds() >= retention_seconds


def _delete_eligible_objects(repo, eligible: list[str], *, errors: list[str]) -> list[str]:
    """Delete eligible orphans across all physical layouts (GAP-2).

    Bundled objects share a ``.pob`` and can't be removed individually,
    so we first run a whole-bundle sweep that drops only bundles whose
    every member is eligible. Loose and chunked orphans (and any bundled
    object whose bundle wasn't fully dead, which ``delete`` refuses) are
    then handled per object.
    """
    deleted: list[str] = []
    swept_ids: set[str] = set()

    backend = getattr(repo.store, "_backend", None)
    sweep = getattr(backend, "sweep_dead_bundles", None)
    if callable(sweep):
        try:
            _count, swept = sweep(set(eligible))
            swept_ids = set(swept)
            deleted.extend(swept)
        except Exception as exc:  # noqa: BLE001 - GC must continue.
            errors.append(f"sweep_dead_bundles: {exc}")

    for object_id in eligible:
        if object_id in swept_ids:
            continue
        try:
            if _delete_object(repo, object_id):
                deleted.append(object_id)
        except Exception as exc:  # noqa: BLE001 - GC must continue.
            errors.append(f"delete {object_id}: {exc}")
    return deleted


def _delete_object(repo, object_id: str) -> bool:
    backend = getattr(repo.store, "_backend", None)
    delete = getattr(backend, "delete", None)
    if not callable(delete):
        raise RuntimeError("object backend does not expose delete")
    return bool(delete(object_id))


def _aware_now(now: datetime | None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current
