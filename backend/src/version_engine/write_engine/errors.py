"""Write-engine error types.

These are part of the L5 write boundary. Keeping them outside the facade
prevents individual writer modules from importing the public engine module.
"""

from __future__ import annotations


class ConcurrentMutationError(RuntimeError):
    """Raised when a caller supplied a stale scope head precondition."""

    def __init__(
        self,
        *,
        scope_path: str,
        expected_head_commit_id: str,
        current_head_commit_id: str,
    ):
        self.scope_path = scope_path
        self.expected_head_commit_id = expected_head_commit_id
        self.current_head_commit_id = current_head_commit_id
        super().__init__(
            "Scope changed since the command started. Pull the latest state "
            "or retry the write against the current scope head."
        )


class NativeRevisionConflictError(RuntimeError):
    """The captured native target, OID, generation or HEAD no longer matches."""


class CrossScopeSubmissionError(PermissionError):
    """Raised when a version submission modifies paths owned by another scope."""

    def __init__(self, *, scope_path: str, rejected_paths: list[str]):
        self.scope_path = scope_path
        self.rejected_paths = rejected_paths
        super().__init__(
            "submission touches paths outside its scope; split the work across "
            f"scope remotes: {rejected_paths[:5]}"
        )


class NonFastForwardSubmissionError(RuntimeError):
    """Raised when a Git transport update loses the ref-update race."""

    def __init__(
        self,
        *,
        expected_head_commit_id: str,
        current_head_commit_id: str,
    ):
        self.expected_head_commit_id = expected_head_commit_id
        self.current_head_commit_id = current_head_commit_id
        super().__init__(
            "non-fast-forward update rejected; fetch and rebase before pushing again"
        )
