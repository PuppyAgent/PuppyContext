"""
InProcessVersionClient — L4 **batch** adapter (in-process).

This module is the ``adapters/batch/`` entry from the architecture doc's
L4 lineup ("Product / AP / batch adapter"). "Batch" here means
"connector / sync job / MCP tool / sandbox" callers that live inside
the API process and need the same scope enforcement, conflict policy,
and audit logging as external ``git push`` traffic — but without the
HTTP round trip. The clone/modify/publish loop is modeled after Git
push so callers can reason about a single contract regardless of
whether they're a real Git client or an in-process consumer.

Usage:
    client = InProcessVersionClient(
        repo_manager, project_id, projection, actor="agent:example"
    )
    files = client.clone()           # full-content read path: {rel_path: bytes}
    client.push({"foo.md": b"new"}, deleted=["old.md"])
"""

from __future__ import annotations

import base64
import asyncio
from datetime import UTC

from src.platform.repository_target.models import RepositoryPathProjection
from src.version_engine.write_engine import tree as tree_mod
from src.version_engine.write_engine.git_object_format import (
    MODE_DIR, MODE_FILE, TreeEntry, encode_object, encode_tree, hash_object,
)
from src.version_engine.write_engine.engine import VersionWriteEngine
from src.version_engine.domain.intents import VersionSubmissionIntent
from src.version_engine.infrastructure.supabase.repo_manager import VersionRepoManager
from src.version_engine.infrastructure.supabase.server_repo import PuppyOneServerRepo


class InProcessVersionClient:
    """Stateless version client that calls handlers in-process.

    Each instance represents one read/index → modify → push cycle.
    Commit identity is a 40-hex SHA-1 over the framed git commit body
    (``head_commit_id``); the old integer ``version`` counter is gone.
    """

    def __init__(
        self,
        repo_manager: VersionRepoManager,
        project_id: str,
        projection: RepositoryPathProjection,
        *,
        actor: str,
        source_channel: str = "access_sandbox",
    ):
        """In-process client used by access-runtime / sandbox / connector flows.

        ``source_channel`` tags every push this client makes so audit
        rows and conflict-policy rules can distinguish Access runtime writes
        from ``sync`` connector imports or hosted ``web`` UI calls. The
        default is ``access_sandbox`` because current in-process callers are
        agent/sandbox runtime flows; product API entry points pass
        ``source_channel="papi"`` explicitly.
        """

        self._repo_manager = repo_manager
        self._project_id = project_id
        self._projection = projection
        self._actor = actor or "ephemeral"
        self._source_channel = source_channel or "access_sandbox"

        self._head_commit_id: str = ""
        self._scope: dict = {}
        self._files: dict[str, bytes] = {}
        self._object_hashes: set[str] = set()
        # Populated by load_scope_index() instead of clone(): {rel_path: blob_hash}.
        # When non-None it signals the hash-index write path — _build_snapshot
        # reuses these hashes for unchanged files instead of re-hashing
        # downloaded content. push() falls back to the full-content
        # path when this is None.
        self._file_hashes: dict[str, str] | None = None

    @property
    def scope(self) -> dict:
        return self._scope

    @property
    def head_commit_id(self) -> str:
        return self._head_commit_id

    @property
    def files(self) -> dict[str, bytes]:
        return dict(self._files)

    def restore_base(self, *, head_commit_id: str, files: dict[str, bytes]) -> None:
        """Restore a caller-owned durable base without silently recloning HEAD.

        The adapter still applies its fixed projection and canonical conflict
        policy. Callers must authenticate/checksum the recovery object first.
        """
        from src.version_engine.write_engine.path_utils import normalize_path

        for path, content in files.items():
            if not path or normalize_path(path) != path or not isinstance(content, bytes):
                raise ValueError("Invalid restored version base")
        self._head_commit_id = head_commit_id
        self._scope = self._projection.as_engine_projection()
        self._files = dict(files)
        self._file_hashes = None
        self._object_hashes = set()

    def _get_server_repo(self) -> PuppyOneServerRepo:
        return self._repo_manager.get_server_repo(self._project_id)

    def _set_audit_agent(self, who: str) -> None:
        """Override the agent identity used for audit log entries on the
        next push/pull/clone.

        The cached host-client model needs this because the client is
        reused across requests but each request comes from a different
        authenticated user, so the per-view lock updates it before each push.
        """
        self._actor = who or "puppyone-host"

    # ── Clone ────────────────────────────────────

    def clone(self) -> dict[str, bytes]:
        """Clone the scope subtree. Returns {rel_path: content}.

        Heavy: walks every reachable blob and downloads its bytes from
        S3, then re-uploads them as base64 across the in-process clone
        boundary. Use only when callers actually need the file content
        (move / hard-delete need to relocate or read existing
        files). Pure-write ops (mkdir / write_file / bulk_write / delete
        by path) should use ``load_scope_index`` instead — that builds a
        ``{path: blob_hash}`` map by walking the tree without ever
        touching blob bytes, which lets ``push`` reuse existing hashes
        for unchanged files and only ship the new blob bytes.
        """
        repo = self._get_server_repo()
        scope = self._projection.as_engine_projection()

        files_raw = repo.list_scope_files(scope)
        scope_tree_hash = repo.build_scope_tree(scope)
        try:
            scope_hashes = tree_mod.collect_reachable_hashes(
                repo.store, scope_tree_hash,
            ) if scope_tree_hash else set()
        except Exception:
            scope_hashes = set()

        head_commit_id = repo.get_scope_head_commit_id(scope.get("path", ""))

        self._head_commit_id = head_commit_id or ""
        self._scope = {
            "path": scope.get("path", ""),
            "exclude": scope.get("exclude", []),
            "mode": scope.get("mode", "rw"),
        }
        self._files = dict(files_raw)
        self._object_hashes = set(scope_hashes)
        self._file_hashes = None  # full-content path; hash index is unused

        try:
            repo.record_audit("clone", self._actor, {
                "scope": scope.get("path", ""),
                "files": len(files_raw),
                "commit_id": head_commit_id,
            })
        except Exception:
            pass

        return dict(self._files)

    def load_scope_index(self) -> dict[str, str]:
        """Load the scope's ``{rel_path: blob_hash}`` index only.

        Walks the scope's tree concurrently — each level of tree nodes
        is fetched in parallel via a thread pool, so cost is bounded by
        ``tree_depth × per-S3-GET`` instead of ``tree_node_count ×
        per-S3-GET``. The default sequential ``tree_to_flat`` was the
        bottleneck on first-write (the user reported 42s on a moderate
        tree); parallel walk brings that down by an order of magnitude
        while still never loading blob payloads. This is not a repository
        clone; it is a server-side tree-index refresh for the cached host
        writer.

        Use this when you only need to push modifications on top of
        the existing tree (mkdir, write_file, bulk_write, path delete)
        — ``push`` will reuse the indexed hashes for unchanged paths
        and only include blob bytes for the entries actually being
        added or replaced.

        Sets ``self._file_hashes``; leaves ``self._files`` empty so any
        caller that mistakenly assumes full content fails loudly rather
        than silently operating on empty bytes.
        """
        from src.version_engine.write_engine.path_utils import normalize_path

        repo = self._get_server_repo()

        # Mirror the full-content auth/scope plumbing without the heavy fetch.
        scope = self._projection.as_engine_projection()
        scope_path = normalize_path(self._projection.path_prefix)
        excludes = [normalize_path(e) for e in self._projection.excludes]

        scope_tree_hash = repo.build_scope_tree(scope)
        flat = self._parallel_tree_walk(repo.store, scope_tree_hash)

        file_hashes: dict[str, str] = {}
        for rel_path, blob_hash in flat.items():
            full_rel = f"{scope_path}/{rel_path}" if scope_path else rel_path
            if any(
                full_rel == ex or full_rel.startswith(ex + "/")
                for ex in excludes
            ):
                continue
            file_hashes[rel_path] = blob_hash

        self._head_commit_id = repo.get_scope_head_commit_id(scope_path)
        self._scope = {
            "path": scope.get("path", ""),
            "exclude": scope.get("exclude", []),
            "mode": scope.get("mode", "rw"),
        }
        self._files = {}
        self._file_hashes = file_hashes
        # No `objects` set — push's negotiate step queries the server for
        # which new objects are actually missing, so we don't need to
        # know what's already on the server up-front.
        self._object_hashes = set()

        return dict(file_hashes)

    @staticmethod
    def _parallel_tree_walk(
        store, root_hash: str, max_workers: int = 16,
    ) -> dict[str, str]:
        """Parallel BFS variant of ``tree.tree_to_flat``.

        For each level of the tree, all ``read_tree`` (which is one S3
        GET per tree node, ignoring the LRU cache hits) calls run
        concurrently via a thread pool. Returns the same shape
        ``{rel_path: blob_hash}`` as ``tree_to_flat``.

        S3 latency to Supabase from non-US regions is ~150-300ms per
        call. With this many serial GETs, even a tree of ~100 nodes can
        push first-call clone past 30s. Parallelising at the level
        boundary collapses the wall-clock cost to ``levels × latency``,
        which is typically << 10 levels deep.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        from src.version_engine.write_engine.tree import read_tree

        result: dict[str, str] = {}
        if not root_hash:
            return result

        pending: list[tuple[str, str]] = [(root_hash, "")]
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            while pending:
                futures = {
                    executor.submit(read_tree, store, tree_hash): (tree_hash, prefix)
                    for tree_hash, prefix in pending
                }
                pending = []
                for fut in as_completed(futures):
                    _, prefix = futures[fut]
                    InProcessVersionClient._absorb_tree_entries(
                        fut.result(), prefix, result, pending,
                    )
        return result

    @staticmethod
    def _absorb_tree_entries(
        entries: dict,
        prefix: str,
        leaf_result: dict[str, str],
        sub_trees: list[tuple[str, str]],
    ) -> None:
        """Process one tree node's entries: blobs into ``leaf_result``,
        sub-trees into ``sub_trees`` for the next BFS level.

        Extracted from ``_parallel_tree_walk`` only to keep its cognitive
        complexity below the 15-branch lint threshold; the logic is
        otherwise unchanged.
        """
        for name, (typ, h) in entries.items():
            path = f"{prefix}{name}" if not prefix else f"{prefix}/{name}"
            if typ == "T":
                sub_trees.append((h, path))
            else:
                leaf_result[path] = h

    # ── Pull ─────────────────────────────────────

    def pull(self) -> dict[str, bytes]:
        """Pull latest changes since last known commit_id.

        Returns updated files or empty dict if up-to-date.
        """
        repo = self._get_server_repo()
        scope = self._projection.as_engine_projection()
        scope_path = self._projection.path_prefix

        current_head = repo.get_scope_head_commit_id(scope_path) or ""
        if current_head and current_head == self._head_commit_id:
            return dict(self._files)

        files_raw = repo.list_scope_files(scope)
        scope_tree_hash = repo.build_scope_tree(scope)
        try:
            scope_hashes = tree_mod.collect_reachable_hashes(
                repo.store, scope_tree_hash,
            ) if scope_tree_hash else set()
        except Exception:
            scope_hashes = set()

        self._head_commit_id = current_head or self._head_commit_id
        self._files = dict(files_raw)
        for h in scope_hashes:
            self._object_hashes.add(h)

        return dict(self._files)

    # ── Push ─────────────────────────────────────

    def push(
        self,
        modified: dict[str, bytes] | None = None,
        deleted: list[str] | None = None,
        message: str = "",
        who: str | None = None,
        policy_override: str | None = None,
    ) -> dict:
        """Push changes back to the server.

        Args:
            modified: {rel_path: new_content} for created/updated files
            deleted: [rel_path, ...] for removed files
            message: commit message
            who: override agent identity

        Returns:
            Push result dict with ``status``, ``commit_id`` (40-hex
            SHA-1 of the git commit object), ``merged``, and
            ``conflicts``.
        """
        import time as _time

        from src.utils.logger import log_info

        modified = modified or {}
        deleted = deleted or []

        scope_path = self._projection.path_prefix
        op_tag = f"[VersionClient][push scope={scope_path!r}]"

        t0 = _time.monotonic()
        snapshot_root, new_objects, merged_files = self._prepare_snapshot(
            modified, deleted,
        )
        log_info(
            f"{op_tag} prepared snapshot "
            f"root={snapshot_root[:12] or '<empty>'} "
            f"new_objects={len(new_objects)} "
            f"base_commit={self._head_commit_id[:12] or '<empty>'} "
            f"in {int((_time.monotonic() - t0) * 1000)}ms",
        )

        repo = self._get_server_repo()
        t1 = _time.monotonic()
        body = self._build_push_body(
            repo, snapshot_root, new_objects, message, who,
        )
        server_changed_since_clone = self._server_head_changed(repo, scope_path)
        log_info(
            f"{op_tag} negotiated, shipping "
            f"{len(body.get('objects', {}))} objects in "
            f"{int((_time.monotonic() - t1) * 1000)}ms",
        )

        t2 = _time.monotonic()
        # Store incoming loose objects under the project's object store before
        # the engine evaluates the proposed tree.
        objects_b64 = body.get("objects") or {}
        for object_id, b64data in objects_b64.items():
            repo.store.put_loose(object_id, base64.b64decode(b64data))

        scope = self._projection.as_engine_projection()
        snapshot = body["snapshots"][-1]
        engine = VersionWriteEngine(self._repo_manager)
        intent = VersionSubmissionIntent(
            project_id=self._project_id,
            scope_path=scope.get("path", "") or "",
            actor=self._actor,
            source_channel=self._source_channel,
            base_commit_id=body.get("base_commit_id", "") or "",
            proposed_tree_id=snapshot["root"],
            client_commit_id=snapshot.get("commit_id", ""),
            message=snapshot.get("message", message or ""),
            scope_excludes=scope.get("exclude") or [],
            audit_detail={"snapshots": len(body.get("snapshots", []))},
            defer_projection=True,
            policy_override=policy_override,
        )
        engine_result = _run_async_from_sync(engine.submit_version(intent))
        result = {
            "status": engine_result.status,
            "pending_conflict_id": engine_result.pending_conflict_id,
            "reason": engine_result.reason,
            "commit_id": engine_result.commit_id,
            "pushed": len(body.get("snapshots", [])),
            "root": engine_result.new_scope_hash,
            "merged": engine_result.merged,
            "conflicts": engine_result.conflicts,
            "merged_changes": engine_result.merged_changes,
            "commit_object": engine_result.commit_object,
        }
        log_info(
            f"{op_tag} engine push returned "
            f"status={result.get('status', '?')} "
            f"commit={(result.get('commit_id') or '')[:12] or '<empty>'} "
            f"merged={result.get('merged', False)} "
            f"in {int((_time.monotonic() - t2) * 1000)}ms",
        )

        if result.get("status") == "ok":
            self._apply_push_result(
                result, modified, deleted, merged_files,
                server_changed_since_clone=server_changed_since_clone,
            )

        return result

    def _server_head_changed(self, repo: PuppyOneServerRepo, scope_path: str) -> bool:
        """Detect a stale cached host client before applying push results.

        The Write Engine reports ``merged=True`` only for conflict
        merges. A push can still be rebased over newer server state without
        conflicts; in that case the server returns an OK snapshot that includes
        merged files, and a cached client must refresh rather than applying
        only its local delta to an old ``_file_hashes`` map.
        """
        try:
            server_head = repo.get_scope_head_commit_id(scope_path or "")
        except Exception:
            return False
        return bool(server_head and server_head != self._head_commit_id)

    def _prepare_snapshot(
        self,
        modified: dict[str, bytes],
        deleted: list[str],
    ) -> tuple[str, dict[str, bytes], dict[str, bytes]]:
        """Build the new snapshot root + new objects to ship.

        Returns ``(snapshot_root, new_objects, merged_files)``.
        ``merged_files`` is only populated on the full-content
        path; the hash-index path leaves it empty (its sole legitimate caller
        is `_do_push`, which discards the client right after push
        returns).
        """
        if self._file_hashes is not None:
            # Hash-index path: load_scope_index() gave us {path: blob_hash} for the
            # existing tree. Reuse those hashes for unchanged files and
            # only ship blob bytes for paths actually being added /
            # replaced. Saves an N×blob-download every push.
            snapshot_root, new_objects = self._build_snapshot_from_index(
                self._file_hashes, modified, deleted,
            )
            return snapshot_root, new_objects, {}

        merged_files = dict(self._files)
        for path, content in modified.items():
            merged_files[path] = content
        for path in deleted:
            merged_files.pop(path, None)
        snapshot_root, new_objects = self._build_snapshot(merged_files)
        return snapshot_root, new_objects, merged_files

    def _build_push_body(
        self,
        repo: PuppyOneServerRepo,
        snapshot_root: str,
        new_objects: dict[str, bytes],
        message: str,
        who: str | None,
    ) -> dict:
        """Negotiate which new objects the server is missing, then build
        the push request body."""
        if new_objects:
            # Inline "which hashes are missing" check (formerly handle_negotiate):
            # ship only objects the server does not already have.
            store = repo.store
            missing: set[str] = set()
            try:
                async_existing = getattr(store, "exists_many", None)
                if callable(async_existing):
                    existing = set(async_existing(list(new_objects.keys())))
                else:
                    existing = {h for h in new_objects if store.exists(h)}
                missing = {h for h in new_objects if h not in existing}
            except Exception:
                missing = set(new_objects.keys())
        else:
            missing = set()

        objects_b64 = {
            h: base64.b64encode(data).decode()
            for h, data in new_objects.items()
            if h in missing
        }
        return {
            "base_commit_id": self._head_commit_id,
            "snapshots": [{
                "id": 1,
                "root": snapshot_root,
                "message": message or "ephemeral push",
                "who": who or self._actor,
                "time": _now_iso(),
            }],
            "objects": objects_b64,
        }

    def _apply_push_result(
        self,
        result: dict,
        modified: dict[str, bytes],
        deleted: list[str],
        merged_files: dict[str, bytes],
        *,
        server_changed_since_clone: bool = False,
    ) -> None:
        """Update cached client state after a successful push.

        Three cases:
          1. server merged with a concurrent commit → refresh from new
              head (index walk if we're on the hash-only path; pull() for
              the full-content path so existing-content readers
              see merged data).
          2. hash-index path, no merge → apply the same edits to ``_file_hashes``
             so the cached host client stays consistent for the next push.
          3. full-content path, no merge → swap in the locally-merged file dict.
        """
        new_cid = result.get("commit_id", "")
        if new_cid:
            self._head_commit_id = new_cid

        if (
            result.get("merged")
            or result.get("merged_changes")
            or server_changed_since_clone
        ):
            if self._file_hashes is not None:
                self.load_scope_index()
            else:
                self.pull()
            return

        if self._file_hashes is not None:
            for path, content in modified.items():
                self._file_hashes[path] = _content_hash(content)
            for path in deleted:
                self._file_hashes.pop(path, None)
            return

        self._files = merged_files

    # ── Read helpers ──────────────────────────────

    def read_file(self, path: str) -> bytes | None:
        """Read a single file from the cloned state."""
        return self._files.get(path)

    def list_files(self) -> list[str]:
        """List all file paths in the scope."""
        return sorted(self._files.keys())

    def stat(self, path: str) -> dict | None:
        """Get basic info about a file."""
        if path not in self._files:
            return None
        content = self._files[path]
        return {
            "path": path,
            "size": len(content),
            "hash": _content_hash(content),
        }

    # ── Snapshot building ────────────────────────

    def _build_snapshot(
        self, files: dict[str, bytes]
    ) -> tuple[str, dict[str, bytes]]:
        """Build a git Merkle tree snapshot from a file dict.

        Returns ``(root_sha1, {sha1: loose_bytes})`` where ``loose_bytes``
        is the zlib-compressed framed git object — exactly what the
        server stores via ``ObjectStore.put_loose`` and what the wire
        protocol ships base64-encoded.
        """
        new_objects: dict[str, bytes] = {}

        nested: dict = {}
        for path, content in files.items():
            blob_hash = _put_blob(content, new_objects)

            parts = path.split("/")
            d = nested
            for p in parts[:-1]:
                d = d.setdefault(p, {})
            d[parts[-1]] = ("B", blob_hash)

        root_hash = self._build_tree_node(nested, new_objects)
        return root_hash, new_objects

    def _build_snapshot_from_index(
        self,
        existing_hashes: dict[str, str],
        modified: dict[str, bytes],
        deleted: list[str],
    ) -> tuple[str, dict[str, bytes]]:
        """Snapshot variant for the hash-index write path.

        Reuses ``existing_hashes`` for unchanged files (no re-hashing,
        no blob bytes carried) and only adds loose-encoded blobs to
        ``new_objects`` for entries actually being added or replaced.
        Tree nodes are encoded as binary git ``tree`` objects.

        Returns ``(root_sha1, {sha1: loose_bytes})``.
        """
        merged_hashes: dict[str, str] = dict(existing_hashes)
        new_objects: dict[str, bytes] = {}
        for path, content in modified.items():
            blob_hash = _put_blob(content, new_objects)
            merged_hashes[path] = blob_hash
        for path in deleted:
            merged_hashes.pop(path, None)

        nested: dict = {}
        for path, blob_hash in merged_hashes.items():
            parts = path.split("/")
            d = nested
            for p in parts[:-1]:
                d = d.setdefault(p, {})
            d[parts[-1]] = ("B", blob_hash)

        root_hash = self._build_tree_node(nested, new_objects)
        return root_hash, new_objects

    def _build_tree_node(
        self, node: dict, new_objects: dict[str, bytes]
    ) -> str:
        """Encode one tree level as a git ``tree`` object.

        Children are either ``("B", blob_sha)`` leaves or nested dicts
        (sub-trees). Stores the loose-encoded tree bytes in
        ``new_objects[sha]`` and returns the SHA-1.
        """
        entries: list[TreeEntry] = []
        for name, val in node.items():
            if isinstance(val, tuple):
                _, blob_hash = val
                entries.append(
                    TreeEntry(name=name, mode=MODE_FILE, sha1_hex=blob_hash)
                )
            else:
                sub_hash = self._build_tree_node(val, new_objects)
                entries.append(
                    TreeEntry(name=name, mode=MODE_DIR, sha1_hex=sub_hash)
                )

        tree_body = encode_tree(entries)
        tree_hash, loose = encode_object("tree", tree_body)
        new_objects[tree_hash] = loose
        return tree_hash


def _put_blob(content: bytes, new_objects: dict[str, bytes]) -> str:
    """Loose-encode *content* as a git ``blob`` object, store its
    zlib bytes in *new_objects*, and return the SHA-1 hex.

    Centralised so blob-loose framing and hash domain match what
    ``ObjectStore.put_blob`` produces server-side — clients and server
    agree byte-for-byte on the on-disk representation.
    """
    sha1, loose = encode_object("blob", content)
    new_objects[sha1] = loose
    return sha1


def _content_hash(data: bytes) -> str:
    """SHA-1 over a git ``blob`` frame of *data* — the canonical git
    blob hash, equal to what ``ObjectStore.put_blob`` returns.

    Use this only when the caller wants a hash without also keeping
    the loose-encoded bytes; the push pipeline builds both at once via
    :func:`_put_blob`.
    """
    return hash_object("blob", data)


def _run_async_from_sync(coro):
    """Run an adapter coroutine from this intentionally synchronous client."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    # Defensive fallback for accidental direct calls from an event-loop thread.
    # The usual callers already wrap ``InProcessVersionClient.push`` in
    # ``asyncio.to_thread``.
    import threading

    result: dict = {}
    error: list[BaseException] = []

    def _runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 - shuttle to caller
            error.append(exc)

    thread = threading.Thread(target=_runner, name="version-in-process-push", daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return result.get("value")


def _now_iso() -> str:
    from datetime import datetime
    return datetime.now(UTC).isoformat(timespec="seconds")
