"""Product operations over admitted Git snapshots and native ref publication.

A bound adapter carries an explicitly authorized principal, never a project-wide
service identity. Workers bind their initiating user; machine routes bind the
actual RuntimeGrant. Every write goes through NativeOperationWriter.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field

from src.platform.authorization.models import ProjectAction
from src.version_engine.domain.intents import ProjectWriteState
from src.version_engine.read.native_tree_reader import NativeTreeReader
from src.version_engine.write_engine.errors import NativeRevisionConflictError
from src.version_engine.write_engine.ref_transaction import admitted_actor


@dataclass
class WriteResult:
    commit_id: str = ""
    status: str = "ok"
    merged: bool = False
    conflicts: int = 0
    paths: list[str] = field(default_factory=list)


class MissingBlobError(RuntimeError):
    """A producer did not supply the verified input body it owns."""


@dataclass(frozen=True)
class BlobRef:
    """Private producer input, not a separately published canonical object."""

    hash: str
    size: int
    content: bytes | None = field(default=None, repr=False)


class ProductOperationAdapter:
    def __init__(self, repo_manager, *, grant=None, operation_key: str | None = None):
        self._repos, self._grant, self._operation_key = repo_manager, grant, operation_key

    def for_grant(self, grant, *, operation_key=None):
        return type(self)(self._repos, grant=grant, operation_key=operation_key)

    def producer_request_key(self, operation, message):
        if not self._operation_key:
            raise ValueError("producer operation key is required")
        step = json.dumps({"operation": operation, "message": message}, sort_keys=True)
        return str(uuid.uuid5(uuid.NAMESPACE_URL, self._operation_key + ":" + step))

    @staticmethod
    def write_result(product):
        return WriteResult(
            commit_id=product["commit_oid"] or "",
            paths=[
                base64.b64decode(path, validate=True).decode("utf-8", "surrogateescape")
                for _action, path in product["changes"]
            ],
        )

    def for_user(self, project_id, user_id, *, operation_key=None):
        from src.platform.project.write_lease import active_project_write_lease
        from src.version_engine.domain.initialization import InitializationGrant

        lease = active_project_write_lease(project_id)
        if lease and lease.initialization_actor == user_id and lease.initialization_operation_key:
            return self.for_grant(
                InitializationGrant(project_id, user_id, lease), operation_key=operation_key
            )
        from src.platform.authorization.factory import build_authorization_service

        grant = build_authorization_service(self._repos._supabase.client).authorize(
            project_id, user_id, ProjectAction.CONTENT_READ
        )
        return self.for_grant(grant, operation_key=operation_key)

    def _bound_grant(self, project_id, state=None):
        grant = self._grant or getattr(state, "repository_grant", None)
        admitted_actor(grant, project_id, write=False)
        return grant

    @contextmanager
    def open_read(self, project_id, grant, *, selector=b"HEAD", bulk=False):
        admitted_actor(grant, project_id, write=False)
        with self._repos.open_native_read(project_id, grant) as snapshot:
            from src.version_engine.domain.errors import (
                NativeObjectNotFoundError,
                ObjectNotFoundError,
            )

            try:
                if bulk:
                    snapshot.backend = snapshot.backend.pinned_reader(snapshot)
                yield NativeTreeReader(snapshot, selector=selector)
            except ObjectNotFoundError as exc:
                raise NativeObjectNotFoundError(str(exc)) from exc

    async def native_ref_metadata(self, project_id, grant):
        return await asyncio.to_thread(self._repos.get_native_ref_metadata, project_id, grant)

    async def native_operation_status(self, project_id, grant, request_key):
        return await asyncio.to_thread(
            self._repos.get_native_operation_status, project_id, grant, request_key
        )

    async def apply_native_command(
        self,
        project_id,
        grant,
        *,
        request_key,
        base,
        input_sha256,
        splice,
        message="",
        write_lease_factory=None,
    ):
        from src.platform.project.write_lease import ProjectWriteLease
        from src.version_engine.write_engine.native_operation_writer import NativeOperationWriter

        admitted_actor(grant, project_id, write=False)
        service = await asyncio.to_thread(self._repos.get_native_service, project_id)
        if service is None:
            raise RuntimeError("repository migration required")
        writer = NativeOperationWriter(service)
        request = dict(
            request_key=request_key, base=base, input_sha256=input_sha256, message=message
        )
        result = await asyncio.to_thread(writer.replay, grant, **request)
        if result is not None:
            return result
        admitted_actor(grant, project_id, write=True)
        async with (write_lease_factory or ProjectWriteLease)(project_id, "product.native"):
            worker = asyncio.create_task(
                asyncio.to_thread(writer.apply, grant, splice=splice, **request)
            )
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                try:
                    await worker
                finally:
                    raise

    def get_project_write_state(self, project_id, user_id):
        bound = self.for_user(project_id, user_id)
        grant = bound._grant
        with bound.open_read(project_id, grant) as reader:
            revision = reader.get_read_revision(project_id)
        return ProjectWriteState(
            project_id,
            "",
            org_id=grant.org_id,
            role=grant.role.value,
            can_write=grant.allows(ProjectAction.CONTENT_WRITE),
            root_hash=revision["tree_oid"],
            head_commit_id=revision["expected_oid"] or "",
            repository_grant=grant,
            repository_revision=revision,
        )

    async def _publish(
        self,
        project_id,
        operation,
        arguments,
        *,
        who="",
        scope="",
        message="",
        base_commit_id=None,
        project_write_state=None,
        policy="",
        **options,
    ):
        if scope:
            raise PermissionError("native Scope operations are not enabled")
        if policy:
            raise ValueError("use an explicit Git conflict workflow")
        from src.version_engine.adapters.product.commands import VersionWriteCommandService
        from src.version_engine.adapters.product.native_commands import compile_native_command

        grant = self._bound_grant(project_id, project_write_state)
        arguments = {**arguments, "message": message}
        digest, splice, _response = compile_native_command(
            VersionWriteCommandService(self), operation, arguments
        )

        def starting_revision():
            revision = getattr(project_write_state, "repository_revision", None)
            if revision is None:
                with self.open_read(project_id, grant) as reader:
                    revision = reader.get_read_revision(project_id)
            return revision

        base = await asyncio.to_thread(starting_revision)
        if self._operation_key:
            # Durable task identity plus logical step. Input digest is checked
            # separately: changing a retry's content cannot create a new task.
            key = self.producer_request_key(operation, message)
            actor = admitted_actor(grant, project_id, write=False)
            service = await asyncio.to_thread(self._repos.get_native_service, project_id)
            base = await asyncio.to_thread(
                service.control.producer_base, project_id, actor, key, digest, base
            )
        else:
            key = str(uuid.uuid4())
        if base_commit_id is not None and base_commit_id != (base["expected_oid"] or ""):
            raise NativeRevisionConflictError("starting commit changed")
        result = await self.apply_native_command(
            project_id,
            grant,
            request_key=key,
            base=base,
            input_sha256=digest,
            splice=splice,
            message=message or operation,
        )
        if result["status"] != "committed":
            raise NativeRevisionConflictError("repository ref or HEAD changed")
        return self.write_result(result["product"])

    async def write_file(self, project_id, path, content, who="", **kwargs):
        return await self._publish(
            project_id,
            "write",
            {"path": path, "content": content, "node_type": "file"},
            who=who,
            **kwargs,
        )

    async def bulk_write(self, project_id, files, who="", deleted=None, **kwargs):
        if len(files) > 100000 or sum(len(body) for body in files.values()) > 256 * 1024**2:
            raise ValueError("native producer batch budget exceeded")
        return await self._publish(
            project_id,
            "bulk_write",
            {
                "files": [dict(path=p, content=b, node_type="file") for p, b in files.items()],
                "deleted": deleted or [],
            },
            who=who,
            **kwargs,
        )

    async def bulk_write_refs(
        self, project_id, file_refs, who="", deleted=None, verify_blobs=True, **kwargs
    ):
        files = {}
        for path, ref in file_refs.items():
            if ref.content is None or len(ref.content) != ref.size:
                raise MissingBlobError("producer input body is required")
            from src.version_engine.write_engine.git_object_format import hash_object

            if hash_object("blob", ref.content) != ref.hash:
                raise MissingBlobError("producer input digest mismatch")
            files[path] = ref.content
        return await self.bulk_write(project_id, files, who=who, deleted=deleted, **kwargs)

    async def stage_blob_from_bytes(self, project_id, content):
        self._bound_grant(project_id)
        from src.version_engine.write_engine.git_object_format import hash_object

        return BlobRef(hash_object("blob", content), len(content), content)

    async def delete(self, project_id, paths, who="", **kwargs):
        return await self._publish(
            project_id,
            "remove",
            {"paths": paths, "recursive": True, "force": True},
            who=who,
            **kwargs,
        )

    async def mkdir(self, project_id, path, who="", **kwargs):
        return await self._publish(
            project_id, "mkdir", {"path": path, "parents": True}, who=who, **kwargs
        )

    async def move(self, project_id, old_path, new_path, who="", **kwargs):
        return await self._publish(
            project_id, "move", {"old_path": old_path, "new_path": new_path}, who=who, **kwargs
        )

    async def copy(self, project_id, old_path, new_path, who="", **kwargs):
        return await self._publish(
            project_id,
            "copy",
            {"old_path": old_path, "new_path": new_path, "recursive": True},
            who=who,
            **kwargs,
        )

    async def touch(self, project_id, paths, who="", **kwargs):
        return await self._publish(project_id, "touch", {"paths": paths}, who=who, **kwargs)

    def _read(self, method, project_id, *args, **kwargs):
        with self.open_read(project_id, self._bound_grant(project_id)) as reader:
            return getattr(reader, method)(project_id, *args, **kwargs)

    def read_file(self, project_id, path):
        return self._read("read_file", project_id, path)

    def read_file_range(self, project_id, path, **kwargs):
        return self._read("read_file_range", project_id, path, **kwargs)

    def list_dir(self, project_id, path="", **kwargs):
        return self._read("list_dir", project_id, path, **kwargs)

    def list_tree(self, project_id, path="", max_depth=-1, **kwargs):
        return self._read("list_tree", project_id, path, max_depth, **kwargs)

    def stat(self, project_id, path, **kwargs):
        return self._read("stat", project_id, path, **kwargs)

    def get_head_commit_id(self, project_id):
        return self._read("get_head_commit_id", project_id)

    def get_root_hash(self, project_id):
        return self._read("get_root_hash", project_id)

    def get_read_revision(self, project_id):
        return self._read("get_read_revision", project_id)

    def get_scope_head_commit_id(self, project_id, scope_path):
        if scope_path:
            raise PermissionError("native Scope operations are not enabled")
        return self.get_head_commit_id(project_id)

    def get_scope_head_commit_id_for_path(self, project_id, path):
        return self.get_head_commit_id(project_id)

    def _scope_read(self, method, project_id, scope, *args, **kwargs):
        if scope:
            raise PermissionError("native Scope operations are not enabled")
        return self._read(method, project_id, *args, **kwargs)

    def read_file_in_scope(self, project_id, scope, path):
        return self._scope_read("read_file", project_id, scope, path)

    def read_file_range_in_scope(self, project_id, scope, path, **kwargs):
        return self._scope_read("read_file_range", project_id, scope, path, **kwargs)

    def list_dir_in_scope(self, project_id, scope, path="", **kwargs):
        return self._scope_read("list_dir", project_id, scope, path, **kwargs)

    def list_tree_in_scope(self, project_id, scope, path="", max_depth=-1, **kwargs):
        return self._scope_read("list_tree", project_id, scope, path, max_depth, **kwargs)

    def stat_in_scope(self, project_id, scope, path, **kwargs):
        return self._scope_read("stat", project_id, scope, path, **kwargs)

    def get_path_timestamps(self, project_id, paths):
        # File timestamps are a derived history view, never a second authority.
        self._bound_grant(project_id)
        from src.version_engine.read.native_history import NativeHistory

        with self.open_read(project_id, self._bound_grant(project_id)) as reader:
            return NativeHistory(reader.snapshot).timestamps(paths)

    async def restore_commit(self, project_id, target):
        """Create a normal Git revert-style commit, preserving current ancestry."""
        from src.version_engine.read.native_history import NativeHistory
        from src.version_engine.write_engine.git_object_format import decode_commit
        from src.version_engine.write_engine.native_tree_diff import native_tree_diff

        grant = self._bound_grant(project_id)
        with self.open_read(project_id, grant) as reader:
            history = NativeHistory(reader.snapshot)
            history.require(target)
            base = reader.get_read_revision(project_id)
            # Only retain a path through commits, not blob bodies. The writer's
            # own snapshot verifies that path again before reading the tree.
            parents = {oid: node["parent_ids"] for oid, node in history.nodes.items()}
            roots = history.roots

        def splice(store, current):
            todo, seen = list(roots), set()
            while todo:
                oid = todo.pop()
                if oid in seen:
                    continue
                seen.add(oid)
                kind, body = store.get_object(oid)
                if kind != "commit":
                    raise ValueError("invalid rollback ancestry")
                info = decode_commit(body)
                if oid == target:
                    tree = info["tree"]
                    changes = native_tree_diff(store, current, tree)
                    return tree, [
                        (
                            {"added": "add", "deleted": "delete", "modified": "update"}[c["op"]],
                            c["path"],
                        )
                        for c in changes
                    ]
                todo.extend(parents[oid])
            raise ValueError("rollback target unavailable")

        result = await self.apply_native_command(
            project_id,
            grant,
            request_key=str(uuid.uuid4()),
            base=base,
            input_sha256=hashlib.sha256(("restore:" + target).encode()).hexdigest(),
            splice=splice,
            message="restore tree from " + target,
        )
        if result["status"] != "committed":
            raise NativeRevisionConflictError("repository changed during rollback")
        return WriteResult(commit_id=result["product"]["commit_oid"] or "")
