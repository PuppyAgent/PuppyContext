"""Native repository initialization facade.

SQL enrollment owns HEAD, lifecycle and admission. Content is published only by
NativeOperationWriter / RefTransactionService; no project-root CAS writer exists.
"""

from __future__ import annotations

import asyncio

from src.version_engine.write_engine.errors import (
    ConcurrentMutationError,
    CrossScopeSubmissionError,
    NonFastForwardSubmissionError,
)


class VersionWriteEngine:
    def __init__(self, repo_manager):
        self._repos = repo_manager

    async def initialize_project_tree(self, project_id: str) -> str:
        from src.version_engine.write_engine.git_object_format import hash_object

        service = await asyncio.to_thread(self._repos.get_native_service, project_id)
        if service is None:
            raise RuntimeError("native repository initialization required")
        return hash_object("tree", b"", object_format=service.object_format)


__all__ = [
    "ConcurrentMutationError",
    "CrossScopeSubmissionError",
    "NonFastForwardSubmissionError",
    "VersionWriteEngine",
]
