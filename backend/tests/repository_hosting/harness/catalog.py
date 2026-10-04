"""Run the pre-existing conflict catalog through the production operation engine.

The schedule is deterministic: every writer starts from the seeded snapshot;
their publication attempts are ordered by delay_ms/index. This exercises stale
snapshot/CAS-retry behavior without sleep-based timing. It is NOT a live PG test.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException

from src.version_engine.domain.intents import ConflictResolutionIntent, ProjectWriteState
from src.version_engine.write_engine.engine import VersionWriteEngine
from src.version_engine.write_engine.errors import ConcurrentMutationError
from src.version_engine.write_engine.tree_objects import flatten_tree_to_bytes


class MemoryLedger:
    def __init__(self):
        self.pending = {}
        self.transactions = []

    def insert_version_transaction(self, **row):
        self.transactions.append(row)
        return len(self.transactions)

    def record_pending_conflict(self, **row):
        self.pending[row["pending_conflict_id"]] = {**row, "status": "pending"}

    def load_pending_conflict(self, project_id, pending_conflict_id):
        return self.pending.get(pending_conflict_id)

    def mark_pending_conflict(self, **row):
        self.pending[row["pending_conflict_id"]].update(row)

    def close_pending_conflict(self, **row):
        self.pending[row["pending_conflict_id"]].update(row)


@dataclass
class CatalogResult:
    files: dict[str, bytes]
    outcomes: list[str]
    errors: list[str]
    pending: int
    commits: list[str]


async def run_case(case, state, monkeypatch):
    repo, adapter = state.repo, state.adapter
    ledger = MemoryLedger()
    adapter._engine = VersionWriteEngine(state.manager, ledger=ledger)
    for path, _mode in case.scopes:
        if path:
            repo.add_scope("catalog-" + path.replace("/", "-"), path)
    seed = {}
    for scope, files in case.setup.items():
        for path, body in files.items():
            seed["/".join(part for part in (scope.strip("/"), path) if part)] = body
    before_seed = repo.history.get_head_commit_id()
    await adapter.bulk_write("test-proj", seed, who="user:seed", defer_projection=True)
    root = repo.history.get_root_hash()
    head = repo.history.get_head_commit_id()
    snapshot = ProjectWriteState(
        "test-proj", "Catalog", can_write=True, root_hash=root, head_commit_id=head
    )
    outcomes = [""] * len(case.writers)
    errors = [""] * len(case.writers)
    commits = []

    for index, writer in sorted(
        enumerate(case.writers), key=lambda pair: (pair[1].delay_ms, pair[0])
    ):
        policy = writer.policy
        kwargs = dict(
            who=writer.actor,
            scope=writer.scope,
            policy=policy,
            source_channel=writer.source_channel,
            defer_projection=True,
            project_write_state=None if case.id == "E09" else snapshot,
        )
        try:
            if writer.operation == "resolve":
                pending = next(
                    (p for p in ledger.pending.values() if p["status"] == "pending"), None
                )
                if pending is None:
                    raise AssertionError("catalog resolver has no pending conflict")
                choice = writer.files["choice"].decode()
                tree = (
                    pending["proposed_tree_id"]
                    if choice == "theirs"
                    else repo.history.get_root_hash()
                )
                resolution_files = None
                if choice == "merged":
                    resolution_files = flatten_tree_to_bytes(
                        repo.store, repo.history.get_root_hash()
                    )
                    resolution_files[pending["changed_paths"][0]] = writer.files["content"]
                result = await adapter._engine.resolve(
                    ConflictResolutionIntent(
                        project_id="test-proj",
                        pending_conflict_id=pending["pending_conflict_id"],
                        scope_path=writer.scope,
                        resolver_actor=writer.actor,
                        source_channel=writer.source_channel,
                        decision="reject" if choice == "reject" else "accept",
                        resolution_tree_id=tree,
                        resolution_files=resolution_files,
                        defer_projection=True,
                    )
                )
                outcomes[index] = (
                    "committed"  # successful resolver action, including explicit rejection
                )
            elif writer.operation == "write_file":
                path, body = next(iter(writer.files.items()))
                result = await adapter.write_file(
                    "test-proj",
                    path,
                    body,
                    **kwargs,
                    base_commit_id=before_seed if writer.base == "frozen" else None,
                )
            elif writer.operation == "bulk_write":
                result = await adapter.bulk_write(
                    "test-proj", dict(writer.files), deleted=list(writer.deleted), **kwargs
                )
            elif writer.operation == "delete":
                result = await adapter.delete("test-proj", list(writer.files), **kwargs)
            elif writer.operation == "rename":
                result = await adapter.move(
                    "test-proj",
                    writer.files["from"].decode(),
                    writer.files["to"].decode(),
                    **kwargs,
                )
            else:
                raise AssertionError(f"unimplemented catalog operation: {writer.operation}")
            if not outcomes[index]:
                outcomes[index] = (
                    "pending_resolution" if result.status == "pending" else "committed"
                )
            if result.commit_id:
                commits.append(result.commit_id)
        except (
            ValueError,
            PermissionError,
            FileNotFoundError,
            ConcurrentMutationError,
            HTTPException,
        ) as exc:
            outcomes[index] = "rejected"
            errors[index] = f"{type(exc).__name__}: {exc}"
        except Exception as exc:
            # Keep all writers running so the report identifies every break; assertions below reject these.
            outcomes[index] = "error"
            errors[index] = f"{type(exc).__name__}: {exc}"
    return CatalogResult(
        flatten_tree_to_bytes(repo.store, repo.history.get_root_hash()),
        outcomes,
        errors,
        sum(p["status"] == "pending" for p in ledger.pending.values()),
        commits,
    )
