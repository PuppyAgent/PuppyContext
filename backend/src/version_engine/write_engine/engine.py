"""Git-native Write Engine facade.

This is the L5 publish authority for PuppyOne version writes. Protocol
adapters parse requests and product services build typed splices, but all
visible version facts still enter through this facade.
"""

from __future__ import annotations

import asyncio
import time

from src.version_engine.domain.intents import (
    ConflictResolutionIntent,
    OperationWriteIntent,
    RollbackIntent,
    TransactionResult,
    VersionSubmissionIntent,
)
from src.version_engine.infrastructure.supabase.repo_manager import VersionRepoManager
from src.version_engine.write_engine.conflict_resolution_writer import (
    ConflictResolutionWriter,
)
from src.version_engine.write_engine.errors import (
    ConcurrentMutationError,
    CrossScopeSubmissionError,
    NonFastForwardSubmissionError,
)
from src.version_engine.write_engine.ledger import (
    NoopVersionTransactionLedger,
    VersionTransactionLedger,
)
from src.version_engine.write_engine.operation_writer import OperationWriter, SpliceFn
from src.version_engine.write_engine.path_utils import normalize_path
from src.version_engine.write_engine.rollback_writer import RollbackWriter
from src.version_engine.write_engine.submission_writer import SubmissionWriter
from src.version_engine.write_engine.trace import (
    VersionTrace,
    active_version_trace,
    trace_mark,
    use_version_trace,
)
from src.utils.logger import log_info


class VersionWriteEngine:
    """Stable L5 entrypoint for operation and version submissions.

    The facade owns dependency wiring, public API stability, and tracing
    boundaries. Intent-specific orchestration lives in focused writer modules
    so root-first CAS, submitted-tree merges, rollback, and conflict
    resolution can evolve independently.
    """

    def __init__(
        self,
        repo_manager: VersionRepoManager,
        ledger: VersionTransactionLedger | None = None,
    ):
        self._repos = repo_manager
        self._ledger = (
            ledger
            or getattr(repo_manager, "transaction_ledger", None)
            or NoopVersionTransactionLedger()
        )
        self._operation_writer = OperationWriter(self._repos, self._ledger)
        self._submission_writer = SubmissionWriter(self._repos, self._ledger)
        self._rollback_writer = RollbackWriter(self._repos)
        self._conflict_resolution_writer = ConflictResolutionWriter(
            self._repos,
            self._ledger,
            self._submission_writer,
        )

    async def initialize_project_tree(self, project_id: str) -> str:
        """Initialize only a genuinely unborn legacy root under checked SQL CAS.

        Initialization is not repair. Existing roots survive missing/corrupt or
        unavailable storage, and a publication winning a race must not be reset.
        Native enrollment uses its separate reviewed authority path.
        """
        repo = self._repos.get_repo(project_id)
        initialize = getattr(repo.history, "initialize_root_hash", None)
        if not callable(initialize):
            raise RuntimeError("checked root initialization unavailable")
        return await asyncio.to_thread(initialize)

    async def apply_operation(
        self,
        intent: OperationWriteIntent,
        splice: SpliceFn,
    ) -> TransactionResult:
        """Apply a typed product operation through root-first CAS."""

        started_ms = int(time.time() * 1000)
        scope_norm = normalize_path(intent.scope_path)
        log_info(
            f"[version_engine][{intent.operation_type}] start "
            f"project={intent.project_id} scope={scope_norm!r} "
            f"actor={intent.actor}",
        )

        return await self._operation_writer.apply_scoped_operation_root_first(
            intent=intent,
            splice=splice,
            started_ms=started_ms,
        )

    async def apply_project_operation(
        self,
        intent: OperationWriteIntent,
        splice: SpliceFn,
    ) -> TransactionResult:
        """Apply a product API operation against the project root."""

        started_ms = int(time.time() * 1000)
        log_info(
            f"[version_engine][{intent.operation_type}:project] start "
            f"project={intent.project_id} actor={intent.actor}",
        )
        existing_trace = active_version_trace()
        trace = existing_trace or VersionTrace(
            f"{intent.operation_type}:project",
            project_id=intent.project_id,
            scope_path="",
            actor=intent.actor,
            source_channel=intent.source_channel,
        )
        with use_version_trace(trace):
            trace_mark(
                "engine.project_operation.start",
                operation_type=intent.operation_type,
            )
            try:
                with trace.phase("engine.apply_project_operation"):
                    result = (
                        await self._operation_writer.apply_project_operation_optimistic(
                            intent=intent,
                            splice=splice,
                            started_ms=started_ms,
                        )
                    )
            except Exception:
                if existing_trace is None:
                    trace.finish(status="error")
                raise
            if existing_trace is None:
                trace.finish(
                    status=result.status,
                    commit_id=result.commit_id,
                    changes=len(result.changes or []),
                )
            return result

    async def submit_version(
        self,
        intent: VersionSubmissionIntent,
    ) -> TransactionResult:
        """Apply a proposed Git tree via root-first server-side decision."""

        started_ms = int(time.time() * 1000)
        scope_norm = normalize_path(intent.scope_path)
        log_info(
            f"[version_engine][{intent.source_channel}_submit] start "
            f"project={intent.project_id} scope={scope_norm!r} "
            f"actor={intent.actor}",
        )

        return await self._submission_writer.submit_version_root_first(
            intent,
            started_ms,
        )

    async def rollback(
        self,
        intent: RollbackIntent,
    ) -> TransactionResult:
        """Restore one scope to a historical commit via root-first CAS."""

        started_ms = int(time.time() * 1000)
        scope_norm = normalize_path(intent.scope_path)
        log_info(
            f"[version_engine][rollback] start "
            f"project={intent.project_id} scope={scope_norm!r} "
            f"actor={intent.actor} target={intent.target_commit_id[:12]}",
        )

        return await self._rollback_writer.rollback_root_first(intent, started_ms)

    async def resolve(
        self,
        intent: ConflictResolutionIntent,
    ) -> TransactionResult:
        """Apply a manual or hosted-agent resolution to a pending conflict."""

        return await self._conflict_resolution_writer.resolve(intent)


__all__ = [
    "ConcurrentMutationError",
    "CrossScopeSubmissionError",
    "NonFastForwardSubmissionError",
    "VersionWriteEngine",
]
