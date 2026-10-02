"""Pending-conflict persistence for L5.

Conflict detection belongs to the write engine. This module only records the
pending review fact and audit trail once L5 has decided manual review is needed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json

from src.version_engine.domain.intents import TransactionResult
from src.version_engine.write_engine.conflict_policy import conflict_to_dict
from src.utils.logger import log_warning


def pending_conflict_id(
    project_id: str,
    scope_path: str,
    current_head_commit_id: str,
    client_commit_id: str,
    paths: list[str],
    *,
    proposed_tree_id: str = "",
    base_commit_id: str = "",
    actor: str = "",
    source_channel: str = "",
    policy: str = "manual_review",
) -> str:
    identity = {
        "project_id": project_id,
        "scope_path": scope_path,
        "current_head_commit_id": current_head_commit_id,
        "client_commit_id": client_commit_id,
        "paths": paths,
    }
    if not client_commit_id:
        if not proposed_tree_id:
            raise ValueError("operation conflict requires a proposed tree identity")
        # Product operations have no client commit. Head + paths alone aliases
        # every proposal against that head, overwriting another writer's work.
        # Preserve the existing Git identity when a client commit is supplied.
        identity.update({
            "identity_version": 2,
            "proposed_tree_id": proposed_tree_id,
            "base_commit_id": base_commit_id,
            "actor": actor,
            "source_channel": source_channel,
            "policy": policy,
            "paths": sorted(set(paths)),
        })
    payload = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha1(payload).hexdigest()


async def record_pending_conflict(
    *,
    ledger,
    repo,
    project_id: str,
    scope_path: str,
    current_head_commit_id: str,
    current_scope_hash: str,
    client_commit_id: str,
    base_commit_id: str,
    proposed_tree_id: str,
    source_channel: str,
    actor: str,
    message: str,
    audit_detail: dict,
    base_files: dict[str, bytes],
    current_files: dict[str, bytes],
    incoming_files: dict[str, bytes],
    manual_conflicts: list,
    policy_reason: str,
    policy: str = "manual_review",
) -> TransactionResult:
    """Persist a pending conflict for review.

    ``policy`` is the conflict policy actually selected (``manual_review``,
    ``agent_review``, or ``agent_auto_resolve``). It MUST be recorded
    faithfully: PUP-5's "Pending review" vs "Conflict" split is driven by
    ``policy``/``resolver_kind``, and the ledger derives ``resolver_kind``
    from it (an ``agent_*`` policy routes to the agent resolver regardless
    of channel). Previously this was hardcoded to ``manual_review``, so
    every agent-claimed conflict was mis-recorded as human manual review.
    """

    paths = sorted({
        getattr(conflict, "path", "")
        for conflict in manual_conflicts
        if getattr(conflict, "path", "")
    })
    conflict_id = pending_conflict_id(
        project_id,
        scope_path,
        current_head_commit_id,
        client_commit_id,
        paths,
        proposed_tree_id=proposed_tree_id,
        base_commit_id=base_commit_id,
        actor=actor,
        source_channel=source_channel,
        policy=policy,
    )
    audit = {
        "status": "pending_manual_review",
        "pending_conflict_id": conflict_id,
        "policy": policy,
        "policy_reason": policy_reason,
        "scope": scope_path,
        "base_commit_id": base_commit_id,
        "current_head_commit_id": current_head_commit_id,
        "client_commit_id": client_commit_id,
        "proposed_tree_id": proposed_tree_id,
        "current_scope_hash": current_scope_hash,
        "conflict_count": len(manual_conflicts),
        "conflicts": [conflict_to_dict(conflict) for conflict in manual_conflicts],
        "base_paths": sorted(base_files),
        "current_paths": sorted(current_files),
        "incoming_paths": sorted(incoming_files),
        **(audit_detail or {}),
    }
    await asyncio.to_thread(
        repo.record_audit,
        f"{source_channel}_push_conflict_pending",
        actor,
        audit,
    )

    txn_id: int | None = None
    try:
        txn_id = await asyncio.to_thread(
            ledger.insert_version_transaction,
            project_id=project_id,
            scope_path=scope_path,
            source_channel=source_channel,
            actor=actor,
            intent_type="submission",
            status="pending_manual_review",
            policy=policy,
            base_commit_id=base_commit_id,
            client_commit_id=client_commit_id,
            proposed_tree_id=proposed_tree_id,
            current_head_at_start=current_head_commit_id,
            message=message,
            audit_detail={
                "pending_conflict_id": conflict_id,
                **(audit_detail or {}),
            },
            reason=policy_reason,
        )
    except Exception as exc:
        log_warning(
            f"[version_engine] failed to record pending version_transactions "
            f"row for {conflict_id[:12]}: {exc}",
        )

    try:
        await asyncio.to_thread(
            ledger.record_pending_conflict,
            project_id=project_id,
            pending_conflict_id=conflict_id,
            scope_path=scope_path,
            base_commit_id=base_commit_id,
            current_commit_id=current_head_commit_id,
            client_commit_id=client_commit_id,
            proposed_tree_id=proposed_tree_id,
            changed_paths=paths,
            conflict_records=[
                conflict_to_dict(conflict) for conflict in manual_conflicts
            ],
            policy=policy,
            source_channel=source_channel,
            actor=actor,
            transaction_id=txn_id,
        )
    except Exception as exc:
        log_warning(
            f"[version_engine] failed to persist pending-conflict row "
            f"{conflict_id[:12]}: {exc}",
        )
        # Never acknowledge a queued proposal when its only recovery record
        # was not persisted. The caller must see a failure, not a pending ID
        # that no resolver can load.
        raise
    return TransactionResult(
        status="pending",
        merged=False,
        conflicts=len(manual_conflicts),
        paths=paths,
        new_scope_hash=current_scope_hash,
        pending_conflict_id=conflict_id,
        reason="manual_review_required",
    )
