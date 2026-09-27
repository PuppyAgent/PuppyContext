"""
Post-commit hooks — keep ``repository_scopes`` consistent with tree mutations.

When files/folders are deleted or moved in a PuppyOne project, scopes
that referenced those paths need to follow the change (rename) or get
surfaced as orphaned (delete). Both hooks are best-effort: failures
log and don't propagate.

Also provides ``push_and_finalize`` for in-process agent/sandbox working
copies that need a clone/edit/write-back lifecycle.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import Context
import threading

from src.version_engine.derived.projection import (
    record_project_view_index_for_commit,
    rebuild_project_root_after_commit,
)
from src.version_engine.write_engine.git_commit import (
    build_git_commit,
    shallow_git_parent_or_empty,
)
from src.version_engine.write_engine.git_object_format import (
    MODE_FILE,
    decode_commit,
    decode_tree,
)
from src.version_engine.write_engine.path_utils import normalize_path
from src.utils.logger import log_error, log_info, log_warning


async def push_and_finalize(
    client,
    project_id: str,
    *,
    repo_manager=None,
    modified: dict[str, bytes] | None = None,
    deleted: list[str] | None = None,
    message: str = "",
    who: str | None = None,
) -> dict:
    """Push changes via InProcessVersionClient and run the post-push hook.

    This is the canonical way to push from any async context (agent,
    sandbox, connector). Using this instead of bare client.push()
    guarantees root_hash is grafted after every successful write.
    """
    result = await asyncio.to_thread(
        client.push,
        modified=modified,
        deleted=deleted,
        message=message,
        who=who,
    )

    if result.get("status") == "ok":
        if repo_manager is None:
            from src.version_engine.bootstrap.dependencies import build_worker_version_engine_container
            repo_manager = build_worker_version_engine_container().repo_manager
        try:
            await asyncio.to_thread(
                run_post_push_hook, project_id, repo_manager, result,
            )
        except Exception as e:
            log_warning(f"[PostCommit] hook failed after push: {e}")

    return result


_SUCCESS_STATUSES = frozenset({"ok", "rolled-back"})
_SCOPE_SYNC_RETRIES = 5

# Neutral system identity for derived scope-view commits / sync audit rows.
# A cross-scope projection is NOT a user write, so it is attributed to this
# system actor — never the source scope's auth.
SCOPE_VIEW_ACTOR = "puppyone-scope-view"


def _record_scope_sync_best_effort(
    repo,
    *,
    scope_path: str,
    committed_commit_id: str,
    current_head_at_start: str,
    source_commit_id: str,
) -> None:
    """Leave an auditable transaction + audit row on a scope whose head was
    just advanced by project-root projection. Best-effort: the head already
    moved, so a missing ``record_scope_sync`` backend or a write failure must
    not break the sync loop."""
    record = getattr(repo, "record_scope_sync", None)
    if not callable(record):
        return
    try:
        record(
            scope_path=scope_path,
            committed_commit_id=committed_commit_id,
            current_head_at_start=current_head_at_start or "",
            source_commit_id=source_commit_id,
            actor=SCOPE_VIEW_ACTOR,
        )
    except Exception as exc:  # noqa: BLE001 — derived audit is best-effort
        log_warning(
            f"[PostCommit] scope-sync audit record failed for {scope_path!r}: {exc}"
        )
_PROJECTION_LOCK_REGISTRY: dict[tuple[str, str], threading.RLock] = {}
_PROJECTION_LOCK_REGISTRY_LOCK = threading.Lock()


@contextmanager
def _projection_locks(project_id: str, scope_paths: set[str]):
    """Order derived root/scope projection updates inside one process."""

    normalized = {
        normalize_path(scope_path)
        for scope_path in scope_paths
    }
    if not normalized:
        normalized = {""}
    ordered = sorted(normalized, key=lambda p: (p.count("/"), p))
    locks = [_projection_lock_for(project_id, scope_path) for scope_path in ordered]
    for lock in locks:
        lock.acquire()
    try:
        yield
    finally:
        for lock in reversed(locks):
            lock.release()


def _projection_lock_for(project_id: str, scope_path: str) -> threading.RLock:
    key = (project_id, normalize_path(scope_path))
    with _PROJECTION_LOCK_REGISTRY_LOCK:
        lock = _PROJECTION_LOCK_REGISTRY.get(key)
        if lock is None:
            lock = threading.RLock()
            _PROJECTION_LOCK_REGISTRY[key] = lock
        return lock


def run_post_push_hook(
    project_id: str,
    repo_manager,
    push_result: dict,
    *,
    raise_errors: bool = False,
) -> None:
    """Legacy scope-publish repair hook.

    Current writes publish through ``run_post_project_update_hook`` after a
    root-authoritative transaction. This function exists only for pre-root-first
    in-process/outbox events that may still need a best-effort root visibility
    repair, path index refresh, and notification fan-out. It must not create
    new scope-promote commits or advance scope heads.
    """
    status = push_result.get("status", "")
    if status not in _SUCCESS_STATUSES:
        return

    commit_id = push_result.get("commit_id") or push_result.get("new_commit_id") or ""
    if not commit_id:
        return

    result_for_graft = {**push_result, "commit_id": commit_id}

    try:
        repo = repo_manager.get_server_repo(project_id)
        entry = repo.history.get_entry(commit_id)
        if not entry:
            return

        changes = entry.get("changes", [])
        if isinstance(changes, str):
            import json
            changes = json.loads(changes)

        deleted_paths = [
            c["path"] for c in changes
            if c.get("action") == "delete" or c.get("op") == "deleted"
        ]
        scope_path = (entry.get("scope_path") or "").strip("/")
        if scope_path:
            deleted_paths = [
                f"{scope_path}/{p.strip('/')}" if p.strip("/") else scope_path
                for p in deleted_paths
            ]

        # Move detection — the L4 move op stashes
        # ``{old_path, new_path}`` in ``audit_detail`` so the hook can
        # dispatch ``post_commit_move`` (rename ``repository_scopes`` rows
        # under the old prefix to the new one) instead of treating the
        # delete+put pair as a real delete. Without this, the
        # ``deleted_paths`` filter below would invoke
        # ``post_commit_delete`` and the affected scopes would be
        # left orphaned despite the user just renaming the folder.
        audit_detail = entry.get("audit_detail") or {}
        if isinstance(audit_detail, str):
            try:
                import json as _json
                audit_detail = _json.loads(audit_detail)
            except Exception:
                audit_detail = {}
        move_old_path = ""
        move_new_path = ""
        if isinstance(audit_detail, dict):
            move_old_path = str(audit_detail.get("old_path") or "")
            move_new_path = str(audit_detail.get("new_path") or "")
        is_move = bool(move_old_path and move_new_path and move_old_path != move_new_path)
        if is_move:
            # Strip the delete side of the move from ``deleted_paths``
            # so ``post_commit_delete`` doesn't ALSO fire for the
            # rename's old path — that would race the move's rename
            # of repository_scopes rows and produce inconsistent state.
            move_old_full = (
                f"{scope_path}/{move_old_path.strip('/')}"
                if scope_path else move_old_path.strip("/")
            )
            deleted_paths = [p for p in deleted_paths if p != move_old_full]

        with _projection_locks(project_id, {"", scope_path}):
            _update_global_root(repo, result_for_graft)

            if is_move:
                post_commit_move(project_id, move_old_path, move_new_path)

            if deleted_paths:
                post_commit_delete(project_id, deleted_paths)

            # Refresh the materialised fs_path_index so the next
            # `puppyone fs find` / `stat` query sees the new files (H1).
            # Best-effort: a Supabase blip degrades fs queries to live S3
            # walks, not a write failure.
            _refresh_fs_path_index(repo, project_id, entry, commit_id, scope_path)

        # Fan out commit_update over WebSocket to subscribed clients.
        # Best-effort: a notification failure must not block the
        # commit. We schedule on the running loop if there is one;
        # otherwise (sync-only context) we fire-and-forget via a
        # short-lived loop so producers never wait on listeners.
        _broadcast_commit_update(project_id, entry, changes)
        _warm_git_transport_views(
            repo,
            project_id,
            _scope_commit_git_view_paths(repo, scope_path, changes),
        )
        # Scope-sync: append path-scoped "upstream advanced" events so sandbox
        # sidecars pull lazily (PUP-sync-trigger-architecture, M3/M4).
        _emit_scope_sync_event(project_id, scope_path, changes, commit_id, entry.get("who"))

    except Exception as e:
        log_error(f"[PostCommit] post-push hook failed for project {project_id}: {e}")
        if raise_errors:
            raise


def _emit_scope_sync_event(project_id, scope_path, changes, commit_id, who) -> None:
    """Append path-scoped scope-sync upstream events for this publish (M3/M4).

    Lazy import + fully guarded: scope-sync eventing must never affect a commit.
    """
    try:
        paths = [c.get("path") for c in (changes or []) if isinstance(c, dict) and c.get("path")]
        if not paths:
            return
        from src.platform.scope_sync.service import get_scope_sync_service
        get_scope_sync_service().record_publish(
            project_id=project_id,
            scope_path=(scope_path or "").strip("/"),
            changed_paths=paths,
            head_version=commit_id,
            origin_user=who or None,
        )
    except Exception as exc:  # noqa: BLE001
        log_warning(f"[PostCommit] scope-sync event emit skipped: {exc}")


def _refresh_fs_path_index(
    repo, project_id: str, entry: dict, commit_id: str, scope_path: str,
) -> None:
    """Update the materialised fs_path_index for the just-landed commit.

    Diffs against the previous head of the same scope so we only touch
    rows that actually changed; on first-ever scope write the "previous"
    is empty and every file becomes an insert.
    """

    try:
        from src.version_engine.derived.path_index import (
            refresh_fs_path_index_for_commit,
        )
    except Exception as exc:
        log_warning(f"[PostCommit] fs_path_index import failed: {exc}")
        return

    parents = entry.get("parents") or []
    previous_commit_id = ""
    if isinstance(parents, list) and parents:
        previous_commit_id = parents[0]
    elif entry.get("parent_commit_id"):
        previous_commit_id = entry["parent_commit_id"]

    try:
        refresh_fs_path_index_for_commit(
            repo,
            project_id=project_id,
            commit_id=commit_id,
            scope_path=scope_path,
            previous_commit_id=previous_commit_id,
            actor=entry.get("who", "") or "",
        )
    except Exception as exc:
        log_warning(f"[PostCommit] fs_path_index refresh failed: {exc}")


def run_post_project_update_hook(
    project_id: str,
    repo_manager,
    push_result: dict,
    *,
    raise_errors: bool = False,
) -> None:
    """Finalize a root-authoritative transaction.

    Product/API/Git scoped writes already CAS-updated the canonical root.
    The hook therefore does not rebuild the project root from child
    scopes. Instead it derives child-scope refs from the accepted root
    so scoped Git/AP clients see the new product state without creating
    extra user-visible commits.
    """

    try:
        repo, entry, commit_id, root_hash, changes = _project_root_hook_context(
            project_id, repo_manager, push_result,
        )
        if not entry:
            return

        run_project_root_visibility_barrier(
            project_id, repo_manager, push_result, raise_errors=raise_errors,
        )

        try:
            entry_scope_path = normalize_path(entry.get("scope_path") or "")
            entry_scope_hash = entry.get("scope_hash") or root_hash
            record_project_view_index_for_commit(
                repo=repo,
                entry=entry,
                scope_path=entry_scope_path,
                scope_hash=entry_scope_hash,
                project_root_hash=root_hash,
                source_commit_id=commit_id,
            )
        except Exception as exc:
            log_warning(
                f"[PostCommit] project-root version index update failed "
                f"for commit {commit_id[:12]}: {exc}",
            )

        deleted_paths = [
            c["path"] for c in changes
            if c.get("action") == "delete" or c.get("op") == "deleted"
        ]
        if deleted_paths:
            post_commit_delete(project_id, deleted_paths)

        # Federated grep / search — populate the text index for the
        # paths this commit added or updated. Failure here MUST NOT
        # propagate; the index is a read-side accelerator, not a
        # write-side invariant. See
        # ``docs/proposals/PUP-cloud-grep.md``.
        try:
            from src.infra.search.text_indexer import index_commit_delta
            from src.version_engine.bootstrap.dependencies import (
                build_worker_version_engine_container,
            )
            container = build_worker_version_engine_container()
            ops = container.product_operations()

            def _read_blob(path: str) -> bytes | None:
                try:
                    return ops.read_file(project_id, path)
                except FileNotFoundError:
                    return None
                except Exception as read_err:  # noqa: BLE001
                    log_warning(
                        f"[PostCommit] text indexer read_file({path}) "
                        f"failed: {read_err}"
                    )
                    return None

            index_commit_delta(
                project_id=project_id,
                commit_id=commit_id,
                changes=changes,
                read_blob=_read_blob,
            )
        except Exception as text_idx_err:  # noqa: BLE001
            log_warning(
                f"[PostCommit] text indexer failed for commit "
                f"{commit_id[:12]}: {text_idx_err}"
            )

        changed_paths = [
            normalize_path(c.get("path", ""))
            for c in changes
            if isinstance(c, dict) and c.get("path")
        ]
        _warm_git_transport_views(
            repo,
            project_id,
            {"", *_project_root_affected_scope_paths(repo, changed_paths)},
        )
        _broadcast_commit_update(project_id, entry, changes)
        # Scope-sync: fan out path-scoped "upstream advanced" events (M3/M4).
        # This is the root-first publish path (git push + typed writes), so the
        # changes are project-root-absolute → scope_path="" lets record_publish
        # translate them into each affected scope's coordinates.
        _emit_scope_sync_event(project_id, "", changes, commit_id, entry.get("who"))

    except Exception as e:
        log_error(
            f"[PostCommit] project-root hook failed for project {project_id}: {e}"
        )
        if raise_errors:
            raise


def run_project_root_visibility_barrier(
    project_id: str,
    repo_manager,
    push_result: dict,
    *,
    raise_errors: bool = False,
) -> None:
    """Synchronously expose a product-root commit to affected scope remotes.

    This is the small read-your-write barrier kept on the request path until
    project-root publish can atomically update affected scope refs inside the
    SQL transaction. Heavy/repairable derived work stays in
    ``run_post_project_update_hook`` and the durable outbox path.
    """

    try:
        repo, entry, commit_id, root_hash, changes = _project_root_hook_context(
            project_id, repo_manager, push_result,
        )
        if not entry:
            return
        changed_paths = [
            normalize_path(c.get("path", ""))
            for c in changes
            if isinstance(c, dict) and c.get("path")
        ]
        source_scope_path = normalize_path(entry.get("scope_path") or "")
        affected_scopes = _project_root_affected_scopes(repo, changed_paths)
        with _projection_locks(
            project_id,
            {
                "",
                *{
                    normalize_path(scope.get("path", ""))
                    for scope in affected_scopes
                    if normalize_path(scope.get("path", ""))
                },
            },
        ):
            current_project_root = _current_project_root_hash(repo)
            stale_project_root = False
            if (
                current_project_root
                and root_hash
                and current_project_root != root_hash
            ):
                stale_project_root = True
                log_info(
                    f"[PostCommit] applying stale project-root delta "
                    f"for commit {commit_id[:12]}"
                )
            _sync_child_scope_refs_from_project_root(
                repo=repo,
                previous_project_root_hash=(
                    push_result.get("old_root", "")
                    or _previous_project_root_hash(repo, entry)
                ),
                project_root_hash=root_hash,
                source_commit_id=commit_id,
                created_at_iso=entry.get("created_at") or entry.get("time") or "",
                changed_paths=changed_paths,
                scopes=affected_scopes,
                stale_project_root=stale_project_root,
                source_scope_path=source_scope_path,
            )
    except Exception as e:
        log_error(
            f"[PostCommit] project-root visibility barrier failed "
            f"for project {project_id}: {e}"
        )
        if raise_errors:
            raise


def _project_root_hook_context(project_id: str, repo_manager, push_result: dict):
    status = push_result.get("status", "")
    if status not in _SUCCESS_STATUSES:
        return None, None, "", "", []

    commit_id = push_result.get("commit_id") or push_result.get("new_commit_id") or ""
    root_hash = push_result.get("root", "")
    if not commit_id or not root_hash:
        return None, None, "", "", []

    repo = repo_manager.get_server_repo(project_id)
    entry = repo.history.get_entry(commit_id)
    if not entry:
        return repo, None, commit_id, root_hash, []

    changes = entry.get("changes", [])
    if isinstance(changes, str):
        import json
        changes = json.loads(changes)
    return repo, entry, commit_id, root_hash, changes


def _project_root_affected_scope_paths(repo, changed_paths: list[str]) -> set[str]:
    return {
        normalize_path(scope.get("path", ""))
        for scope in _project_root_affected_scopes(repo, changed_paths)
        if normalize_path(scope.get("path", ""))
    }


def _project_root_affected_scopes(repo, changed_paths: list[str]) -> list[dict]:
    try:
        scopes = repo.scopes.list_all()
    except Exception:
        return []
    affected: list[dict] = []
    for scope in scopes:
        scope_path = normalize_path(scope.get("path", ""))
        if not scope_path:
            continue
        if changed_paths and not _scope_intersects_paths(scope_path, changed_paths):
            continue
        affected.append(scope)
    return affected


def _scope_commit_git_view_paths(repo, scope_path: str, changes: list[dict]) -> set[str]:
    """Git view caches affected by a landed scope commit.

    Scope/root projection is L6 derived work. Once those projections finish,
    warm only the likely affected protocol views so the next Git client does
    not pay the object-copy cost on the request path.
    """

    scope_norm = normalize_path(scope_path)
    full_changed_paths = _full_change_paths(scope_norm, changes)
    paths = {"", scope_norm}
    if scope_norm:
        parts = scope_norm.split("/")
        for index in range(1, len(parts)):
            paths.add("/".join(parts[:index]))

    try:
        scopes = repo.scopes.list_all()
    except Exception:
        scopes = []
    for scope in scopes:
        candidate = normalize_path(scope.get("path", ""))
        if candidate in paths:
            continue
        if full_changed_paths and _scope_intersects_paths(candidate, full_changed_paths):
            paths.add(candidate)
    return paths


def _full_change_paths(scope_path: str, changes: list[dict]) -> list[str]:
    full_paths: list[str] = []
    for change in changes:
        if not isinstance(change, dict):
            continue
        path = normalize_path(str(change.get("path") or ""))
        if not path:
            if scope_path:
                full_paths.append(scope_path)
            continue
        full_paths.append(f"{scope_path}/{path}" if scope_path else path)
    return full_paths


def _warm_git_transport_views(
    repo,
    project_id: str,
    scope_paths: set[str],
) -> None:
    """Advance derived Git protocol caches for the given logical scopes."""

    normalized = {normalize_path(path) for path in scope_paths}
    if not normalized:
        normalized = {""}
    rows: list[dict] = []
    try:
        rows = repo.scopes.list_all()
    except Exception:
        rows = []

    views: dict[tuple[str, tuple[str, ...]], list[str]] = {}
    if "" in normalized:
        views[("", ())] = []
    for scope in rows:
        path = normalize_path(scope.get("path", ""))
        if path not in normalized:
            continue
        excludes = tuple(sorted(normalize_path(item) for item in (scope.get("exclude") or [])))
        views.setdefault((path, excludes), list(excludes))

    # Project view state may exist before any optional repository Scope exists in
    # older fixtures. Keep the root/project Git remote warm in that case too.
    if "" in normalized:
        views.setdefault(("", ()), [])

    if not views:
        return

    try:
        from src.version_engine.derived.git_transport_cache import (
            warm_git_transport_view,
        )
    except Exception as exc:
        log_warning(f"[PostCommit] git transport cache warm import failed: {exc}")
        return

    warmed = 0
    for (path, _exclude_key), excludes in sorted(views.items()):
        try:
            warm_git_transport_view(repo, path, excludes)
            warmed += 1
        except Exception as exc:
            log_warning(
                f"[PostCommit] git transport cache warm failed "
                f"project={project_id} scope={path!r}: {exc}",
            )
    if warmed:
        log_info(
            f"[PostCommit] warmed {warmed} git transport view(s) "
            f"project={project_id}",
        )


def schedule_post_push_hook(project_id: str, repo_manager, push_result: dict) -> None:
    """Run post-commit projection work off the user request path.

    The accepted scope commit/head/history/audit have already been published
    atomically. Project-root grafts and Git project-view commits are derived
    projections, so AP-FS and Git pushes should not wait on their S3/DB round
    trips. The durable outbox remains the repair path if this best-effort
    background execution fails or the process exits before it completes.
    """

    _schedule_post_commit_hook(
        project_id,
        repo_manager,
        push_result,
        run_post_push_hook,
        label="scope projection",
    )


def schedule_post_project_update_hook(
    project_id: str,
    repo_manager,
    push_result: dict,
) -> None:
    """Run product-root derived work off the user request path."""

    _schedule_post_commit_hook(
        project_id,
        repo_manager,
        push_result,
        run_post_project_update_hook,
        label="project-root projection",
    )


def _schedule_post_commit_hook(
    project_id: str,
    repo_manager,
    push_result: dict,
    hook_fn,
    *,
    label: str,
) -> None:
    commit_id = push_result.get("commit_id") or push_result.get("new_commit_id") or ""
    if not commit_id:
        return

    def _run() -> None:
        try:
            hook_fn(
                project_id,
                repo_manager,
                push_result,
                raise_errors=True,
            )
            try:
                from src.version_engine.derived.outbox import (
                    complete_version_outbox_for_commit,
                )

                complete_version_outbox_for_commit(project_id, commit_id)
            except Exception as exc:
                log_warning(
                    f"[PostCommit] could not complete outbox for "
                    f"{commit_id[:12]}: {exc}",
                )
        except Exception as exc:
            log_error(
                f"[PostCommit] async {label} failed for project "
                f"{project_id} commit={commit_id[:12]}: {exc}",
            )

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(asyncio.to_thread(_run), context=Context())
    except RuntimeError:
        thread = threading.Thread(
            target=_run,
            name=f"post-commit-{commit_id[:12]}",
            daemon=True,
        )
        thread.start()


def _broadcast_commit_update(project_id: str, entry: dict, changes: list[dict]) -> None:
    """Fire a ``commit_update`` event for connected WebSocket listeners.

    The actual fanout is asynchronous; we just schedule it and return.
    Errors are logged at warning level — the push is already durable
    in the DB, so a flaky listener stream must not propagate up.
    """
    try:
        from src.version_engine.derived.notifications import NotificationManager
        manager = NotificationManager.get()
        # ``pusher_client_id`` came in via the request header
        # ``X-PuppyOne-Client-Id`` (when present) and was stashed into
        # ``audit_detail`` by the L1 router. NotificationManager uses
        # it to suppress echo to the exact tab/device that fired the
        # write while still echoing to that user's other devices.
        audit_detail = entry.get("audit_detail") or {}
        pusher_client_id = ""
        if isinstance(audit_detail, dict):
            pusher_client_id = str(audit_detail.get("pusher_client_id") or "")
        coro = manager.broadcast_commit_update(
            project_id=project_id,
            scope_path=(entry.get("scope_path") or ""),
            commit_id=entry.get("commit_id", ""),
            pushed_by=entry.get("who", ""),
            message=entry.get("message", ""),
            scope_hash=entry.get("scope_hash", ""),
            changes=changes,
            pusher_client_id=pusher_client_id,
        )
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(coro)
        except RuntimeError:
            # Sync caller (e.g. ARQ worker) — run to completion in a
            # transient loop so the broadcast actually happens.
            asyncio.run(coro)
    except Exception as e:
        log_warning(f"[PostCommit] broadcast_commit_update failed: {e}")


def _update_global_root(repo, push_result: dict) -> None:
    """Delegate to application/root_projection.

    The graft + CAS retry algorithm and the project-view index update both
    live in application/root_projection now (per
    docs/architecture/07-version-engine-supplement.md §4: graft is an
    application-layer primitive, not a service-layer one). This wrapper
    is kept only because run_post_push_hook and the version-outbox
    worker still call it by name.
    """

    if not rebuild_project_root_after_commit(repo, push_result):
        commit_id = push_result.get("commit_id") or push_result.get("new_commit_id") or ""
        raise RuntimeError(
            "project-root projection did not publish"
            + (f" for commit {commit_id[:12]}" if commit_id else "")
        )


def _sync_child_scope_refs_from_project_root(
    *,
    repo,
    previous_project_root_hash: str = "",
    project_root_hash: str,
    source_commit_id: str,
    created_at_iso: str,
    changed_paths: list[str] | None = None,
    scopes: list[dict] | None = None,
    stale_project_root: bool = False,
    source_scope_path: str = "",
) -> None:
    """Derive scoped access-point refs from an accepted project root."""

    if scopes is None:
        try:
            scopes = repo.scopes.list_all()
        except Exception as exc:
            log_warning(f"[PostCommit] could not list repo scopes for root sync: {exc}")
            return

    for scope in scopes:
        scope_path = normalize_path(scope.get("path", ""))
        if not scope_path:
            continue
        if scope_path == normalize_path(source_scope_path):
            # The source scope row is part of the accepted publish transaction.
            # Re-deriving it here can silently replace the Git-visible client
            # commit with a synthetic scope-view commit, breaking normal
            # fast-forward pulls for native Git remotes.
            continue
        if changed_paths is not None and not _scope_intersects_paths(
            scope_path,
            changed_paths,
        ):
            continue

        for attempt in range(_SCOPE_SYNC_RETRIES):
            current_hash, current_head = _scope_state(repo, scope_path)
            target_hash = _merge_project_root_delta_into_child_scope(
                repo=repo,
                scope_path=scope_path,
                previous_project_root_hash=previous_project_root_hash,
                project_root_hash=project_root_hash,
                current_scope_hash=current_hash,
                changed_paths=changed_paths or [],
                stale_project_root=stale_project_root,
            )

            if current_hash == target_hash:
                break

            if not target_hash:
                if _cas_or_set_scope_state(repo, scope_path, current_hash, "", ""):
                    log_info(
                        f"[PostCommit] cleared child scope {scope_path!r} after "
                        f"project-root commit {source_commit_id[:12]}"
                    )
                    break
            else:
                parent = shallow_git_parent_or_empty(repo, current_head) if current_head else ""
                scope_commit_id = build_git_commit(
                    repo,
                    tree_sha=target_hash,
                    parent_sha=parent,
                    who=SCOPE_VIEW_ACTOR,
                    message=f"Puppyone scope view for {source_commit_id}",
                    created_at_iso=created_at_iso,
                    validate_parent_graph=False,
                )
                if _cas_or_set_scope_state(
                    repo, scope_path, current_hash, target_hash, scope_commit_id,
                ):
                    log_info(
                        f"[PostCommit] synced child scope {scope_path!r} from "
                        f"project-root commit {source_commit_id[:12]}"
                    )
                    # Leave an auditable trail on the synced scope: its head
                    # just advanced via a derived projection (not a user
                    # write), so without this the change is invisible in this
                    # scope's transaction/audit stream. Attribution is a
                    # neutral system identity — never the source scope's auth.
                    _record_scope_sync_best_effort(
                        repo,
                        scope_path=scope_path,
                        committed_commit_id=scope_commit_id,
                        current_head_at_start=current_head,
                        source_commit_id=source_commit_id,
                    )
                    break

            if attempt == _SCOPE_SYNC_RETRIES - 1:
                log_warning(
                    f"[PostCommit] skipped stale child-scope sync for "
                    f"{scope_path!r} from project-root commit "
                    f"{source_commit_id[:12]}"
                )


def _scope_state(repo, scope_path: str) -> tuple[str, str]:
    try:
        return repo.get_scope_state(scope_path)
    except Exception:
        pass
    try:
        return (
            repo.get_scope_hash(scope_path),
            repo.get_scope_head_commit_id(scope_path),
        )
    except Exception:
        return "", ""


def _current_project_root_hash(repo) -> str:
    try:
        return repo.get_root_hash() or ""
    except Exception:
        pass
    try:
        return repo.history.get_root_hash() or ""
    except Exception:
        return ""


def _previous_project_root_hash(repo, entry: dict) -> str:
    commit_id = entry.get("commit_id") or ""
    if not commit_id:
        return ""
    try:
        obj_type, body = repo.store.get_object(commit_id)
        if obj_type != "commit":
            return ""
        info = decode_commit(body)
        parents = info.get("parents") or []
        if not parents:
            return ""
        parent_type, parent_body = repo.store.get_object(parents[0])
        if parent_type != "commit":
            return ""
        parent_info = decode_commit(parent_body)
        return parent_info.get("tree", "") or ""
    except Exception:
        return ""


def _merge_project_root_delta_into_child_scope(
    *,
    repo,
    scope_path: str,
    previous_project_root_hash: str,
    project_root_hash: str,
    current_scope_hash: str,
    changed_paths: list[str],
    stale_project_root: bool = False,
) -> str:
    """Apply the project-root delta to one child scope without losing child edits.

    Product/root writes are parent-authoritative: when root and a child scope
    touch the same relative path, the root version wins. Independent child
    paths are preserved so concurrent Git pushes and frontend saves converge
    instead of clobbering each other.
    """

    new_subtree_hash = _tree_hash_at_path(repo.store, project_root_hash, scope_path)
    if (
        not stale_project_root
        and _project_root_replaces_scope(scope_path, changed_paths)
    ):
        return new_subtree_hash

    from src.version_engine.write_engine.tree import tree_to_flat, tree_path_modes
    from src.version_engine.write_engine.tree_objects import build_tree_from_blob_ids

    # GAP-5: run the 3-way merge at the BLOB OBJECT-ID level, never on blob
    # bytes. ``tree_to_flat`` returns ``{path: blob_oid}`` by reading only
    # the (small, cached) tree objects; it does not download file content.
    # Because the store is content-addressed, two paths have identical bytes
    # iff their blob oids are equal, so OID comparison is exactly equivalent
    # to the old byte comparison — but a large scope no longer pulls every
    # blob (×3 subtrees) onto the request path. The rebuild references the
    # existing blob oids directly instead of re-uploading bytes.
    #
    # Blob MODES are merged alongside oids (A1-1): the merged mode follows
    # the same source as the merged oid, so an executable/symlink kept from
    # the child scope or taken from the parent isn't downgraded to 100644.
    old_subtree_hash = _tree_hash_at_path(
        repo.store,
        previous_project_root_hash,
        scope_path,
    ) if previous_project_root_hash else ""
    old_oids = tree_to_flat(repo.store, old_subtree_hash) if old_subtree_hash else {}
    new_oids = tree_to_flat(repo.store, new_subtree_hash) if new_subtree_hash else {}
    current_oids = tree_to_flat(repo.store, current_scope_hash) if current_scope_hash else {}
    new_modes = tree_path_modes(repo.store, new_subtree_hash) if new_subtree_hash else {}
    merged_modes = tree_path_modes(repo.store, current_scope_hash) if current_scope_hash else {}

    merged_oids = dict(current_oids)
    for rel_path in set(old_oids) | set(new_oids):
        before = old_oids.get(rel_path)
        after = new_oids.get(rel_path)
        if before == after:
            continue
        if stale_project_root and current_oids.get(rel_path) != before:
            continue
        if after is None:
            merged_oids.pop(rel_path, None)
            merged_modes.pop(rel_path, None)
        else:
            merged_oids[rel_path] = after
            merged_modes[rel_path] = new_modes.get(rel_path, MODE_FILE)

    return (
        build_tree_from_blob_ids(repo.store, merged_oids, modes=merged_modes)
        if merged_oids else ""
    )


def _set_scope_state(repo, scope_path: str, scope_hash: str, head_commit_id: str) -> None:
    history = getattr(repo, "history", repo)
    history.set_scope_hash(scope_path, scope_hash)
    history.set_scope_head_commit_id(scope_path, head_commit_id)


def _cas_or_set_scope_state(
    repo,
    scope_path: str,
    old_scope_hash: str,
    new_scope_hash: str,
    head_commit_id: str,
) -> bool:
    """CAS child-scope projection writes when the repo supports it.

    Project-root commits derive child scope refs as a projection. A stale
    projection must never overwrite a newer scoped Git/AP head, so production
    repositories use the same scope CAS primitive as user writes. Tiny test
    doubles that predate the CAS facade fall back to direct assignment.
    """

    cas = getattr(repo, "cas_update_scope", None)
    if callable(cas):
        updated = bool(cas(
            scope_path,
            old_scope_hash or "",
            new_scope_hash or "",
            head_commit_id or "",
        ))
        if updated and not new_scope_hash and not head_commit_id:
            try:
                repo.set_scope_head_commit_id(scope_path, "")
            except Exception:
                pass
        return updated
    _set_scope_state(repo, scope_path, new_scope_hash, head_commit_id)
    return True


def _tree_hash_at_path(store, root_hash: str, path: str) -> str:
    if not root_hash:
        return ""
    current = root_hash
    for part in [p for p in normalize_path(path).split("/") if p]:
        try:
            obj_type, body = store.get_object(current)
        except Exception:
            return ""
        if obj_type != "tree":
            return ""
        match = next((entry for entry in decode_tree(body) if entry.name == part), None)
        if match is None or not match.is_dir:
            return ""
        current = match.sha1_hex
    return current


def _scope_intersects_paths(scope_path: str, changed_paths: list[str]) -> bool:
    """Return true when a root-level change can affect a child scope."""

    scope_norm = normalize_path(scope_path)
    if not scope_norm:
        return True
    for changed in changed_paths:
        changed_norm = normalize_path(changed)
        if not changed_norm:
            return True
        if changed_norm == scope_norm:
            return True
        if changed_norm.startswith(scope_norm + "/"):
            return True
        if scope_norm.startswith(changed_norm + "/"):
            return True
    return False


def _project_root_replaces_scope(scope_path: str, changed_paths: list[str]) -> bool:
    """Return true when the root operation replaced/deleted the scope boundary."""

    scope_norm = normalize_path(scope_path)
    if not scope_norm:
        return True
    for changed in changed_paths:
        changed_norm = normalize_path(changed)
        if not changed_norm:
            return True
        if changed_norm == scope_norm:
            return True
        if scope_norm.startswith(changed_norm + "/"):
            return True
    return False


def post_commit_delete(project_id: str, deleted_paths: list[str]) -> None:
    """After deleting paths from the tree, surface any ``repository_scopes`` rows
    whose path is now orphaned. Best-effort: failures log and don't
    propagate.
    """
    if not deleted_paths:
        return
    _post_commit_delete_repository_scopes(project_id, deleted_paths)


def _post_commit_delete_repository_scopes(
    project_id: str, deleted_paths: list[str],
) -> None:
    """New: log scopes whose path falls under a deleted subtree.

    We deliberately DON'T auto-rewrite repository_scopes.path because the
    target path is an explicit user-owned boundary.

    AccessSurface rows attached to the orphaned scope keep their FK; the user
    sees the orphaned scope in /scopes and decides what to do. Logging
    here gives ops a forensics trail."""
    try:
        from src.repo.scope_repository import RepositoryScopeRepository
        scopes = RepositoryScopeRepository().list_by_project(project_id)
        for scope in scopes:
            scope_path = scope.path or ""
            if scope_path and _path_matches_any(scope_path, deleted_paths):
                log_warning(
                    f"[PostCommit] repo_scope {scope.id} path={scope_path!r} "
                    f"is now orphaned (parent folder was deleted). Surface in "
                    f"/scopes UI for user to delete or re-target."
                )
    except Exception as e:
        log_error(f"[PostCommit] delete hook (repository_scopes) failed: {e}")


def post_commit_move(project_id: str, old_prefix: str, new_prefix: str) -> None:
    """After moving/renaming paths in the tree, rewrite ``repository_scopes.path``
    so scopes that lived under ``old_prefix`` follow the move.
    """
    _post_commit_move_repository_scopes(project_id, old_prefix, new_prefix)


def _post_commit_move_repository_scopes(
    project_id: str, old_prefix: str, new_prefix: str,
) -> None:
    """Rewrite repository_scopes.path on folder rename."""
    try:
        from src.repo.scope_repository import RepositoryScopeRepository
        scopes = RepositoryScopeRepository()
        for scope in scopes.list_by_project(project_id):
            old_path = scope.path or ""
            if not old_path:
                continue
            new_path = _rewrite_path(old_path, old_prefix, new_prefix)
            if new_path == old_path:
                continue
            try:
                scopes.update_path(scope.id, new_path)
                log_info(
                    f"[PostCommit] repo_scope {scope.id} path "
                    f"{old_path!r} → {new_path!r}"
                )
            except Exception as e:
                # The UNIQUE(project_id, path) constraint can fire if a
                # different scope already lives at the new path. Log and
                # leave the orphan for the user to resolve via UI.
                log_warning(
                    f"[PostCommit] repo_scope {scope.id} path rewrite "
                    f"{old_path!r} → {new_path!r} rejected (likely UNIQUE "
                    f"conflict with existing scope): {e}"
                )
    except Exception as e:
        log_error(f"[PostCommit] move hook (repository_scopes) failed: {e}")


def _path_matches_any(path: str, deleted_paths: list[str]) -> bool:
    """Check if path equals or is a child of any deleted path."""
    normalized = path.strip("/")
    for dp in deleted_paths:
        dp_norm = dp.strip("/")
        if normalized == dp_norm or normalized.startswith(dp_norm + "/"):
            return True
    return False


def _rewrite_path(path: str, old_prefix: str, new_prefix: str) -> str:
    """Replace old_prefix with new_prefix in path."""
    old_norm = old_prefix.rstrip("/")
    new_norm = new_prefix.rstrip("/")
    if path == old_norm:
        return new_norm
    if path.startswith(old_norm + "/"):
        return new_norm + path[len(old_norm):]
    return path
