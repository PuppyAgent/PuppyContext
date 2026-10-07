"""Grant-bound history reads over canonical native Git objects."""

from __future__ import annotations

from starlette.concurrency import run_in_threadpool


class VersionAdminService:
    """Grant-bound administrative reads of standard Git objects."""

    def __init__(self, repo_manager, grant=None):
        self._repos, self._grant = repo_manager, grant

    def for_grant(self, grant):
        return type(self)(self._repos, grant)

    async def init_tree(self, project_id):
        from src.version_engine.write_engine.engine import VersionWriteEngine

        return await VersionWriteEngine(self._repos).initialize_project_tree(project_id)

    def _read(self, project_id, callback):
        from src.version_engine.adapters.product.operation_adapter import ProductOperationAdapter
        from src.version_engine.read.native_history import NativeHistory

        with ProductOperationAdapter(self._repos).open_read(project_id, self._grant) as reader:
            return callback(NativeHistory(reader.snapshot))

    async def get_history_view(self, project_id, path=None, limit=50, since_commit_id=""):
        return await run_in_threadpool(
            self._read,
            project_id,
            lambda h: (
                h.linear(limit, since_commit_id, path),
                h.head,
                h.snapshot.revision().tree_oid,
            ),
        )

    async def get_commit_history(self, project_id, path=None, limit=50, since_commit_id=""):
        return await run_in_threadpool(
            self._read, project_id, lambda h: h.linear(limit, since_commit_id, path)
        )

    async def get_project_head_commit_id(self, project_id):
        return await run_in_threadpool(self._read, project_id, lambda h: h.head)

    async def get_commit_parent_ids(self, project_id, commit_ids):
        return await run_in_threadpool(
            self._read,
            project_id,
            lambda h: {oid: h.require(oid)["parent_ids"] for oid in commit_ids},
        )

    async def get_commit_content(self, project_id, path, commit_id):
        return await run_in_threadpool(self._read, project_id, lambda h: h.content(commit_id, path))

    async def compute_diff(self, project_id, from_commit_id, to_commit_id):
        return await run_in_threadpool(
            self._read, project_id, lambda h: h.diff(from_commit_id, to_commit_id)
        )
