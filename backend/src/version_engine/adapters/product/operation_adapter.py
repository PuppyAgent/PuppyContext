"""
ProductOperationAdapter — Product Operation Adapter for typed Git-tree operations.

All channels (Web UI / Agent / Sandbox / MCP / Datasource / Ingest /
Table / Seed) submit product file actions through this class. The public
contract is typed product operations over the Write Engine.

Architecture:
    Each typed write op (``write_file``, ``delete``, ``mkdir``, ``move``,
    ``bulk_write``) constructs an O(D) tree splice via
    ``services.tree_splice`` and routes it through the Git-native transaction
    engine. Product/Web/API writes are project-root transactions by default.
    Access Point and Git adapters pass an explicit scope when they need a
    scoped repo facade. No host-client index bootstrap, no Git transport materialization,
    no full blob downloads, no protocol-handler publish path.

    Read ops resolve straight against the persisted Merkle root via
    ``VersionTreeReader`` (S3-cached tree-node reads only, no flattening).

    External protocol clients submit version intents through adapters.
    Product operations do not own publish semantics directly.

Usage:
    ops = ProductOperationAdapter(repo_manager)
    result = await ops.write_file("proj_1", "readme.md", b"# Hi", who="user:123")
    content = ops.read_file("proj_1", "readme.md")
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.version_engine.write_engine.engine import VersionWriteEngine
from src.version_engine.domain.intents import OperationWriteIntent, ProjectWriteState
from src.version_engine.infrastructure.supabase.repo_manager import VersionRepoManager
from src.version_engine.admission.validation import validate_path
from src.version_engine.read.tree_reader import VersionEntry, VersionTreeReader
from src.version_engine.adapters.product.tree_patch import (
    splice_batch,
    splice_copy,
    splice_mkdir,
    splice_move,
    splice_put_blob,
    splice_put_blob_ref,
    splice_remove,
    splice_touch,
)
from src.version_engine.write_engine.tree_objects import find_missing_tree_objects
from src.config import settings
from src.utils.logger import log_error


@dataclass
class WriteResult:
    commit_id: str = ""
    status: str = "ok"
    merged: bool = False
    conflicts: int = 0
    paths: list[str] = field(default_factory=list)


class MissingBlobError(RuntimeError):
    """Raised by ``ProductOperationAdapter.bulk_write_refs`` when a referenced blob
    isn't in the project's ObjectStore.

    Distinct from ``FileNotFoundError`` (which is for path-level
    misses) so callers can tell the two apart and respond
    differently — a missing path is normal (idempotent delete), a
    missing blob is an upload-pipeline bug.
    """


@dataclass(frozen=True)
class BlobRef:
    """Reference to an already-staged blob in a project's version object store.

    Carries enough metadata to record a commit (hash for the tree
    pointer, size for audit/quota) without having to materialize the
    payload in the Python process.

    The ``hash`` MUST identify a blob that's already present in the
    project's ``ObjectStore`` — i.e. some upstream stage step has
    already written it (or confirmed it exists). Producing a
    ``BlobRef`` IS the contract: "I have already put bytes such that
    ``store.get(hash) == those bytes``." Anyone holding a
    ``BlobRef`` can safely commit it without re-uploading.

    Why a dataclass and not a tuple: ``size`` is purely informational
    today (tree nodes only store ``hash``), but we surface it in
    audit logs (``"uploaded N files (X bytes)"``), and future tree
    formats may inline size for fast directory listing. The named
    fields keep that future change cheap.
    """

    hash: str
    size: int


class ProductOperationAdapter:
    """Unified entry point for version tree operations."""

    def __init__(self, repo_manager: VersionRepoManager):
        self._repos = repo_manager
        self._reader = VersionTreeReader(repo_manager)
        self._engine = VersionWriteEngine(repo_manager)

    def get_project_write_state(
        self,
        project_id: str,
        user_id: str,
    ) -> ProjectWriteState | None:
        return self._repos.get_project_write_state(project_id, user_id)

    async def _apply_operation(
        self,
        project_id: str,
        scope: str,
        splice_fn,
        *,
        who: str,
        message: str,
        op_type: str,
        audit_detail: dict | None = None,
        expected_head_commit_id: str | None = None,
        allow_same_tree_commit: bool = False,
        defer_projection: bool = False,
        policy: str = "",
        source_channel: str = "papi",
        project_write_state: ProjectWriteState | None = None,
        pusher_client_id: str = "",
    ):
        intent = OperationWriteIntent(
            project_id=project_id,
            scope_path=scope,
            actor=who,
            source_channel=source_channel,
            operation_type=op_type,
            message=message,
            audit_detail=audit_detail or {},
            expected_head_commit_id=expected_head_commit_id,
            allow_same_tree_commit=allow_same_tree_commit,
            defer_projection=defer_projection,
            policy_override=policy,
            project_write_state=project_write_state,
            pusher_client_id=pusher_client_id,
        )
        if scope:
            result = await self._engine.apply_operation(intent, splice_fn)
        else:
            result = await self._engine.apply_project_operation(intent, splice_fn)

        # Optional post-commit tree-closure tripwire. The blob safety net
        # (``_verify_blobs_present``) only proves leaf blobs exist; it does NOT
        # prove the published root's subtree TREE objects do. A builder/graft
        # regression that referenced an unpersisted subtree would publish a
        # dangling tree — the on-disk shape of a "Damaged folder". When enabled
        # (off by default — a full closure walk is O(tree) per write), verify
        # the freshly-published root resolves end to end and fail loud if not,
        # so corruption surfaces at the write that caused it instead of at a
        # later read. The builders are proven complete by
        # ``tests/version_engine/test_tree_closure.py``; this flag is the
        # belt-and-suspenders runtime guard for prod paranoia / incident triage.
        if settings.VERSION_VERIFY_TREE_CLOSURE_ON_WRITE:
            self._assert_published_tree_closure(project_id)
        return result

    def _assert_published_tree_closure(self, project_id: str) -> None:
        try:
            root = self._reader.get_root_hash(project_id)
            if not root:
                return
            store = self._repos.get_server_repo(project_id).store
            missing = find_missing_tree_objects(store, root)
        except Exception as exc:  # noqa: BLE001 - never mask the write itself.
            log_error(
                f"[ProductOperationAdapter] tree-closure check errored for "
                f"{project_id}: {exc}"
            )
            return
        if missing:
            log_error(
                f"[ProductOperationAdapter] published root {root[:12]} for "
                f"{project_id} references {len(missing)} missing object(s) "
                f"(e.g. {missing[:5]}) — dangling tree"
            )
            raise MissingBlobError(
                f"published root {root[:12]} references {len(missing)} missing "
                "object(s); refusing to leave a dangling tree unflagged"
            )

    # ══════════════════════════════════════════════
    # Write operations — typed splice → Write Engine
    # ══════════════════════════════════════════════

    async def write_file(
        self,
        project_id: str,
        path: str,
        content: bytes,
        who: str,
        scope: str = "",
        message: str = "",
        base_commit_id: str | None = None,
        defer_projection: bool = False,
        policy: str = "",
        source_channel: str = "papi",
        project_write_state: ProjectWriteState | None = None,
        pusher_client_id: str = "",
    ) -> WriteResult:
        """Write a single file (create or update).

        Product/API writes are project-root user actions and stay one
        project-root commit/history/audit row. Scoped access-point callers
        pass ``scope`` explicitly, preserving their per-scope CAS boundary
        without leaking internal access-point scopes into frontend history.

        ``policy`` lets the caller opt into a stricter conflict policy
        (e.g. ``"manual_review"``) than the configured rule set would
        select on its own — the engine queues conflicts in
        the conflict table instead of silently merging via LWW.
        """
        path = validate_path(path)
        target_scope, rel_path = self._resolve_write_target(
            project_id, path, scope,
        )

        def splice_fn(store, root_hash):
            return splice_put_blob(store, root_hash, rel_path, content)

        result = await self._apply_operation(
            project_id, target_scope, splice_fn,
            who=who,
            message=message or f"write {path}",
            op_type="write_file",
            audit_detail={"path": path, "size": len(content)},
            expected_head_commit_id=base_commit_id,
            defer_projection=defer_projection,
            policy=policy,
            source_channel=source_channel,
            project_write_state=project_write_state,
            pusher_client_id=pusher_client_id,
        )
        return _to_result(result, [path])

    async def delete(
        self,
        project_id: str,
        paths: list[str],
        who: str,
        scope: str = "",
        message: str = "",
        base_commit_id: str | None = None,
        defer_projection: bool = False,
        policy: str = "",
        source_channel: str = "papi",
        project_write_state: ProjectWriteState | None = None,
    ) -> WriteResult:
        """Delete one or more files.

        Product/API deletes default to one project-root transaction, even
        when the deleted folder contains child access-point scopes. Scoped
        access point callers pass ``scope`` explicitly, in which case all
        paths are relative to that scope and produce one scoped commit.

        ``policy`` / ``source_channel`` flow through to the engine's
        conflict-policy selection (e.g. ``manual_review`` queues
        ambiguous deletes rather than silently winning LWW).
        """
        clean = [validate_path(p) for p in paths]
        if scope:
            return await self._delete_in_scope(
                project_id, scope, clean, who, message, base_commit_id,
                defer_projection,
                policy=policy,
                source_channel=source_channel,
                project_write_state=project_write_state,
            )

        groups = self._group_paths_by_scope(project_id, clean)
        if base_commit_id is not None and len(groups) > 1:
            raise ValueError(
                "base_commit_id is ambiguous for multi-scope delete operations"
            )
        first_result: WriteResult | None = None
        for target_scope, rel_paths in groups.items():
            r = await self._delete_in_scope(
                project_id, target_scope, rel_paths, who, message, base_commit_id,
                defer_projection,
                policy=policy,
                source_channel=source_channel,
                project_write_state=project_write_state,
            )
            first_result = first_result or r
        return first_result or WriteResult(paths=clean)

    async def _delete_in_scope(
        self,
        project_id: str,
        scope: str,
        rel_paths: list[str],
        who: str,
        message: str,
        base_commit_id: str | None = None,
        defer_projection: bool = False,
        *,
        policy: str = "",
        source_channel: str = "papi",
        project_write_state: ProjectWriteState | None = None,
    ) -> WriteResult:
        def splice_fn(store, root_hash):
            return splice_remove(store, root_hash, rel_paths)

        result = await self._apply_operation(
            project_id, scope, splice_fn,
            who=who,
            message=message or f"delete {len(rel_paths)} files",
            op_type="delete",
            audit_detail={"paths": rel_paths},
            expected_head_commit_id=base_commit_id,
            defer_projection=defer_projection,
            policy=policy,
            source_channel=source_channel,
            project_write_state=project_write_state,
        )
        full_paths = [
            self._join_scope_path(scope, p) for p in rel_paths
        ]
        return _to_result(result, full_paths)

    async def mkdir(
        self,
        project_id: str,
        path: str,
        who: str,
        scope: str = "",
        message: str = "",
        base_commit_id: str | None = None,
        defer_projection: bool = False,
        policy: str = "",
        source_channel: str = "papi",
        project_write_state: ProjectWriteState | None = None,
    ) -> WriteResult:
        """Create a directory (writes a ``.keep`` placeholder).

        Product/API calls create the directory in a project-root
        transaction, same as ``write_file``. Explicit scoped callers keep
        their scoped CAS boundary. Existing directories are no-ops (no
        commit created). Errors out if a file already occupies the path.
        """
        path = validate_path(path)
        keep_full = f"{path}/.keep"
        target_scope, rel_keep = self._resolve_write_target(
            project_id, keep_full, scope,
        )
        rel_dir = (
            rel_keep[: -len("/.keep")]
            if rel_keep.endswith("/.keep")
            else rel_keep
        )

        def splice_fn(store, root_hash):
            return splice_mkdir(store, root_hash, rel_dir)

        result = await self._apply_operation(
            project_id, target_scope, splice_fn,
            who=who,
            message=message or f"mkdir {path}",
            op_type="mkdir",
            audit_detail={"path": path},
            expected_head_commit_id=base_commit_id,
            defer_projection=defer_projection,
            policy=policy,
            source_channel=source_channel,
            project_write_state=project_write_state,
        )
        return _to_result(result, [path])

    async def move(
        self,
        project_id: str,
        old_path: str,
        new_path: str,
        who: str,
        scope: str = "",
        message: str = "",
        base_commit_id: str | None = None,
        defer_projection: bool = False,
        policy: str = "",
        source_channel: str = "papi",
        project_write_state: ProjectWriteState | None = None,
    ) -> WriteResult:
        """Move / rename a file or folder.

        Operates on tree-node hashes — the underlying blobs are NOT
        downloaded or re-uploaded. A folder rename of a 1000-file
        subtree costs the same as a 1-file rename.
        """
        old_path = validate_path(old_path)
        new_path = validate_path(new_path)
        if _is_descendant_path(old_path, new_path):
            raise ValueError(
                f"cannot move {old_path!r} into its own subtree: {new_path!r}",
            )

        if scope:
            old_scope, old_rel = scope, old_path
            new_scope, new_rel = scope, new_path
        else:
            old_scope, old_rel = self._select_write_scope(project_id, old_path)
            new_scope, new_rel = self._select_write_scope(project_id, new_path)

        if old_scope != new_scope:
            raise ValueError(
                f"cross-scope move not supported: "
                f"{old_path!r} (scope={old_scope!r}) → "
                f"{new_path!r} (scope={new_scope!r})",
            )

        # Capture the source blob hash from the CURRENT scope tree
        # BEFORE we hand off to the engine. If a concurrent writer
        # renames or deletes ``old_rel`` between now and our CAS retry's
        # splice, the salvage path below uses this hash to recreate the
        # rename's add-side at ``new_rel``. This preserves the user's
        # intent ("the file I started from should end up at new_rel")
        # even when their src token is no longer in the tree.
        salvage_blob_hash = self._lookup_blob_hash(
            project_id, old_scope, old_rel,
        )

        def splice_fn(store, root_hash):
            try:
                return splice_move(store, root_hash, old_rel, new_rel)
            except FileNotFoundError:
                if not salvage_blob_hash:
                    raise
                # ``old_rel`` vanished underneath us (concurrent rename
                # or delete by another writer). Recreate the rename's
                # add-side using the blob we captured at submit time so
                # the new file lands as intended. The delete-side is
                # already realized by the concurrent op — no further work.
                from src.utils.logger import log_info
                log_info(
                    f"[move] source {old_rel!r} missing in scope tree "
                    f"during retry; salvaging via base blob "
                    f"{salvage_blob_hash[:12]} → {new_rel!r}",
                )
                return splice_put_blob_ref(
                    store, root_hash, new_rel, salvage_blob_hash,
                )

        result = await self._apply_operation(
            project_id, old_scope, splice_fn,
            who=who,
            message=message or f"move {old_path} → {new_path}",
            op_type="move",
            audit_detail={"old_path": old_path, "new_path": new_path},
            expected_head_commit_id=base_commit_id,
            defer_projection=defer_projection,
            policy=policy,
            source_channel=source_channel,
            project_write_state=project_write_state,
        )

        # ``post_commit_move`` (rename of ``repository_scopes`` rows under
        # the old prefix) is now dispatched by ``run_post_push_hook``
        # itself — the L4 layer stashed ``{old_path, new_path}`` into
        # ``audit_detail`` above and the hook reads it back from the
        # committed history entry. L4 no longer reaches into L6.
        return _to_result(result, [old_path, new_path])

    async def copy(
        self,
        project_id: str,
        old_path: str,
        new_path: str,
        who: str,
        scope: str = "",
        message: str = "",
        base_commit_id: str | None = None,
        defer_projection: bool = False,
        policy: str = "",
        source_channel: str = "papi",
        project_write_state: ProjectWriteState | None = None,
    ) -> WriteResult:
        """Copy a file or folder without downloading blob contents."""
        old_path = validate_path(old_path)
        new_path = validate_path(new_path)

        if scope:
            old_scope, old_rel = scope, old_path
            new_scope, new_rel = scope, new_path
        else:
            old_scope, old_rel = self._select_write_scope(project_id, old_path)
            new_scope, new_rel = self._select_write_scope(project_id, new_path)

        if old_scope != new_scope:
            raise ValueError(
                f"cross-scope copy not supported: "
                f"{old_path!r} (scope={old_scope!r}) -> "
                f"{new_path!r} (scope={new_scope!r})",
            )

        def splice_fn(store, root_hash):
            return splice_copy(store, root_hash, old_rel, new_rel)

        result = await self._apply_operation(
            project_id, old_scope, splice_fn,
            who=who,
            message=message or f"copy {old_path} -> {new_path}",
            op_type="copy",
            audit_detail={"old_path": old_path, "new_path": new_path},
            expected_head_commit_id=base_commit_id,
            defer_projection=defer_projection,
            policy=policy,
            source_channel=source_channel,
            project_write_state=project_write_state,
        )
        return _to_result(result, [old_path, new_path])

    async def touch(
        self,
        project_id: str,
        paths: list[str],
        who: str,
        scope: str = "",
        message: str = "",
        base_commit_id: str | None = None,
        defer_projection: bool = False,
        policy: str = "",
        source_channel: str = "papi",
        project_write_state: ProjectWriteState | None = None,
    ) -> WriteResult:
        """Update mtime for existing files without changing blob content."""
        clean = [validate_path(p) for p in paths]
        if not clean:
            return WriteResult()

        if scope:
            target_scope = scope
            rel_paths = clean
        else:
            groups = self._group_paths_by_scope(project_id, clean)
            if len(groups) != 1:
                raise ValueError("touch across multiple scopes is not supported")
            target_scope, rel_paths = next(iter(groups.items()))

        def splice_fn(store, root_hash):
            return splice_touch(store, root_hash, rel_paths)

        result = await self._apply_operation(
            project_id, target_scope, splice_fn,
            who=who,
            message=message or f"touch {len(clean)} files",
            op_type="touch",
            audit_detail={"paths": clean},
            expected_head_commit_id=base_commit_id,
            allow_same_tree_commit=True,
            defer_projection=defer_projection,
            policy=policy,
            source_channel=source_channel,
            project_write_state=project_write_state,
        )
        full_paths = [self._join_scope_path(target_scope, p) for p in rel_paths]
        return _to_result(result, full_paths)

    async def bulk_write(
        self,
        project_id: str,
        files: dict[str, bytes],
        who: str,
        scope: str = "",
        deleted: list[str] | None = None,
        message: str = "",
        defer_projection: bool = False,
        policy: str = "",
        source_channel: str = "papi",
        project_write_state: ProjectWriteState | None = None,
    ) -> WriteResult:
        """Batch write + optional batch delete.

        When ``scope`` is empty, paths are committed as one project-root
        product transaction. Scoped access-point callers pass ``scope``
        explicitly to keep their local CAS boundary.
        """
        clean = {validate_path(k): v for k, v in files.items()}
        clean_del = [validate_path(p) for p in (deleted or [])]
        if scope:
            return await self._bulk_write_in_scope(
                project_id, scope,
                {k: clean[k] for k in clean},
                clean_del,
                who, message, defer_projection,
                policy=policy, source_channel=source_channel,
                project_write_state=project_write_state,
            )

        write_groups = self._group_paths_by_scope(
            project_id, list(clean.keys()),
        )
        del_groups = self._group_paths_by_scope(project_id, clean_del)
        all_scopes = set(write_groups.keys()) | set(del_groups.keys())

        first_result: WriteResult | None = None
        for target_scope in all_scopes:
            rel_files = {
                rel: clean[self._join_scope_path(target_scope, rel)]
                for rel in write_groups.get(target_scope, [])
            }
            rel_dels = del_groups.get(target_scope, [])
            r = await self._bulk_write_in_scope(
                project_id, target_scope,
                rel_files, rel_dels, who, message,
                defer_projection,
                policy=policy, source_channel=source_channel,
                project_write_state=project_write_state,
            )
            first_result = first_result or r
        return first_result or WriteResult(
            paths=list(clean.keys()) + clean_del,
        )

    async def _bulk_write_in_scope(
        self,
        project_id: str,
        scope: str,
        rel_files: dict[str, bytes],
        rel_dels: list[str],
        who: str,
        message: str,
        defer_projection: bool = False,
        *,
        policy: str = "",
        source_channel: str = "papi",
        project_write_state: ProjectWriteState | None = None,
    ) -> WriteResult:
        ops: list[tuple] = []
        ops.extend(("put", path, content) for path, content in rel_files.items())
        ops.extend(("rm", path) for path in rel_dels)
        if not ops:
            return WriteResult()

        def splice_fn(store, root_hash):
            return splice_batch(store, root_hash, ops)

        result = await self._apply_operation(
            project_id, scope, splice_fn,
            who=who,
            message=message or f"bulk write {len(rel_files)} files",
            op_type="bulk_write",
            policy=policy,
            source_channel=source_channel,
            audit_detail={
                "writes": len(rel_files),
                "deletes": len(rel_dels),
            },
            defer_projection=defer_projection,
            project_write_state=project_write_state,
        )
        full_paths = [
            self._join_scope_path(scope, p)
            for p in list(rel_files.keys()) + rel_dels
        ]
        return _to_result(result, full_paths)

    # ══════════════════════════════════════════════
    # Hash-first APIs (Layer 1 + Layer 2 of the upload pipeline)
    # ══════════════════════════════════════════════
    #
    # The byte-taking ``write_file`` / ``bulk_write`` above are
    # convenience wrappers for "I have raw bytes in process memory".
    # They internally stage each blob (write to ObjectStore by hash)
    # and then commit by reference. The methods below expose those
    # two phases separately for callers that staged elsewhere — most
    # importantly, the multipart-upload path where blobs are
    # materialized in S3 directly and we never want them in the
    # Python process.
    #
    # Public surface for upload-pipeline callers:
    #   ``stage_blob_from_bytes(content)`` — returns a ``BlobRef`` after
    #     a single ``ObjectStore.put`` call. Same as what
    #     ``write_file`` does internally.
    #   ``bulk_write_refs(file_refs)`` — commit a tree update referencing
    #     already-staged blobs by hash. ``verify_blobs=True`` (default)
    #     does a HEAD round-trip per blob as a safety net against
    #     dangling commits — turn it off in known-good batches if the
    #     extra latency matters.
    #
    # The upload path (browser multipart, future CLI binary push) uses
    # ``stage_blob_from_s3`` (in ``platform/upload/jobs.py`` —
    # different module, S3-aware) to ``CopyObject`` from the upload
    # key into the version object key without touching the bytes, and
    # then calls ``bulk_write_refs`` here.

    async def stage_blob_from_bytes(
        self,
        project_id: str,
        content: bytes,
    ) -> BlobRef:
        """Write ``content`` to the project's version object store and
        return a ``BlobRef`` pointing at it.

        The store call is content-addressed and idempotent: writing
        the same bytes twice computes the same hash and is a no-op
        on the second call (so caller-side dedup is unnecessary —
        re-uploading the same file is free).

        Use this when you genuinely have bytes in memory (CLI text
        writes, connector outputs, internal templates). For multipart
        uploads where the bytes already live in S3, use
        ``platform.upload.jobs.stage_blob_from_s3`` instead — it
        uses S3 ``CopyObject`` to put the blob at the canonical object key without
        ever loading it into the backend process.

        Implementation note: we deliberately bypass the Write Engine
        here. A pure stage doesn't change any tree, so the scope
        lock + ``scope_hash`` DB read that a write transaction does
        upfront would be wasted work. Instead we go straight to the
        ObjectStore — exactly what the write path does internally for
        the blob-write step.
        """
        repo = self._repos.get_server_repo(project_id)
        store = repo.store
        async_put = getattr(store, "async_put", None)
        if async_put is not None:
            blob_hash = await async_put(content)
        else:
            import asyncio
            blob_hash = await asyncio.to_thread(store.put, content)
        return BlobRef(hash=blob_hash, size=len(content))

    async def bulk_write_refs(
        self,
        project_id: str,
        file_refs: dict[str, BlobRef],
        who: str,
        scope: str = "",
        deleted: list[str] | None = None,
        message: str = "",
        verify_blobs: bool = True,
        source_channel: str = "papi",
        project_write_state: ProjectWriteState | None = None,
    ) -> WriteResult:
        """Commit a tree update referencing already-staged blobs by hash.

        Same scope-routing semantics as ``bulk_write``: product/API
        callers commit once at project root; scoped access-point callers
        pass an explicit scope.

        **Caller contract**: every ``BlobRef.hash`` MUST already be
        present in the project's ObjectStore. The default
        ``verify_blobs=True`` does a HEAD round-trip per ref before
        constructing the commit — failing loud if any blob is
        missing. This is the safety net that makes "dangling commit"
        impossible in practice. Set ``verify_blobs=False`` only when
        the caller has independently confirmed all blobs are present
        in the same transaction (e.g. CLI ``/push`` after
        ``async_put_many``) and wants to skip the HEAD latency on a
        large batch.

        Compared to ``bulk_write(files: dict[str, bytes])``: this
        skips the per-blob ``store.put`` round-trip — the bytes
        never enter the Python process. For a 100-file folder
        upload at typical web-PDF sizes, that's the difference
        between holding ~500 MB in RAM (and ~5 seconds of S3
        re-download) and holding zero.
        """
        clean = {validate_path(k): v for k, v in file_refs.items()}
        clean_del = [validate_path(p) for p in (deleted or [])]

        if verify_blobs and clean:
            await self._verify_blobs_present(project_id, clean.values())

        if scope:
            return await self._bulk_write_refs_in_scope(
                project_id, scope, clean, clean_del, who, message,
                source_channel=source_channel,
                project_write_state=project_write_state,
            )

        write_groups = self._group_paths_by_scope(
            project_id, list(clean.keys()),
        )
        del_groups = self._group_paths_by_scope(project_id, clean_del)
        all_scopes = set(write_groups.keys()) | set(del_groups.keys())

        first_result: WriteResult | None = None
        for target_scope in all_scopes:
            rel_refs = {
                rel: clean[self._join_scope_path(target_scope, rel)]
                for rel in write_groups.get(target_scope, [])
            }
            rel_dels = del_groups.get(target_scope, [])
            r = await self._bulk_write_refs_in_scope(
                project_id, target_scope,
                rel_refs, rel_dels, who, message,
                source_channel=source_channel,
                project_write_state=project_write_state,
            )
            first_result = first_result or r
        return first_result or WriteResult(
            paths=list(clean.keys()) + clean_del,
        )

    async def _bulk_write_refs_in_scope(
        self,
        project_id: str,
        scope: str,
        rel_refs: dict[str, BlobRef],
        rel_dels: list[str],
        who: str,
        message: str,
        *,
        source_channel: str = "papi",
        project_write_state: ProjectWriteState | None = None,
    ) -> WriteResult:
        ops: list[tuple] = []
        ops.extend(
            ("put_ref", path, ref.hash) for path, ref in rel_refs.items()
        )
        ops.extend(("rm", path) for path in rel_dels)
        if not ops:
            return WriteResult()

        def splice_fn(store, root_hash):
            return splice_batch(store, root_hash, ops)

        total_size = sum(r.size for r in rel_refs.values())
        result = await self._apply_operation(
            project_id, scope, splice_fn,
            who=who,
            message=message or f"bulk write {len(rel_refs)} files",
            op_type="bulk_write",
            audit_detail={
                "writes": len(rel_refs),
                "deletes": len(rel_dels),
                "total_size": total_size,
            },
            source_channel=source_channel,
            project_write_state=project_write_state,
        )
        full_paths = [
            self._join_scope_path(scope, p)
            for p in list(rel_refs.keys()) + rel_dels
        ]
        return _to_result(result, full_paths)

    async def _verify_blobs_present(
        self,
        project_id: str,
        refs,  # Iterable[BlobRef]
    ) -> None:
        """Confirm every ``BlobRef.hash`` is present in the project's
        ObjectStore. Raises ``MissingBlobError`` on the first gap.

        Each check is a HEAD round-trip. We deduplicate hashes
        first (the same blob can land at multiple paths in one
        commit, e.g. dropping a folder with duplicate PDFs) so the
        worst case is ``O(unique_blobs)`` HEADs, not ``O(paths)``.

        The verification is a safety net, not a correctness
        requirement: the read path verifies ``hash_bytes(get(h))
        == h`` on every fetch (see ``ObjectStore.get``), so a
        missing blob produces a fail-loud ``ObjectNotFoundError``
        at read time, never silent corruption. We HEAD here only
        to reject the commit BEFORE history / audit fire — much
        easier to recover from than discovering at read time.
        """
        unique_hashes = {ref.hash for ref in refs if ref.hash}
        if not unique_hashes:
            return

        repo = self._repos.get_server_repo(project_id)
        store = repo.store
        async_exists_many = getattr(store, "async_exists_many", None)
        if async_exists_many is not None:
            ordered_hashes = sorted(unique_hashes)
            existing_hashes = await async_exists_many(ordered_hashes)
            missing = [h for h in ordered_hashes if h not in existing_hashes]
            if missing:
                h = missing[0]
                raise MissingBlobError(
                    f"blob {h[:12]}… not present in project {project_id}'s "
                    f"object store; refusing to commit a dangling tree"
                )
            return

        # ``async_exists`` is the abstract interface; concrete S3
        # backends expose ``async_exists_many`` above for bounded
        # parallel HEADs. Fall back to one-by-one checks for in-memory
        # or third-party stores.
        async_exists = getattr(store, "async_exists", None)
        for h in sorted(unique_hashes):
            if async_exists is not None:
                exists = await async_exists(h)
            else:
                import asyncio
                exists = await asyncio.to_thread(store.exists, h)
            if not exists:
                raise MissingBlobError(
                    f"blob {h[:12]}… not present in project {project_id}'s "
                    f"object store; refusing to commit a dangling tree"
                )

    # ══════════════════════════════════════════════
    # Read operations (sync — direct Merkle tree reads)
    # ══════════════════════════════════════════════

    def read_file(self, project_id: str, path: str) -> bytes:
        return self._reader.read_file(project_id, path.strip("/"))

    def read_file_in_scope(self, project_id: str, scope: str, path: str) -> bytes:
        return self._reader.read_file_in_scope(
            project_id, scope.strip("/"), path.strip("/"),
        )

    def read_file_range(
        self,
        project_id: str,
        path: str,
        *,
        start: int = 0,
        limit: int | None = None,
    ):
        return self._reader.read_file_range(
            project_id,
            path.strip("/"),
            start=start,
            limit=limit,
        )

    def read_file_range_in_scope(
        self,
        project_id: str,
        scope: str,
        path: str,
        *,
        start: int = 0,
        limit: int | None = None,
    ):
        return self._reader.read_file_range_in_scope(
            project_id,
            scope.strip("/"),
            path.strip("/"),
            start=start,
            limit=limit,
        )

    def list_dir(
        self, project_id: str, path: str = "", *, include_size: bool = False
    ) -> list[VersionEntry]:
        return self._reader.list_dir(
            project_id, path.strip("/"), include_size=include_size,
        )

    def list_dir_in_scope(
        self,
        project_id: str,
        scope: str,
        path: str = "",
        *,
        include_size: bool = False,
    ) -> list[VersionEntry]:
        return self._reader.list_dir_in_scope(
            project_id, scope.strip("/"), path.strip("/"),
            include_size=include_size,
        )

    def list_tree(
        self,
        project_id: str,
        path: str = "",
        max_depth: int = -1,
        *,
        include_size: bool = False,
        max_entries: int | None = None,
    ) -> list[VersionEntry]:
        return self._reader.list_tree(
            project_id, path.strip("/"), max_depth=max_depth,
            include_size=include_size,
            max_entries=max_entries,
        )

    def list_tree_in_scope(
        self,
        project_id: str,
        scope: str,
        path: str = "",
        max_depth: int = -1,
        *,
        include_size: bool = False,
        max_entries: int | None = None,
    ) -> list[VersionEntry]:
        return self._reader.list_tree_in_scope(
            project_id, scope.strip("/"), path.strip("/"),
            max_depth=max_depth,
            include_size=include_size,
            max_entries=max_entries,
        )

    def stat(
        self, project_id: str, path: str, *, include_size: bool = False
    ) -> VersionEntry | None:
        return self._reader.stat(
            project_id, path.strip("/"), include_size=include_size,
        )

    def stat_in_scope(
        self,
        project_id: str,
        scope: str,
        path: str,
        *,
        include_size: bool = False,
    ) -> VersionEntry | None:
        return self._reader.stat_in_scope(
            project_id, scope.strip("/"), path.strip("/"),
            include_size=include_size,
        )

    def get_head_commit_id(self, project_id: str) -> str:
        return self._reader.get_head_commit_id(project_id)

    def get_scope_head_commit_id(self, project_id: str, scope_path: str) -> str:
        project_repo = self._repos.get_repo(project_id)
        return project_repo.history.get_scope_head_commit_id(
            (scope_path or "").strip("/"),
        ) or ""

    def get_scope_head_commit_id_for_path(
        self, project_id: str, path: str,
    ) -> str:
        scope_path, _rel_path = self._select_write_scope(
            project_id, validate_path(path),
        )
        return self.get_scope_head_commit_id(project_id, scope_path)

    def get_path_timestamps(
        self,
        project_id: str,
        paths: list[str],
        *,
        limit: int = 5000,
    ) -> dict[str, dict[str, str]]:
        clean_paths = {(p or "").strip("/") for p in paths}
        timestamps: dict[str, dict[str, str]] = {
            p: {"created_at": "", "modified_at": ""} for p in clean_paths
        }
        if not clean_paths:
            return timestamps

        try:
            repo = self._repos.get_server_repo(project_id)
            commits = repo.get_history_since("", limit=limit)
        except Exception:
            return timestamps

        for commit in commits:
            ts = str(commit.get("created_at") or commit.get("time") or "")
            if not ts:
                continue
            changes = commit.get("changes") or []
            if not isinstance(changes, list):
                continue
            for change in changes:
                path = str(change.get("path") or "").strip("/")
                if not path and "" not in clean_paths:
                    continue
                affected = {path, ""}
                parts = [part for part in path.split("/") if part]
                for index in range(1, len(parts)):
                    affected.add("/".join(parts[:index]))
                for affected_path in affected & clean_paths:
                    row = timestamps[affected_path]
                    if not row["created_at"] and change.get("action") == "add":
                        row["created_at"] = ts
                    if not row["created_at"] and affected_path != path:
                        row["created_at"] = ts
                    row["modified_at"] = ts
        return timestamps

    def get_root_hash(self, project_id: str) -> str:
        return self._reader.get_root_hash(project_id)

    # ══════════════════════════════════════════════
    # External-callers hook
    # ══════════════════════════════════════════════

    def push_and_finalize(self, project_id: str, push_result: dict) -> dict:
        """Run post-push hooks after any push.

        Used by in-process callers that already have a push result and need
        the same post-commit projection/finalization behavior as Git and
        typed product operations.
        """
        from src.version_engine.derived.hooks import run_post_push_hook

        run_post_push_hook(project_id, self._repos, push_result)
        return push_result

    # ══════════════════════════════════════════════
    # Scope routing helpers
    # ══════════════════════════════════════════════

    def _select_write_scope(
        self, project_id: str, path: str,
    ) -> tuple[str, str]:
        """Return the product-root write target for a project path.

        Frontend/Data-page operations are repository-level user actions:
        they must produce one project-root transaction and one visible
        history item. Access-point/Git flows already know their scope and
        pass it explicitly through ``_resolve_write_target``; this helper
        intentionally does not infer child scopes from project paths.
        """
        clean = validate_path(path)
        return "", clean

    def _resolve_write_target(
        self, project_id: str, path: str, explicit_scope: str,
    ) -> tuple[str, str]:
        """Decide ``(scope, rel_path)`` for a single write/mkdir.

        If the caller supplied an explicit scope, trust it: assume
        ``path`` is already relative to that scope. Otherwise route to
        the project root; frontend/product operations must not infer a
        child scope from the path.
        """
        if explicit_scope:
            return explicit_scope, path
        return self._select_write_scope(project_id, path)

    def _group_paths_by_scope(
        self, project_id: str, paths: list[str],
    ) -> dict[str, list[str]]:
        """Bucket paths into publish groups.

        For product/API callers this intentionally returns one root
        group. Explicit scoped callers do not use this helper; they pass
        their scope to the in-scope write path directly.
        """
        if not paths:
            return {}
        groups: dict[str, list[str]] = {}
        for p in paths:
            scope_path, rel_path = self._select_write_scope(project_id, p)
            groups.setdefault(scope_path, []).append(rel_path)
        return groups

    @staticmethod
    def _join_scope_path(scope_path: str, rel_path: str) -> str:
        scope = (scope_path or "").strip("/")
        rel = (rel_path or "").strip("/")
        if not scope:
            return rel
        if not rel:
            return scope
        return f"{scope}/{rel}"

    def _lookup_blob_hash(
        self, project_id: str, scope: str, rel_path: str,
    ) -> str:
        """Resolve the blob hash for ``scope/rel_path`` in the CURRENT
        scope state. Returns an empty string when the path doesn't exist,
        refers to a directory, or cannot be inspected.
        """

        try:
            from src.version_engine.write_engine import tree as tree_mod

            repo = self._repos.get_server_repo(project_id)
            scope_hash = repo.get_scope_hash(scope) or ""
            if not scope_hash:
                return ""
            parts = [p for p in rel_path.strip("/").split("/") if p]
            if not parts:
                return ""
            current = scope_hash
            for part in parts[:-1]:
                entries = tree_mod.read_tree(repo.store, current)
                typ, child = entries.get(part, (None, None))
                if typ != "T":
                    return ""
                current = child
            entries = tree_mod.read_tree(repo.store, current)
            typ, hash_val = entries.get(parts[-1], (None, None))
            if typ != "B":
                return ""
            return hash_val or ""
        except Exception:
            return ""


# ══════════════════════════════════════════════════
# Internal helpers
# ══════════════════════════════════════════════════


def _to_result(
    raw,
    paths: list[str] | None = None,
) -> WriteResult:
    """Translate transaction-engine results into ``WriteResult``.

    The two dataclasses already match field-for-field — this function
    exists so a future schema change in the transaction result can be
    absorbed without touching every call site, and so we can override
    the ``paths`` list with the caller's preferred presentation.
    """
    return WriteResult(
        commit_id=raw.commit_id,
        status=raw.status,
        merged=raw.merged,
        conflicts=raw.conflicts,
        paths=paths if paths is not None else list(raw.paths),
    )


def _is_descendant_path(parent: str, child: str) -> bool:
    clean_parent = validate_path(parent)
    clean_child = validate_path(child)
    return bool(clean_parent and clean_child.startswith(f"{clean_parent}/"))
