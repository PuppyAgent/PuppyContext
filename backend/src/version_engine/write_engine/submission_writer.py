"""Submission-oriented write paths for L5.

Native Git pushes and other submitted-tree workflows have extra protocol
semantics: fast-forward checks, cross-scope validation, optional object
promotion, and sparse server-side merges. Those concerns stay isolated here
instead of living in the public engine facade.
"""

from __future__ import annotations

import asyncio

from src.exceptions import CasRetriesExhausted
from src.version_engine.domain.intents import TransactionResult, VersionSubmissionIntent
from src.version_engine.infrastructure.supabase.repo_manager import VersionRepoManager
from src.version_engine.write_engine.audit import (
    audit_detail_with_pusher as _audit_detail_with_pusher,
    log_done as _log_done,
    now_iso as _now_iso,
)
from src.version_engine.write_engine.cas_backoff import cas_backoff
from src.version_engine.write_engine.conflict_policy import (
    QUEUE_POLICIES,
    merge_file_sets_for_policy,
    select_conflict_policy,
)
from src.version_engine.write_engine.conflict_queue import (
    record_pending_conflict as _record_pending_conflict_generic,
)
from src.version_engine.write_engine.errors import (
    CrossScopeSubmissionError,
    NonFastForwardSubmissionError,
)
from src.version_engine.write_engine.git_commit import build_git_commit
from src.version_engine.write_engine.path_utils import normalize_path
from src.version_engine.write_engine.publisher import (
    publish_project_update as _publish_project_update,
)
from src.version_engine.write_engine.root_state import (
    get_project_root_state_for_write as _get_project_root_state_for_write,
    get_project_view_head as _get_project_view_head,
    get_scope_state as _get_scope_state,
    graft_scope_tree as _graft_scope_tree,
    parent_scope_files as _parent_scope_files,
    scope_tree_hash_for_write as _scope_tree_hash_for_write,
)
from src.version_engine.write_engine.scope_view import (
    build_scope_view_commit as _build_scope_view_commit,
    git_safe_parent as _git_safe_parent,
)
from src.version_engine.write_engine.submission_commit import (
    select_or_create_commit as _select_or_create_commit,
)
from src.version_engine.write_engine.tree_access import (
    apply_sparse_file_merge as _apply_sparse_file_merge,
    changed_paths_from_tree_diff as _changed_paths_from_tree_diff,
    changed_relative_paths as _changed_relative_paths,
    changes_from_tree_diff as _changes_from_tree_diff,
    compute_merged_changes as _compute_merged_changes,
    files_at_commit as _files_at_commit,
    scope_files_for_head as _scope_files_for_head,
    sparse_files_at_tree_paths as _sparse_files_at_tree_paths,
    tree_hash_at_commit as _tree_hash_at_commit,
)
from src.version_engine.write_engine.tree_objects import (
    build_full_changes,
    build_tree_from_files,
    compute_changeset,
    validate_scope_bound_files,
)
from src.utils.logger import log_info, log_warning


_MAX_CAS_ATTEMPTS = 5


class SubmissionWriter:
    """Root-first submitted-tree writer."""

    def __init__(self, repo_manager: VersionRepoManager, ledger):
        self._repos = repo_manager
        self._ledger = ledger

    async def submit_version_root_first(
        self,
        intent: VersionSubmissionIntent,
        started_ms: int,
    ) -> TransactionResult:
        """Publish a submitted scope tree by grafting it into project root."""

        repo = self._repos.get_server_repo(intent.project_id)
        scope_norm = normalize_path(intent.scope_path)
        incoming_files = (
            dict(intent.proposed_files)
            if intent.proposed_files is not None
            else None
        )
        changed_paths_hint = [
            normalize_path(path)
            for path in intent.changed_paths
            if normalize_path(path)
        ]

        last_error: Exception | None = None
        for attempt in range(_MAX_CAS_ATTEMPTS):
            await cas_backoff(attempt)
            attempt_no = attempt + 1
            old_root_hash, base_root_hash = _get_project_root_state_for_write(repo)
            project_head_commit_id = _get_project_view_head(repo, old_root_hash)
            old_scope_hash = _scope_tree_hash_for_write(
                repo, base_root_hash, scope_norm,
            )
            _cached_scope_hash, current_scope_head_id = _get_scope_state(
                repo, scope_norm,
            )
            expected_scope_head_commit_id = (
                current_scope_head_id
                if scope_norm or intent.source_channel in {"git", "access_git"}
                else None
            )
            acceptable_git_heads: set[str] | None = None
            if intent.source_channel in {"git", "access_git"}:
                if scope_norm:
                    canonical_scope_head_id = current_scope_head_id or ""
                    git_visible_head_id = str(
                        (intent.audit_detail or {}).get("git_visible_old_commit_id")
                        or ""
                    )
                    # A root-derived view can have a visible HEAD before its
                    # first scope-state row exists. That alias is valid only
                    # while the canonical source head remains absent. Keeping
                    # the old visible alias after another writer commits would
                    # turn a stale Git CAS retry into an automatic merge/ack.
                    acceptable_git_heads = {
                        head for head in (canonical_scope_head_id or git_visible_head_id,)
                        if head
                    }
                    current_scope_head_id = (
                        canonical_scope_head_id or git_visible_head_id
                    )
                else:
                    current_scope_head_id = (
                        current_scope_head_id
                        or (
                            repo.get_head_commit_id()
                            if hasattr(repo, "get_head_commit_id")
                            else ""
                        )
                        or project_head_commit_id
                    )
            current_scope_head_id = (
                current_scope_head_id
                or (project_head_commit_id if not scope_norm else "")
            )

            if intent.source_channel in {"git", "access_git"}:
                acceptable_heads = acceptable_git_heads or {
                    head for head in (current_scope_head_id,) if head
                }
                if acceptable_heads and intent.base_commit_id not in acceptable_heads:
                    raise NonFastForwardSubmissionError(
                        expected_head_commit_id=intent.base_commit_id,
                        current_head_commit_id=(
                            current_scope_head_id
                            or sorted(acceptable_heads)[0]
                        ),
                    )
                if not acceptable_heads and intent.base_commit_id:
                    raise NonFastForwardSubmissionError(
                        expected_head_commit_id=intent.base_commit_id,
                        current_head_commit_id="",
                    )
                if (
                    intent.base_commit_id in acceptable_heads
                    and not current_scope_head_id
                ):
                    current_scope_head_id = intent.base_commit_id

            promoted_objects = False
            if (
                intent.promote_objects is not None
                and not changed_paths_hint
                and incoming_files is None
            ):
                await asyncio.to_thread(intent.promote_objects)
                promoted_objects = True

            current_files: dict[str, bytes] | None = None
            if incoming_files is not None:
                current_files = await asyncio.to_thread(
                    _scope_files_for_head, repo, scope_norm, old_scope_hash,
                )
                changed_relative_paths = _changed_relative_paths(
                    current_files,
                    incoming_files,
                )
            else:
                changed_relative_paths = changed_paths_hint or await asyncio.to_thread(
                    _changed_paths_from_tree_diff,
                    repo,
                    old_scope_hash,
                    intent.proposed_tree_id,
                )
            rejected = validate_scope_bound_files(
                repo,
                scope_norm,
                changed_relative_paths,
                intent.scope_excludes,
            )
            if rejected:
                await asyncio.to_thread(
                    repo.record_audit,
                    f"{intent.source_channel}_push_rejected",
                    intent.actor,
                    {
                        "scope": scope_norm,
                        "rejected_paths": rejected,
                        **intent.audit_detail,
                    },
                )
                try:
                    await asyncio.to_thread(
                        self._ledger.insert_version_transaction,
                        project_id=intent.project_id,
                        scope_path=scope_norm,
                        source_channel=intent.source_channel,
                        actor=intent.actor,
                        intent_type="submission",
                        status="rejected",
                        base_commit_id=intent.base_commit_id,
                        client_commit_id=intent.client_commit_id,
                        proposed_tree_id=intent.proposed_tree_id,
                        current_head_at_start=current_scope_head_id,
                        message=intent.message,
                        audit_detail={
                            "rejected_paths": rejected[:50],
                            **(intent.audit_detail or {}),
                        },
                        reason="cross_scope_paths_outside_scope",
                    )
                except Exception as exc:
                    log_warning(
                        f"[version_engine] failed to record rejected "
                        f"version_transactions row: {exc}",
                    )
                raise CrossScopeSubmissionError(
                    scope_path=scope_norm,
                    rejected_paths=rejected,
                )

            if intent.promote_objects is not None and not promoted_objects:
                await asyncio.to_thread(intent.promote_objects)
                promoted_objects = True

            if intent.base_commit_id == current_scope_head_id:
                new_scope_hash = intent.proposed_tree_id
                conflicts = []
                merged_changes: list[dict] = []
                if incoming_files is not None:
                    if current_files is None:
                        current_files = await asyncio.to_thread(
                            _scope_files_for_head, repo, scope_norm, old_scope_hash,
                        )
                    changes = compute_changeset(
                        scope_norm,
                        current_files,
                        incoming_files,
                    )
                    if intent.proposed_tree_id != old_scope_hash:
                        new_scope_hash = await asyncio.to_thread(
                            build_tree_from_files, repo.store, incoming_files,
                        )
                else:
                    changes = await asyncio.to_thread(
                        _changes_from_tree_diff,
                        repo,
                        scope_norm,
                        old_scope_hash,
                        new_scope_hash,
                    )
            else:
                if incoming_files is None:
                    base_tree_for_merge = await asyncio.to_thread(
                        _tree_hash_at_commit,
                        repo,
                        scope_norm,
                        intent.base_commit_id,
                    )
                    merge_paths = changed_paths_hint or await asyncio.to_thread(
                        _changed_paths_from_tree_diff,
                        repo,
                        base_tree_for_merge,
                        intent.proposed_tree_id,
                    )
                    base_files = await asyncio.to_thread(
                        _sparse_files_at_tree_paths,
                        repo,
                        base_tree_for_merge,
                        merge_paths,
                    )
                    current_files = await asyncio.to_thread(
                        _sparse_files_at_tree_paths,
                        repo,
                        old_scope_hash,
                        merge_paths,
                    )
                    incoming_files = await asyncio.to_thread(
                        _sparse_files_at_tree_paths,
                        repo,
                        intent.proposed_tree_id,
                        merge_paths,
                    )
                    sparse_merge = True
                else:
                    merge_paths = list(incoming_files.keys())
                    sparse_merge = False
                if current_files is None:
                    current_files = await asyncio.to_thread(
                        _scope_files_for_head, repo, scope_norm, old_scope_hash,
                    )
                if not sparse_merge:
                    base_files = await asyncio.to_thread(
                        _files_at_commit, repo, scope_norm, intent.base_commit_id,
                    )
                if intent.policy_override in QUEUE_POLICIES:
                    from src.version_engine.domain.conflicts import (
                        ConflictPolicyDecision,
                    )

                    policy = ConflictPolicyDecision(
                        policy=intent.policy_override,
                        reason=(
                            f"submission_intent_override:{intent.policy_override}"
                        ),
                    )
                else:
                    policy = select_conflict_policy(
                        scope_path=scope_norm,
                        source_channel=intent.source_channel,
                        actor=intent.actor,
                        paths=merge_paths,
                    )
                parent_scope_files = await asyncio.to_thread(
                    _parent_scope_files, repo, scope_norm, merge_paths,
                )
                merge_result = merge_file_sets_for_policy(
                    base_files,
                    current_files,
                    incoming_files,
                    policy=policy,
                    parent_scope_files=parent_scope_files,
                )
                if merge_result.manual_conflicts and policy.policy in QUEUE_POLICIES:
                    result = await self.record_pending_conflict(
                        repo=repo,
                        intent=intent,
                        scope_path=scope_norm,
                        current_head_commit_id=current_scope_head_id,
                        current_scope_hash=old_scope_hash,
                        base_files=base_files,
                        current_files=current_files,
                        incoming_files=incoming_files,
                        manual_conflicts=merge_result.manual_conflicts,
                        policy_reason=policy.reason,
                        policy=policy.policy,
                    )
                    _log_done(
                        f"{intent.source_channel}_push_pending",
                        intent.project_id,
                        scope_norm,
                        result,
                        started_ms,
                    )
                    return result
                merged_files = merge_result.merged_files
                conflicts = (
                    list(merge_result.auto_merge_records)
                    + list(merge_result.lww_records)
                    + list(merge_result.superseded_by_parent)
                )
                if sparse_merge:
                    new_scope_hash, sparse_changes = await asyncio.to_thread(
                        _apply_sparse_file_merge,
                        repo,
                        old_scope_hash,
                        current_files,
                        merged_files,
                        merge_paths,
                    )
                    changes = build_full_changes(scope_norm, sparse_changes)
                else:
                    new_scope_hash = await asyncio.to_thread(
                        build_tree_from_files, repo.store, merged_files,
                    )
                    changes = compute_changeset(scope_norm, current_files, merged_files)
                merged_changes = _compute_merged_changes(
                    current_files, merged_files, incoming_files, scope_norm,
                )

            preserves_new_git_identity = (
                intent.source_channel in {"git", "access_git"}
                and bool(intent.client_commit_id)
                and intent.client_commit_id != current_scope_head_id
                and new_scope_hash == intent.proposed_tree_id
            )
            if (
                not changes
                and new_scope_hash == old_scope_hash
                and not preserves_new_git_identity
            ):
                return TransactionResult(
                    status="ok",
                    commit_id=current_scope_head_id,
                    new_scope_hash=old_scope_hash,
                    is_noop=True,
                )

            new_root_hash = _graft_scope_tree(
                repo, base_root_hash, scope_norm, new_scope_hash,
            )
            created_at_iso = _now_iso()
            if not scope_norm:
                commit_id = _select_or_create_commit(
                    repo=repo,
                    intent=intent,
                    tree_id=new_root_hash,
                    parent_id=current_scope_head_id,
                    created_at_iso=created_at_iso,
                    preserve_client=True,
                )
            else:
                trailers = {
                    "PuppyOne-Source": intent.source_channel,
                    "PuppyOne-Scope": scope_norm or "/",
                    "PuppyOne-Original-Commit": intent.client_commit_id or "",
                    "PuppyOne-Base-Commit": intent.base_commit_id or "",
                }
                commit_id = await asyncio.to_thread(
                    build_git_commit,
                    repo,
                    tree_sha=new_root_hash,
                    parent_sha=_git_safe_parent(repo, project_head_commit_id),
                    who=intent.actor,
                    message=intent.message,
                    created_at_iso=created_at_iso,
                    trailers=trailers,
                    validate_parent_graph=False,
                )

            scope_head_commit_id = ""
            if scope_norm:
                if (
                    intent.client_commit_id
                    and new_scope_hash == intent.proposed_tree_id
                ):
                    scope_head_commit_id = intent.client_commit_id
                else:
                    scope_head_commit_id = await asyncio.to_thread(
                        _build_scope_view_commit,
                        repo,
                        scope_path=scope_norm,
                        scope_hash=new_scope_hash,
                        parent_id=current_scope_head_id,
                        actor=intent.actor,
                        message=intent.message,
                        created_at_iso=created_at_iso,
                        source_channel=intent.source_channel,
                        original_commit_id=intent.client_commit_id,
                        base_commit_id=intent.base_commit_id,
                    )

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
                commit_id=commit_id,
                actor=intent.actor,
                message=intent.message,
                op_type=f"{intent.source_channel}_push",
                audit_detail={
                    "base_commit_id": intent.base_commit_id,
                    "client_commit_id": intent.client_commit_id,
                    **_audit_detail_with_pusher(intent),
                },
                changes=changes,
                conflicts=conflicts,
                created_at_iso=created_at_iso,
                cas_attempt=attempt_no,
                merged=bool(conflicts) or new_scope_hash != intent.proposed_tree_id,
                merged_changes=merged_changes,
                source_channel=intent.source_channel,
                base_commit_id=intent.base_commit_id,
                client_commit_id=intent.client_commit_id,
                proposed_tree_id=intent.proposed_tree_id,
                intent_type="submission",
            )
            if result is not None:
                result.new_scope_hash = new_scope_hash
                _log_done(
                    f"{intent.source_channel}_push",
                    intent.project_id,
                    scope_norm,
                    result,
                    started_ms,
                )
                return result

            last_error = RuntimeError("project root CAS lost")
            log_info(
                f"[version_engine][{intent.source_channel}_push] root CAS lost "
                f"(attempt {attempt_no}/{_MAX_CAS_ATTEMPTS}) "
                f"project={intent.project_id} scope={scope_norm!r}",
            )

        raise CasRetriesExhausted(
            f"[version_engine][{intent.source_channel}_push] root CAS still failing "
            f"after {_MAX_CAS_ATTEMPTS} attempts "
            f"(project={intent.project_id}, scope={scope_norm!r}); "
            f"last error: {last_error}",
        )

    async def record_pending_conflict(
        self,
        *,
        repo,
        intent: VersionSubmissionIntent,
        scope_path: str,
        current_head_commit_id: str,
        current_scope_hash: str,
        base_files: dict[str, bytes],
        current_files: dict[str, bytes],
        incoming_files: dict[str, bytes],
        manual_conflicts: list,
        policy_reason: str,
        policy: str = "manual_review",
    ) -> TransactionResult:
        """Submission-intent shaped wrapper for conflict queue recording."""

        return await _record_pending_conflict_generic(
            ledger=self._ledger,
            repo=repo,
            project_id=intent.project_id,
            scope_path=scope_path,
            current_head_commit_id=current_head_commit_id,
            current_scope_hash=current_scope_hash,
            client_commit_id=intent.client_commit_id,
            base_commit_id=intent.base_commit_id,
            proposed_tree_id=intent.proposed_tree_id,
            source_channel=intent.source_channel,
            actor=intent.actor,
            message=intent.message,
            audit_detail=_audit_detail_with_pusher(intent),
            base_files=base_files,
            current_files=current_files,
            incoming_files=incoming_files,
            manual_conflicts=manual_conflicts,
            policy_reason=policy_reason,
            policy=policy,
        )
