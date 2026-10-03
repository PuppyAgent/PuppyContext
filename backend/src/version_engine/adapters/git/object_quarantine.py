"""Git transport-only object materialization and quarantine helpers.

These helpers are intentionally scoped to smart-HTTP clone/fetch/push.
They may walk reachable Git objects to satisfy Git protocol expectations,
but product Save/Product API writes must never import them. Product writes
operate on content hashes, Merkle tree splices, shallow parent validation,
and DB CAS only.
"""

from __future__ import annotations

import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

from src.version_engine.adapters.git._filelock import file_exclusive_lock
from src.version_engine.adapters.git.protocol import (
    ACCESS_POINT_MAIN_REF,
    ZERO_ID,
    is_object_id,
    run_git,
)
from src.version_engine.adapters.git.view_cache import (
    GitViewCacheKey,
    invalidate_git_view_cache,
    write_git_view_cache_metadata,
)
from src.version_engine.adapters.git.view_projection import (
    GitViewHead,
    resolve_git_view_head,
)
from src.version_engine.write_engine.git_object_format import (
    MODE_DIR,
    MODE_FILE,
    decode_object,
    decode_tree,
    encode_object,
    hash_object,
)
from src.version_engine.write_engine.git_object_graph import ObjectKind, object_edges
from src.version_engine.storage.object_store import stage_object_writes
from src.version_engine.write_engine.trace import trace_mark, trace_phase


class GitViewCurrentCorruptError(RuntimeError):
    """Raised when the current view cannot be represented as native Git."""


@contextmanager
def transport_bare_repo(
    repo,
    scope_path: str,
    scope_excludes: list[str] | None = None,
    *,
    follow_history: bool = True,
    include_blobs: bool = True,
    extra_roots: list[str] | None = None,
):
    """Yield an incrementally maintained bare repo for one Git view.

    The cache is keyed by project + canonical object-store namespace + scope +
    excludes. It is a transport cache, not an authority: PuppyOne's object
    store and DB ref state remain canonical. Each request advances the cache
    only by copying objects that are reachable from the current scope view head
    and missing locally. Once an object is already in the cache we treat its
    reachable closure as cached too, so a one-file commit only copies the new
    commit plus the changed tree chain/blob instead of re-copying the whole
    scope graph.
    """

    view_head = _transport_cache_head(
        repo,
        scope_path,
        scope_excludes,
    )
    _raise_if_current_corrupt(view_head)
    cache_key = GitViewCacheKey.from_repo(
        repo,
        scope_path,
        scope_excludes,
        follow_history=follow_history,
        include_blobs=include_blobs,
    )
    cache_dir = cache_key.cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    from src.version_engine.adapters.git.view_cache import view_lock_path

    lock_path = view_lock_path(cache_dir)
    with file_exclusive_lock(lock_path):
        bare_dir = cache_dir / "repo.git"
        _ensure_bare_repo(bare_dir)
        head = view_head.head
        additional_roots = [
            root for root in (extra_roots or [])
            if is_object_id(root) and root != ZERO_ID and root != head
        ]
        copy_reachable_objects_to_bare(
            repo,
            bare_dir,
            [head, *additional_roots],
            follow_history=follow_history,
            include_blobs=include_blobs,
        )
        _write_main_ref(bare_dir, head)
        write_git_view_cache_metadata(
            cache_dir,
            cache_key,
            head=head,
            view_health=view_head.health,
            canonical_head=view_head.canonical_head,
            health_reason=view_head.reason,
            history_cut=view_head.history_cut,
        )
        # Hold the lease while Git consumes the bare repo/alternates. Pruning
        # uses a non-blocking attempt on the same lock and skips active views.
        yield bare_dir


@contextmanager
def upload_pack_bare_repo(
    repo,
    scope_path: str,
    scope_excludes: list[str] | None = None,
    *,
    wants: list[str] | None = None,
    extra_refs: dict[str, str] | None = None,
):
    """Yield a request-local upload-pack repo.

    Smart HTTP is stateless across ``info/refs`` and ``git-upload-pack``.
    If the canonical view advances between those two HTTP requests, the
    client can still legitimately ask for the head it just saw advertised.
    Keep those requested ``want`` objects reachable through temporary refs
    for this one upload-pack invocation without polluting the durable cache's
    advertised ref namespace.

    ``extra_refs`` (GAP-3) is the scope's stored branch/tag refs
    (``{full_ref_name: commit_id}``). They are written into THIS request's
    bare repo — never the shared cache — so ``git clone`` / ``git fetch``
    sees and can serve them, while the durable transport cache stays a pure
    projection of the scope head.
    """

    wanted_roots = [
        want for want in (wants or [])
        if is_object_id(want) and want != ZERO_ID
    ]
    named_refs = _sanitize_named_refs(extra_refs or {})
    ref_roots = [
        commit_id for commit_id in named_refs.values()
        if commit_id not in wanted_roots
    ]
    with transport_bare_repo(
        repo,
        scope_path,
        scope_excludes,
        follow_history=True,
        include_blobs=True,
        extra_roots=wanted_roots + ref_roots,
    ) as cache_bare:
        with tempfile.TemporaryDirectory(prefix="puppyone-git-upload-pack-") as tmp:
            bare_dir = Path(tmp) / "repo.git"
            _ensure_bare_repo(bare_dir)
            alternates = bare_dir / "objects" / "info" / "alternates"
            alternates.parent.mkdir(parents=True, exist_ok=True)
            alternates.write_text(
                f"{(cache_bare / 'objects').resolve()}\n",
                encoding="utf-8",
            )
            cache_ref = cache_bare / "refs" / "heads" / "main"
            if cache_ref.exists():
                _write_main_ref(bare_dir, cache_ref.read_text(encoding="ascii").strip())
            _write_upload_pack_want_refs(bare_dir, wanted_roots)
            _write_named_refs(bare_dir, named_refs)
            yield bare_dir


def _sanitize_named_refs(extra_refs: dict[str, str]) -> dict[str, str]:
    """Filter stored refs to the safe, advertisable set.

    Defence in depth on top of the version_refs CHECK constraint: only
    ``refs/heads/*`` and ``refs/tags/*`` (never the scope head main), valid
    object ids, and no path-traversal segments are allowed to become files
    in the per-request bare repo.
    """
    safe: dict[str, str] = {}
    for ref_name, commit_id in extra_refs.items():
        if not isinstance(ref_name, str) or not isinstance(commit_id, str):
            continue
        if ref_name == ACCESS_POINT_MAIN_REF:
            continue
        if not ref_name.startswith(("refs/heads/", "refs/tags/")):
            continue
        if ".." in ref_name or ref_name.endswith("/") or "//" in ref_name:
            continue
        if not is_object_id(commit_id) or commit_id == ZERO_ID:
            continue
        safe[ref_name] = commit_id
    return safe


def _write_named_refs(bare_dir: Path, named_refs: dict[str, str]) -> None:
    for ref_name, commit_id in named_refs.items():
        ref_path = bare_dir / Path(ref_name)
        ref_path.parent.mkdir(parents=True, exist_ok=True)
        ref_path.write_text(f"{commit_id}\n", encoding="ascii")


def warm_transport_bare_repo(
    repo,
    scope_path: str,
    scope_excludes: list[str] | None = None,
    *,
    follow_history: bool = True,
    include_blobs: bool = True,
) -> str:
    """Advance the protocol cache for one Git view and return its head."""

    with transport_bare_repo(
        repo,
        scope_path,
        scope_excludes,
        follow_history=follow_history,
        include_blobs=include_blobs,
    ) as bare_dir:
        ref_path = bare_dir / "refs" / "heads" / "main"
        if not ref_path.exists():
            return ""
        return ref_path.read_text(encoding="ascii").strip()


@contextmanager
def receive_pack_advertisement_bare_repo(
    repo,
    scope_path: str,
    scope_excludes: list[str] | None = None,
    *,
    extra_refs: dict[str, str] | None = None,
):
    """Yield a minimal bare repo for receive-pack ref advertisement.

    ``git receive-pack --advertise-refs`` only needs refs; missing objects are
    tolerated by Git and are hydrated later for the actual POST receive-pack
    request. Keeping this path refs-only prevents a push preflight from pulling
    large blobs just to tell the client what ``main`` points at.
    """

    with tempfile.TemporaryDirectory(prefix="puppyone-git-receive-advertise-") as tmp:
        bare_dir = Path(tmp) / "repo.git"
        _ensure_bare_repo(bare_dir)
        view_head = _receive_pack_advertisement_head(repo, scope_path, scope_excludes)
        _raise_if_current_corrupt(view_head)
        _write_main_ref(bare_dir, view_head.head)
        _write_named_refs(bare_dir, _sanitize_named_refs(extra_refs or {}))
        yield bare_dir


def _receive_pack_advertisement_head(
    repo,
    scope_path: str,
    scope_excludes: list[str] | None,
) -> GitViewHead:
    return resolve_git_view_head(repo, scope_path, scope_excludes)


def _transport_cache_head(
    repo,
    scope_path: str,
    scope_excludes: list[str] | None,
) -> GitViewHead:
    return resolve_git_view_head(repo, scope_path, scope_excludes)


def _raise_if_current_corrupt(view_head: GitViewHead) -> None:
    if view_head.health != "current_corrupt":
        return
    reason = f": {view_head.reason}" if view_head.reason else ""
    raise GitViewCurrentCorruptError(
        "PuppyOne Git view is corrupt at the current version; restore or "
        f"repair the current tree before using Git{reason}"
    )


@contextmanager
def quarantine_bare_repo(
    repo,
    scope_path: str,
    scope_excludes: list[str] | None = None,
    *,
    include_blobs: bool = True,
    follow_history: bool = False,
    extra_refs: dict[str, str] | None = None,
) -> Path:
    """Yield an isolated object DB; callers choose boundary or full closure."""

    named_refs = _sanitize_named_refs(extra_refs or {})
    with transport_bare_repo(
        repo,
        scope_path,
        scope_excludes,
        follow_history=follow_history,
        include_blobs=include_blobs,
        extra_roots=list(named_refs.values()),
    ) as cache_bare:
        with tempfile.TemporaryDirectory(prefix="puppyone-git-quarantine-") as tmp:
            bare_dir = Path(tmp) / "repo.git"
            _ensure_bare_repo(bare_dir)
            alternates = bare_dir / "objects" / "info" / "alternates"
            alternates.parent.mkdir(parents=True, exist_ok=True)
            alternates.write_text(
                f"{(cache_bare / 'objects').resolve()}\n",
                encoding="utf-8",
            )
            cache_ref = cache_bare / "refs" / "heads" / "main"
            if cache_ref.exists():
                _write_main_ref(bare_dir, cache_ref.read_text(encoding="ascii").strip())
            _write_named_refs(bare_dir, named_refs)
            yield bare_dir


class GitObjectQuarantine:
    """Validated temporary Git object database for one receive-pack request."""

    def __init__(
        self,
        *,
        repo,
        bare_dir: Path,
        roots: list[str],
        exclude_roots: list[str] | None = None,
    ):
        self.repo = repo
        self.bare_dir = bare_dir
        self.roots = roots
        self.exclude_roots = exclude_roots or []
        self._promoted = False

    def get_object(self, object_id: str) -> tuple[str, bytes]:
        obj_type = run_git([
            "--git-dir",
            str(self.bare_dir),
            "cat-file",
            "-t",
            object_id,
        ]).decode("ascii", errors="replace").strip()
        body = run_git([
            "--git-dir",
            str(self.bare_dir),
            "cat-file",
            obj_type,
            object_id,
        ])
        return obj_type, body

    def object_size(self, object_id: str) -> int:
        raw = run_git([
            "--git-dir",
            str(self.bare_dir),
            "cat-file",
            "-s",
            object_id,
        ])
        return int(raw.decode("ascii", errors="replace").strip() or "0")

    def changed_paths(self, old_commit_id: str, new_commit_id: str) -> list[str]:
        if is_object_id(old_commit_id) and old_commit_id != ZERO_ID:
            out = run_git([
                "--git-dir",
                str(self.bare_dir),
                "diff-tree",
                "-r",
                "--no-commit-id",
                "--name-only",
                "-z",
                old_commit_id,
                new_commit_id,
            ])
        else:
            out = run_git([
                "--git-dir",
                str(self.bare_dir),
                "ls-tree",
                "-r",
                "-z",
                "--name-only",
                new_commit_id,
            ])
        return [
            item.decode("utf-8", errors="replace")
            for item in out.split(b"\x00")
            if item
        ]

    def commit_is_ancestor_or_same(self, ancestor_id: str, descendant_id: str) -> bool:
        if not (
            is_object_id(ancestor_id)
            and ancestor_id != ZERO_ID
            and is_object_id(descendant_id)
            and descendant_id != ZERO_ID
        ):
            return False
        if ancestor_id == descendant_id:
            return True
        try:
            run_git([
                "--git-dir",
                str(self.bare_dir),
                "merge-base",
                "--is-ancestor",
                ancestor_id,
                descendant_id,
            ])
            return True
        except Exception:
            return False

    def blob_id_for_path(self, tree_id: str, path: str) -> str:
        current = tree_id
        parts = [part for part in path.split("/") if part]
        for index, part in enumerate(parts):
            obj_type, body = self.get_object(current)
            if obj_type != "tree":
                return ""
            for entry in decode_tree(body):
                if entry.name != part:
                    continue
                if index == len(parts) - 1:
                    return "" if entry.mode == MODE_DIR else entry.sha1_hex
                if entry.mode != MODE_DIR:
                    return ""
                current = entry.sha1_hex
                break
            else:
                return ""
        return ""

    def mode_for_path(self, tree_id: str, path: str) -> bytes:
        """Git blob mode of ``path`` in the quarantined tree (A1-1).

        Lets the excluded-scope merge preserve the pushed file's mode
        (executable/symlink/gitlink) instead of defaulting to 100644.
        Returns ``MODE_FILE`` when the path is absent or a directory.
        """
        current = tree_id
        parts = [part for part in path.split("/") if part]
        for index, part in enumerate(parts):
            obj_type, body = self.get_object(current)
            if obj_type != "tree":
                return MODE_FILE
            for entry in decode_tree(body):
                if entry.name != part:
                    continue
                if index == len(parts) - 1:
                    return MODE_FILE if entry.mode == MODE_DIR else entry.mode
                if entry.mode != MODE_DIR:
                    return MODE_FILE
                current = entry.sha1_hex
                break
            else:
                return MODE_FILE
        return MODE_FILE

    def flatten_tree_to_bytes(self, tree_id: str) -> dict[str, bytes]:
        out: dict[str, bytes] = {}
        self._flatten_tree(tree_id, "", out)
        return out

    def promote_reachable(self) -> None:
        """Promote accepted reachable objects into PuppyOne's canonical store.

        The roots are accepted by stock Git and bounded by ``new --not old``.
        Git object ids are content hashes, so writing the same id again is
        idempotent; avoid remote existence probes on the receive-pack hot path.
        """

        if self._promoted:
            return
        objects_dir = self.bare_dir / "objects"
        object_ids = sorted(
            _reachable_object_ids_from_bare(
                self.bare_dir,
                self.roots,
                exclude_roots=self.exclude_roots,
            )
        )
        if not object_ids:
            self._promoted = True
            return

        with stage_object_writes(self.repo.store) as object_batch:
            with trace_phase(
                "git.quarantine.promote.materialize",
                count=len(object_ids),
            ):
                for object_id in object_ids:
                    self.repo.store.put_loose(
                        object_id,
                        self._loose_bytes_for_object(objects_dir, object_id),
                    )
            if object_batch is not None:
                with trace_phase(
                    "git.quarantine.promote.flush",
                    count=len(object_ids),
                ):
                    object_batch.flush()
        trace_mark("git.quarantine.promote.done", count=len(object_ids))
        self._promoted = True

    def _loose_bytes_for_object(self, objects_dir: Path, object_id: str) -> bytes:
        loose = objects_dir / object_id[:2] / object_id[2:]
        if loose.exists():
            return loose.read_bytes()
        obj_type, body = self.get_object(object_id)
        encoded_id, loose_bytes = encode_object(obj_type, body)
        if encoded_id != object_id:
            raise ValueError(
                f"quarantine object hash mismatch: {object_id} != {encoded_id}"
            )
        return loose_bytes

    def _flatten_tree(self, tree_id: str, prefix: str, out: dict[str, bytes]) -> None:
        obj_type, body = self.get_object(tree_id)
        if obj_type != "tree":
            raise ValueError(f"object {tree_id} is not a tree")
        for entry in decode_tree(body):
            path = f"{prefix}/{entry.name}" if prefix else entry.name
            if entry.mode == MODE_DIR:
                self._flatten_tree(entry.sha1_hex, path, out)
            else:
                blob_type, blob = self.get_object(entry.sha1_hex)
                if blob_type != "blob":
                    raise ValueError(f"object {entry.sha1_hex} is not a blob")
                out[path] = blob


class OfficialReceivePackQuarantine:
    """Official Git receive-pack result plus its temporary object DB."""

    def __init__(
        self,
        *,
        output: bytes,
        quarantine: GitObjectQuarantine,
        bare_dir: Path,
    ):
        self.output = output
        self.quarantine = quarantine
        self.bare_dir = bare_dir

    def ref_value(self, ref: str) -> str:
        try:
            out = run_git([
                "--git-dir",
                str(self.bare_dir),
                "rev-parse",
                "--verify",
                ref,
            ])
        except Exception:
            return ""
        return out.decode("ascii", errors="replace").strip()

    def ref_points_to(self, ref: str, object_id: str) -> bool:
        return bool(object_id and self.ref_value(ref) == object_id)


@contextmanager
def quarantine_pack(
    repo,
    scope_path: str,
    pack: bytes,
    roots: list[str] | None = None,
    exclude_roots: list[str] | None = None,
    scope_excludes: list[str] | None = None,
) -> GitObjectQuarantine:
    if not pack:
        raise ValueError("receive-pack request has no pack data")
    root_ids = roots or []
    with quarantine_bare_repo(repo, scope_path, scope_excludes) as bare_dir:
        _unpack_and_validate(bare_dir, pack, root_ids)
        yield GitObjectQuarantine(
            repo=repo,
            bare_dir=bare_dir,
            roots=root_ids,
            exclude_roots=exclude_roots or [],
        )


@contextmanager
def official_receive_pack_quarantine(
    repo,
    scope_path: str,
    request_path: Path,
    *,
    roots: list[str] | None = None,
    exclude_roots: list[str] | None = None,
    scope_excludes: list[str] | None = None,
    extra_refs: dict[str, str] | None = None,
) -> OfficialReceivePackQuarantine:
    """Run stock Git receive-pack against an isolated temporary repo.

    The temporary repo has alternates pointing at the transport cache, so Git
    can validate thin packs and existing ancestry exactly like a normal bare
    repository would. The receiver needs the full advertised ancestry: clients
    may omit any object reachable from that head, not just the current tree.
    A boundary-only cache incorrectly rejects reverts and ancestor tags after
    cache eviction. Full-closure receipts keep subsequent copies incremental.
    Ref advertisement stays refs-only; product writes never call this walker.

    The repo remains quarantine-only until admission permits object promotion;
    objects must be durable before Version Engine publishes their references.
    """

    root_ids = roots or []
    retry_after_invalidate = False
    with quarantine_bare_repo(
        repo,
        scope_path,
        scope_excludes,
        include_blobs=True,
        follow_history=True,
        extra_refs=extra_refs,
    ) as bare_dir:
        try:
            output = _run_official_receive_pack(bare_dir, request_path)
        except RuntimeError as exc:
            if not _is_unresolved_delta_error(exc):
                raise
            retry_after_invalidate = True
        else:
            yield OfficialReceivePackQuarantine(
                output=output,
                quarantine=GitObjectQuarantine(
                    repo=repo,
                    bare_dir=bare_dir,
                    roots=root_ids,
                    exclude_roots=exclude_roots or [],
                ),
                bare_dir=bare_dir,
            )
            return

    if retry_after_invalidate:
        trace_mark("git.receive_pack.retry_after_cache_invalidate")
        invalidate_git_view_cache(
            GitViewCacheKey.from_repo(
                repo,
                scope_path,
                scope_excludes,
                follow_history=True,
                include_blobs=True,
            )
        )
        with quarantine_bare_repo(
            repo,
            scope_path,
            scope_excludes,
            include_blobs=True,
            follow_history=True,
            extra_refs=extra_refs,
        ) as bare_dir:
            output = _run_official_receive_pack(bare_dir, request_path)
            yield OfficialReceivePackQuarantine(
                output=output,
                quarantine=GitObjectQuarantine(
                    repo=repo,
                    bare_dir=bare_dir,
                    roots=root_ids,
                    exclude_roots=exclude_roots or [],
                ),
                bare_dir=bare_dir,
            )


def _run_official_receive_pack(bare_dir: Path, request_path: Path) -> bytes:
    # Bound wall-clock time so a hostile pack cannot hang receive-pack (ISSUE-014).
    from src.config import settings
    timeout = settings.GIT_SUBPROCESS_TIMEOUT_SECONDS or None
    with request_path.open("rb") as stdin:
        try:
            proc = subprocess.run(
                ["git", "receive-pack", "--stateless-rpc", str(bare_dir)],
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"git receive-pack timed out after {timeout}s") from exc
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(stderr or "git receive-pack failed")
    stdout_text = proc.stdout.decode("utf-8", errors="replace")
    if _is_unresolved_delta_message(stdout_text):
        raise RuntimeError(stdout_text)
    return proc.stdout


def _is_unresolved_delta_error(exc: RuntimeError) -> bool:
    return _is_unresolved_delta_message(str(exc))


def _is_unresolved_delta_message(message: str) -> bool:
    return (
        "unresolved deltas left after unpacking" in message
        or "unpack-objects abnormal exit" in message
    )


def _unpack_and_validate(bare_dir: Path, pack: bytes, roots: list[str]) -> None:
    run_git(
        ["--git-dir", str(bare_dir), "unpack-objects", "-q"],
        input_data=pack,
    )
    _write_quarantine_refs(bare_dir, roots)
    run_git([
        "--git-dir",
        str(bare_dir),
        "fsck",
        "--connectivity-only",
        "--strict",
        "--no-dangling",
    ])


def _write_quarantine_refs(bare_dir: Path, roots: list[str]) -> None:
    refs_dir = bare_dir / "refs" / "puppyone" / "quarantine"
    for index, object_id in enumerate(roots):
        if not is_object_id(object_id) or object_id == ZERO_ID:
            continue
        refs_dir.mkdir(parents=True, exist_ok=True)
        (refs_dir / str(index)).write_text(f"{object_id}\n", encoding="ascii")


def _write_upload_pack_want_refs(bare_dir: Path, roots: list[str]) -> None:
    refs_dir = bare_dir / "refs" / "puppyone" / "upload-pack-wants"
    for index, object_id in enumerate(roots):
        if not is_object_id(object_id) or object_id == ZERO_ID:
            continue
        refs_dir.mkdir(parents=True, exist_ok=True)
        (refs_dir / str(index)).write_text(f"{object_id}\n", encoding="ascii")


def copy_reachable_objects_to_bare(
    repo,
    bare_dir: Path,
    roots: list[str],
    *,
    follow_history: bool = True,
    include_blobs: bool = True,
) -> None:
    objects_dir = bare_dir / "objects"
    # Object presence alone is not proof of a complete cached closure: a prior
    # materialization may have crashed after writing its root but before a child.
    # Separate receipts also prevent a shallow/blobless copy satisfying a full one.
    receipts = bare_dir / "puppyone-closure" / f"history-{int(follow_history)}-blobs-{int(include_blobs)}"
    missing_blobs: set[str] = set()
    seen: set[str] = set()
    kinds: dict[str, str] = {}
    expected_types: dict[str, str] = {}
    stack: list[tuple[str, ObjectKind | None]] = [
        (root, None)
        for root in roots
        if is_object_id(root) and root != ZERO_ID
    ]

    with trace_phase("git.transport_cache.walk", roots=len(stack)):
        while stack:
            object_id, expected_type = stack.pop()
            if expected_type is not None:
                previous_type = expected_types.setdefault(object_id, expected_type)
                if previous_type != expected_type or kinds.get(object_id, expected_type) != expected_type:
                    raise RuntimeError(f"required Git object {object_id} has conflicting type references")
            if object_id in seen:
                continue
            seen.add(object_id)
            receipt = receipts / object_id
            if _bare_has_object(bare_dir, object_id) and receipt.is_file():
                cached_kind = receipt.read_text(encoding="ascii")
                if cached_kind in {"blob", "tree", "commit", "tag"}:
                    if expected_type is not None and cached_kind != expected_type:
                        raise RuntimeError(f"required Git object {object_id} has unexpected cached type")
                    kinds[object_id] = cached_kind
                    continue

            if expected_type == "blob":
                if include_blobs:
                    missing_blobs.add(object_id)
                continue

            try:
                obj_type, body = repo.store.get_object(object_id)
            except Exception as exc:
                raise RuntimeError(
                    f"required Git object {object_id} is missing from the canonical store"
                ) from exc

            if expected_type is not None and obj_type != expected_type:
                raise RuntimeError(
                    f"required Git object {object_id} has type {obj_type}, expected {expected_type}"
                )
            encoded_id, loose = encode_object(obj_type, body)
            if encoded_id != object_id:
                raise RuntimeError(
                    f"canonical store returned mismatched Git object {encoded_id} "
                    f"for requested {object_id}"
                )
            _write_loose_object(objects_dir, object_id, loose)
            kinds[object_id] = obj_type

            try:
                stack.extend(object_edges(obj_type, body, follow_history=follow_history))
            except ValueError as exc:
                raise RuntimeError(f"{obj_type} object {object_id} cannot be decoded") from exc

    if missing_blobs:
        with trace_phase(
            "git.transport_cache.fetch_blobs",
            count=len(missing_blobs),
        ):
            found_blobs = _get_loose_many(repo.store, sorted(missing_blobs))
            still_missing = sorted(missing_blobs - set(found_blobs))
            if still_missing:
                sample = ", ".join(still_missing[:3])
                raise RuntimeError(
                    f"required Git blob objects are missing from the canonical store: {sample}"
                )
            for object_id, loose in found_blobs.items():
                kind, body = decode_object(loose)
                if hash_object(kind, body) != object_id:
                    raise RuntimeError(f"required Git object {object_id} has mismatched bytes")
                if kind != "blob":
                    raise RuntimeError(f"required Git object {object_id} has type {kind}, expected blob")
                _write_loose_object(objects_dir, object_id, loose)
                kinds[object_id] = kind

    # No receipt is issued until every required object and edge was verified.
    # An interruption before this point leaves harmless cache objects, not proof.
    receipts.mkdir(parents=True, exist_ok=True)
    for object_id, kind in kinds.items():
        (receipts / object_id).write_text(kind, encoding="ascii")


def _write_loose_object(objects_dir: Path, object_id: str, loose: bytes) -> None:
    target = objects_dir / object_id[:2] / object_id[2:]
    target.parent.mkdir(parents=True, exist_ok=True)
    # The cache may contain a truncated object from an interrupted old writer.
    # Atomically replace unverified bytes before issuing a closure receipt.
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as staged:
        temporary = Path(staged.name)
        try:
            staged.write(loose)
            staged.flush()
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)


def _get_loose_many(store, object_ids: list[str]) -> dict[str, bytes]:
    getter = getattr(store, "get_loose_many", None)
    if callable(getter):
        try:
            found = getter(object_ids)
            missing = [object_id for object_id in object_ids if object_id not in found]
            for object_id in missing:
                try:
                    found[object_id] = store.get_loose(object_id)
                except Exception:
                    continue
            return found
        except Exception:
            pass

    found: dict[str, bytes] = {}
    for object_id in object_ids:
        try:
            found[object_id] = store.get_loose(object_id)
        except Exception:
            continue
    return found


def _ensure_bare_repo(bare_dir: Path) -> None:
    if not (bare_dir / "objects").exists():
        run_git(["init", "--bare", str(bare_dir)])
    (bare_dir / "HEAD").write_text(
        f"ref: {ACCESS_POINT_MAIN_REF}\n", encoding="utf-8"
    )


def _write_main_ref(bare_dir: Path, head: str) -> None:
    ref_path = bare_dir / "refs" / "heads" / "main"
    if head:
        ref_path.parent.mkdir(parents=True, exist_ok=True)
        ref_path.write_text(f"{head}\n", encoding="ascii")
    elif ref_path.exists():
        ref_path.unlink()


def _bare_has_object(bare_dir: Path, object_id: str) -> bool:
    loose = bare_dir / "objects" / object_id[:2] / object_id[2:]
    return loose.exists()


def _reachable_object_ids_from_bare(
    bare_dir: Path,
    roots: list[str],
    *,
    exclude_roots: list[str] | None = None,
) -> set[str]:
    fast = _rev_list_object_ids_from_bare(
        bare_dir,
        roots,
        exclude_roots=exclude_roots or [],
    )
    if fast is not None:
        return fast

    reachable: set[str] = set()
    stack = [root for root in roots if is_object_id(root) and root != ZERO_ID]
    excluded = set(
        _reachable_object_ids_from_bare(bare_dir, exclude_roots or [])
        if exclude_roots else set()
    )
    while stack:
        object_id = stack.pop()
        if object_id in reachable or object_id in excluded:
            continue
        reachable.add(object_id)
        try:
            obj_type = run_git([
                "--git-dir",
                str(bare_dir),
                "cat-file",
                "-t",
                object_id,
            ]).decode("ascii", errors="replace").strip()
            body = run_git([
                "--git-dir",
                str(bare_dir),
                "cat-file",
                obj_type,
                object_id,
            ])
        except Exception as exc:
            raise RuntimeError(f"required Git object {object_id} cannot be read") from exc
        stack.extend(edge.oid for edge in object_edges(obj_type, body))
    return reachable


def _rev_list_object_ids_from_bare(
    bare_dir: Path,
    roots: list[str],
    *,
    exclude_roots: list[str],
) -> set[str] | None:
    include = [root for root in roots if is_object_id(root) and root != ZERO_ID]
    if not include:
        return set()
    exclude = [
        root for root in exclude_roots
        if is_object_id(root) and root != ZERO_ID
    ]
    args = [
        "--git-dir",
        str(bare_dir),
        "rev-list",
        "--objects",
        *include,
    ]
    if exclude:
        args.extend(["--not", *exclude])
    try:
        out = run_git(args)
    except Exception:
        return None

    object_ids: set[str] = set()
    for raw_line in out.splitlines():
        token = raw_line.split(maxsplit=1)[0].decode("ascii", errors="ignore")
        if is_object_id(token) and token != ZERO_ID:
            object_ids.add(token)
    return object_ids


def copy_bare_objects_to_store(repo, bare_dir: Path) -> None:
    objects_dir = bare_dir / "objects"
    for shard in objects_dir.iterdir():
        if not shard.is_dir() or len(shard.name) != 2:
            continue
        for obj in shard.iterdir():
            object_id = shard.name + obj.name
            if is_object_id(object_id):
                repo.store.put_loose(object_id, obj.read_bytes())
