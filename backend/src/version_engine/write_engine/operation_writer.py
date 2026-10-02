"""Operation-oriented write paths for L5.

This module owns typed product/API operations: a caller supplies a splice
function, the writer turns it into a root-first committed version fact.
"""

from __future__ import annotations

import asyncio
import time
from typing import Callable

from src.exceptions import CasRetriesExhausted
from src.version_engine.domain.intents import OperationWriteIntent, TransactionResult
from src.version_engine.infrastructure.supabase.repo_manager import VersionRepoManager
from src.version_engine.storage.object_store import ObjectStore, stage_object_writes
from src.version_engine.write_engine.audit import (
    audit_detail_with_pusher as _audit_detail_with_pusher,
    log_done as _log_done,
    now_iso as _now_iso,
)
from src.version_engine.write_engine.cas_backoff import cas_backoff
from src.version_engine.write_engine.cas_retry import (
    merge_on_cas_retry as _merge_on_cas_retry,
)
from src.version_engine.write_engine.conflict_policy import QUEUE_POLICIES
from src.version_engine.write_engine.conflict_queue import (
    record_pending_conflict as _record_pending_conflict_generic,
)
from src.version_engine.write_engine.errors import ConcurrentMutationError
from src.version_engine.write_engine.git_commit import build_git_commit
from src.version_engine.write_engine.git_object_format import encode_tree
from src.version_engine.write_engine.path_utils import normalize_path
from src.version_engine.write_engine.publisher import (
    publish_project_update as _publish_project_update,
)
from src.version_engine.write_engine.root_state import (
    get_project_root_hash as _get_project_root_hash,
    get_project_root_state_for_write as _get_project_root_state_for_write,
    get_project_view_head as _get_project_view_head,
    get_scope_state as _get_scope_state,
    graft_scope_tree as _graft_scope_tree,
    scope_tree_hash_for_write as _scope_tree_hash_for_write,
)
from src.version_engine.write_engine.scope_view import (
    build_scope_view_commit as _build_scope_view_commit,
    git_safe_parent as _git_safe_parent,
)
from src.version_engine.write_engine.trace import trace_mark, trace_phase
from src.version_engine.write_engine.tree_objects import (
    build_full_changes,
    compute_changeset,
    flatten_tree_to_bytes,
)
from src.utils.logger import log_info


SpliceFn = Callable[[ObjectStore, str], "tuple[str, list[tuple[str, str]]]"]

_MAX_CAS_ATTEMPTS = 5


class OperationWriter:
    """Root-first product operation writer."""

    def __init__(self, repo_manager: VersionRepoManager, ledger):
        self._repos = repo_manager
        self._ledger = ledger

    async def apply_scoped_operation_root_first(
        self,
        *,
        intent: OperationWriteIntent,
        splice: SpliceFn,
        started_ms: int,
    ) -> TransactionResult:
        """Apply a scope-relative product operation to the canonical root.

        Scope/access still defines the caller's working directory and policy
        boundary, but the linearization point is the project root CAS. The
        accepted scope subtree is written back to scope state only as a
        derived cache for Git/AP reads and legacy callers.
        """

        repo = self._repos.get_server_repo(intent.project_id)
        scope_norm = normalize_path(intent.scope_path)

        last_error: Exception | None = None
        base_scope_hash: str | None = None
        merge_audit: dict | None = None
        for attempt in range(_MAX_CAS_ATTEMPTS):
            await cas_backoff(attempt)
            attempt_no = attempt + 1
            old_root_hash, base_root_hash = _get_project_root_state_for_write(repo)
            current_head_commit_id = _get_project_view_head(repo, old_root_hash)
            current_scope_hash = _scope_tree_hash_for_write(
                repo, base_root_hash, scope_norm,
            )

            if attempt == 0:
                base_scope_hash = current_scope_hash

            if (
                intent.expected_head_commit_id is not None
                and current_head_commit_id != intent.expected_head_commit_id
            ):
                raise ConcurrentMutationError(
                    scope_path=scope_norm,
                    expected_head_commit_id=intent.expected_head_commit_id,
                    current_head_commit_id=current_head_commit_id,
                )

            with stage_object_writes(repo.store) as object_batch:
                new_scope_hash, splice_changes = await asyncio.to_thread(
                    splice, repo.store, current_scope_hash,
                )
                full_changes = build_full_changes(scope_norm, splice_changes)

                pending_result: TransactionResult | None = None
                if (
                    attempt > 0
                    and base_scope_hash is not None
                    and base_scope_hash != current_scope_hash
                    and new_scope_hash != current_scope_hash
                ):
                    (
                        merged_tree,
                        merge_audit,
                        manual_conflicts,
                        merge_policy,
                        base_files,
                        current_files_at_head,
                        incoming_files_for_audit,
                    ) = _merge_on_cas_retry(
                        repo=repo,
                        intent=intent,
                        scope_norm=scope_norm,
                        base_scope_hash=base_scope_hash,
                        current_scope_hash=current_scope_hash,
                        incoming_scope_hash=new_scope_hash,
                    )
                    if manual_conflicts and merge_policy in QUEUE_POLICIES:
                        # A retry may build a proposal not flushed by the first
                        # attempt. Persist it before recording/acknowledging the
                        # pending ID; leaving this batch discards staged bytes.
                        if object_batch is not None:
                            await asyncio.to_thread(object_batch.flush)
                        pending_result = await _record_pending_conflict_generic(
                            ledger=self._ledger,
                            repo=repo,
                            project_id=intent.project_id,
                            scope_path=scope_norm,
                            current_head_commit_id=current_head_commit_id,
                            current_scope_hash=current_scope_hash,
                            client_commit_id="",
                            base_commit_id=base_scope_hash,
                            proposed_tree_id=new_scope_hash,
                            source_channel=intent.source_channel,
                            actor=intent.actor,
                            message=intent.message,
                            audit_detail=_audit_detail_with_pusher(intent),
                            base_files=base_files,
                            current_files=current_files_at_head,
                            incoming_files=incoming_files_for_audit,
                            manual_conflicts=manual_conflicts,
                            policy_reason=(merge_audit or {}).get(
                                "policy_reason", "manual_review",
                            ),
                            policy=merge_policy,
                        )
                    elif merged_tree is not None and merged_tree != new_scope_hash:
                        old_files = await asyncio.to_thread(
                            flatten_tree_to_bytes, repo.store, current_scope_hash,
                        )
                        new_files = await asyncio.to_thread(
                            flatten_tree_to_bytes, repo.store, merged_tree,
                        )
                        full_changes = compute_changeset(
                            scope_norm, old_files, new_files,
                        )
                        new_scope_hash = merged_tree

                if pending_result is not None:
                    _log_done(
                        f"{intent.operation_type}_pending",
                        intent.project_id,
                        scope_norm,
                        pending_result,
                        started_ms,
                    )
                    return pending_result

                if (
                    not full_changes
                    or (
                        new_scope_hash == current_scope_hash
                        and not intent.allow_same_tree_commit
                    )
                ):
                    elapsed = int(time.time() * 1000) - started_ms
                    log_info(
                        f"[version_engine][{intent.operation_type}] noop "
                        f"project={intent.project_id} scope={scope_norm!r} "
                        f"elapsed={elapsed}ms",
                    )
                    return TransactionResult(
                        status="ok",
                        is_noop=True,
                        new_scope_hash=current_scope_hash,
                    )

                new_root_hash = _graft_scope_tree(
                    repo, base_root_hash, scope_norm, new_scope_hash,
                )
                created_at_iso = _now_iso()
                new_commit_id = await asyncio.to_thread(
                    build_git_commit,
                    repo,
                    tree_sha=new_root_hash,
                    parent_sha=_git_safe_parent(repo, current_head_commit_id),
                    who=intent.actor,
                    message=intent.message,
                    created_at_iso=created_at_iso,
                    validate_parent_graph=False,
                )

                if object_batch is not None:
                    await asyncio.to_thread(object_batch.flush)
                    register_candidates = getattr(
                        repo.history, "register_object_gc_candidates", None
                    )
                    if callable(register_candidates):
                        await asyncio.to_thread(
                            register_candidates, object_batch.flushed_ids
                        )

            scope_head_commit_id = ""
            expected_scope_head_commit_id: str | None = None
            if scope_norm:
                _cached_scope_hash, previous_scope_head_id = _get_scope_state(
                    repo, scope_norm,
                )
                expected_scope_head_commit_id = previous_scope_head_id
                scope_head_commit_id = await asyncio.to_thread(
                    _build_scope_view_commit,
                    repo,
                    scope_path=scope_norm,
                    scope_hash=new_scope_hash,
                    parent_id=previous_scope_head_id,
                    actor=intent.actor,
                    message=intent.message,
                    created_at_iso=created_at_iso,
                    source_channel=intent.source_channel,
                    base_commit_id=previous_scope_head_id,
                )

            audit_detail = dict(_audit_detail_with_pusher(intent))
            if merge_audit:
                audit_detail.setdefault("cas_retry_merge", merge_audit)
            result = await _publish_project_update(
                repo_manager=self._repos,
                repo=repo,
                project_id=intent.project_id,
                old_root_hash=old_root_hash,
                new_root_hash=new_root_hash,
                scope_path=scope_norm,
                scope_hash=new_scope_hash,
                scope_head_commit_id=scope_head_commit_id,
                expected_scope_head_commit_id=expected_scope_head_commit_id,
                commit_id=new_commit_id,
                actor=intent.actor,
                message=intent.message,
                op_type=intent.operation_type,
                audit_detail=audit_detail,
                changes=full_changes,
                conflicts=None,
                created_at_iso=created_at_iso,
                cas_attempt=attempt_no,
                merged=bool(merge_audit),
                merged_changes=[],
                source_channel=intent.source_channel,
                policy=intent.policy_override,
                base_commit_id=current_head_commit_id,
                proposed_tree_id=new_scope_hash,
                intent_type="operation",
            )
            if result is not None:
                result.new_scope_hash = new_scope_hash
                _log_done(
                    intent.operation_type,
                    intent.project_id,
                    scope_norm,
                    result,
                    started_ms,
                )
                return result

            last_error = RuntimeError("project root CAS lost")
            log_info(
                f"[version_engine][{intent.operation_type}] root CAS lost "
                f"(attempt {attempt_no}/{_MAX_CAS_ATTEMPTS}) "
                f"project={intent.project_id} scope={scope_norm!r}",
            )

        raise CasRetriesExhausted(
            f"[version_engine][{intent.operation_type}] root CAS still failing "
            f"after {_MAX_CAS_ATTEMPTS} attempts "
            f"(project={intent.project_id}, scope={scope_norm!r}); "
            f"last error: {last_error}",
        )

    async def apply_project_operation_optimistic(
        self,
        *,
        intent: OperationWriteIntent,
        splice: SpliceFn,
        started_ms: int,
    ) -> TransactionResult:
        """Apply a project-root operation against the canonical root."""

        write_state = intent.project_write_state
        with trace_phase("repo.resolve", project_id=intent.project_id):
            repo = self._repos.get_server_repo(
                intent.project_id,
                project_name=write_state.project_name if write_state else None,
            )

        last_error: Exception | None = None
        merge_base_root_hash: str | None = None
        merge_audit: dict | None = None
        for attempt in range(_MAX_CAS_ATTEMPTS):
            await cas_backoff(attempt)
            attempt_no = attempt + 1
            if attempt == 0 and write_state is not None:
                old_root_hash = write_state.root_hash or ""
                current_head_commit_id = write_state.head_commit_id or ""
                trace_mark(
                    "db.project_write_state.reused",
                    attempt=attempt_no,
                    root_hash=old_root_hash[:12],
                    head_commit_id=current_head_commit_id[:12],
                )
            else:
                with trace_phase("db.get_project_root", attempt=attempt_no):
                    old_root_hash = _get_project_root_hash(repo)
                with trace_phase(
                    "db.get_project_view_head",
                    attempt=attempt_no,
                    root_hash=old_root_hash[:12],
                ):
                    current_head_commit_id = _get_project_view_head(repo, old_root_hash)
            if old_root_hash:
                base_root_hash = old_root_hash
            else:
                with trace_phase("object.create_empty_root", attempt=attempt_no):
                    base_root_hash = repo.store.put_tree(encode_tree([]))
            if attempt == 0:
                merge_base_root_hash = base_root_hash
            if (
                intent.expected_head_commit_id is not None
                and current_head_commit_id != intent.expected_head_commit_id
            ):
                raise ConcurrentMutationError(
                    scope_path="",
                    expected_head_commit_id=intent.expected_head_commit_id,
                    current_head_commit_id=current_head_commit_id,
                )

            with stage_object_writes(repo.store) as object_batch:
                with trace_phase(
                    "tree.splice",
                    attempt=attempt_no,
                    base_root_hash=base_root_hash[:12],
                ):
                    new_root_hash, changes = await asyncio.to_thread(
                        splice, repo.store, base_root_hash,
                    )

                pending_result: TransactionResult | None = None
                if (
                    attempt > 0
                    and merge_base_root_hash is not None
                    and merge_base_root_hash != base_root_hash
                    and new_root_hash != base_root_hash
                ):
                    (
                        merged_tree,
                        merge_audit,
                        manual_conflicts,
                        merge_policy,
                        base_files,
                        current_files_at_head,
                        incoming_files_for_audit,
                    ) = _merge_on_cas_retry(
                        repo=repo,
                        intent=intent,
                        scope_norm="",
                        base_scope_hash=merge_base_root_hash,
                        current_scope_hash=base_root_hash,
                        incoming_scope_hash=new_root_hash,
                    )
                    if manual_conflicts and merge_policy in QUEUE_POLICIES:
                        if object_batch is not None:
                            await asyncio.to_thread(object_batch.flush)
                        pending_result = await _record_pending_conflict_generic(
                            ledger=self._ledger,
                            repo=repo,
                            project_id=intent.project_id,
                            scope_path="",
                            current_head_commit_id=current_head_commit_id,
                            current_scope_hash=base_root_hash,
                            client_commit_id="",
                            base_commit_id=merge_base_root_hash,
                            proposed_tree_id=new_root_hash,
                            source_channel=intent.source_channel,
                            actor=intent.actor,
                            message=intent.message,
                            audit_detail=_audit_detail_with_pusher(intent),
                            base_files=base_files,
                            current_files=current_files_at_head,
                            incoming_files=incoming_files_for_audit,
                            manual_conflicts=manual_conflicts,
                            policy_reason=(merge_audit or {}).get(
                                "policy_reason", "manual_review",
                            ),
                            policy=merge_policy,
                        )
                    elif merged_tree is not None and merged_tree != new_root_hash:
                        changes = await asyncio.to_thread(
                            compute_changeset,
                            "",
                            await asyncio.to_thread(
                                flatten_tree_to_bytes, repo.store, base_root_hash,
                            ),
                            await asyncio.to_thread(
                                flatten_tree_to_bytes, repo.store, merged_tree,
                            ),
                        )
                        new_root_hash = merged_tree

                if pending_result is not None:
                    _log_done(
                        f"{intent.operation_type}:project_pending",
                        intent.project_id,
                        "",
                        pending_result,
                        started_ms,
                    )
                    return pending_result

                if (
                    not changes
                    or (
                        new_root_hash == base_root_hash
                        and not intent.allow_same_tree_commit
                    )
                ):
                    elapsed = int(time.time() * 1000) - started_ms
                    log_info(
                        f"[version_engine][{intent.operation_type}:project] noop "
                        f"project={intent.project_id} elapsed={elapsed}ms",
                    )
                    return TransactionResult(
                        status="ok",
                        is_noop=True,
                        new_scope_hash=base_root_hash,
                    )

                created_at_iso = _now_iso()
                with trace_phase(
                    "git.parent.resolve",
                    attempt=attempt_no,
                    current_head=current_head_commit_id[:12],
                ):
                    parent_commit_id = _git_safe_parent(repo, current_head_commit_id)
                with trace_phase(
                    "git.build_commit",
                    attempt=attempt_no,
                    tree_sha=new_root_hash[:12],
                    parent_sha=parent_commit_id[:12],
                ):
                    new_commit_id = await asyncio.to_thread(
                        build_git_commit,
                        repo,
                        tree_sha=new_root_hash,
                        parent_sha=parent_commit_id,
                        who=intent.actor,
                        message=intent.message,
                        created_at_iso=created_at_iso,
                        validate_parent_graph=False,
                    )

                if object_batch is not None:
                    count = getattr(object_batch, "count", lambda: None)()
                    with trace_phase("object.flush", attempt=attempt_no, count=count):
                        await asyncio.to_thread(object_batch.flush)
                    register_candidates = getattr(
                        repo.history, "register_object_gc_candidates", None
                    )
                    if callable(register_candidates):
                        await asyncio.to_thread(
                            register_candidates, object_batch.flushed_ids
                        )

            with trace_phase("changes.build_full_changes", attempt=attempt_no):
                full_changes = build_full_changes("", changes)
            with trace_phase(
                "db.publish_project_update",
                attempt=attempt_no,
                old_root_hash=old_root_hash[:12],
                new_root_hash=new_root_hash[:12],
                commit_id=new_commit_id[:12],
            ):
                result = await _publish_project_update(
                    repo_manager=self._repos,
                    repo=repo,
                    project_id=intent.project_id,
                    old_root_hash=old_root_hash,
                    new_root_hash=new_root_hash,
                    commit_id=new_commit_id,
                    actor=intent.actor,
                    message=intent.message,
                    op_type=intent.operation_type,
                    audit_detail=_audit_detail_with_pusher(intent),
                    changes=full_changes,
                    conflicts=None,
                    created_at_iso=created_at_iso,
                    cas_attempt=attempt_no,
                    merged=False,
                    merged_changes=[],
                    source_channel=intent.source_channel,
                    policy=intent.policy_override,
                    base_commit_id=current_head_commit_id,
                    proposed_tree_id=new_root_hash,
                    intent_type="operation",
                )
            if result is not None:
                _log_done(
                    f"{intent.operation_type}:project",
                    intent.project_id,
                    "",
                    result,
                    started_ms,
                )
                return result

            last_error = RuntimeError("project root CAS lost")
            log_info(
                f"[version_engine][{intent.operation_type}:project] root CAS lost "
                f"(attempt {attempt_no}/{_MAX_CAS_ATTEMPTS}) "
                f"project={intent.project_id}",
            )

        raise CasRetriesExhausted(
            f"[version_engine][{intent.operation_type}:project] root CAS still "
            f"failing after {_MAX_CAS_ATTEMPTS} attempts "
            f"(project={intent.project_id}); last error: {last_error}",
        )
