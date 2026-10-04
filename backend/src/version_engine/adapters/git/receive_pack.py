"""Git receive-pack parsing and publish handoff."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import Response

from src.version_engine.adapters.git._async_context import sync_context_off_loop
from src.version_engine.adapters.git.object_quarantine import (
    official_receive_pack_quarantine,
)
from src.version_engine.adapters.git.protocol import (
    ACCESS_POINT_MAIN_REF,
    ZERO_ID,
    flush_pkt,
    is_object_id,
    pkt_line,
    read_pkt_lines,
)
from src.version_engine.adapters.git.refs import scope_named_refs
from src.version_engine.adapters.git.submission import submit_git_tree
from src.version_engine.adapters.git.view_projection import resolve_git_view_head
from src.version_engine.write_engine import tree as tree_mod
from src.version_engine.write_engine.git_object_format import (
    MODE_DIR,
    MODE_FILE,
    TreeEntry,
    decode_commit,
    encode_tree,
)
from src.version_engine.write_engine.engine import (
    CrossScopeSubmissionError,
    NonFastForwardSubmissionError,
)
from src.version_engine.write_engine.path_utils import normalize_path
from src.version_engine.write_engine.tree_objects import (
    is_path_excluded,
    validate_scope_bound_files,
)
from src.version_engine.infrastructure.supabase.repo_manager import VersionRepoManager
from src.utils.logger import log_error


class ReceiveCommand:
    def __init__(
        self,
        *,
        old_id: str,
        new_id: str,
        ref: str,
        pack: bytes,
        capabilities: set[str],
    ):
        self.old_id = old_id
        self.new_id = new_id
        self.ref = ref
        self.pack = pack
        self.capabilities = capabilities


async def receive_pack_response(
    *,
    repo_manager: VersionRepoManager,
    repo,
    project_id: str,
    scope_path: str,
    scope_excludes: list[str],
    actor: str,
    body: bytes,
    read_only: bool,
    audit_detail: dict | None = None,
) -> Response:
    tmp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            prefix="puppyone-git-receive-pack-",
            delete=False,
        ) as tmp:
            tmp.write(body)
            tmp_name = tmp.name
        return await receive_pack_response_from_path(
            repo_manager=repo_manager,
            repo=repo,
            project_id=project_id,
            scope_path=scope_path,
            scope_excludes=scope_excludes,
            actor=actor,
            request_path=Path(tmp_name),
            read_only=read_only,
            audit_detail=audit_detail,
        )
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass


async def receive_pack_response_from_path(
    *,
    repo_manager: VersionRepoManager,
    repo,
    project_id: str,
    scope_path: str,
    scope_excludes: list[str],
    actor: str,
    request_path: Path,
    read_only: bool,
    audit_detail: dict | None = None,
) -> Response:
    try:
        command = parse_receive_pack_request_file(request_path, allow_empty=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if command is None:
        try:
            async with sync_context_off_loop(
                official_receive_pack_quarantine(
                    repo,
                    scope_path,
                    request_path,
                    scope_excludes=scope_excludes,
                )
            ) as official:
                return _official_receive_pack_response(official.output)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if read_only:
        return receive_pack_result(
            command.ref,
            outcome="rejected",
            message="puppyone-rejected: access point is read-only",
            capabilities=command.capabilities,
        )
    ref_allowed, ref_reason = _ref_writability(command.ref)
    if not ref_allowed:
        return receive_pack_result(
            command.ref,
            outcome="rejected",
            message=f"puppyone-rejected: {ref_reason}",
            capabilities=command.capabilities,
        )
    if command.new_id == ZERO_ID:
        return receive_pack_result(
            command.ref,
            outcome="rejected",
            message="puppyone-rejected: delete is not supported; "
                    "scope-bound history is append-only",
            capabilities=command.capabilities,
            stderr_lines=[
                "PuppyOne: refs are append-only on this remote.",
                "PuppyOne: use the rollback API to back out a commit.",
            ],
        )

    try:
        official_ref_updated = False
        named_refs = scope_named_refs(repo, scope_path, strict=True)
        async with sync_context_off_loop(
            official_receive_pack_quarantine(
                repo,
                scope_path,
                request_path,
                roots=[command.new_id],
                exclude_roots=_named_ref_exclude_roots(
                    repo, project_id, scope_path, scope_excludes, command,
                    named_refs=named_refs,
                ),
                scope_excludes=scope_excludes,
                extra_refs=named_refs,
            )
        ) as official:
            official_ref_updated = official.ref_points_to(command.ref, command.new_id)
            if not official_ref_updated:
                # Object presence is not acceptance: existing objects also
                # remain readable after stock Git rejects a command. Never
                # promote or publish a ref that the receiver did not update.
                if official.output:
                    return _official_receive_pack_response(official.output)
                return receive_pack_result(
                    command.ref,
                    outcome="rejected",
                    message="puppyone-rejected: git receive-pack rejected update",
                    capabilities=command.capabilities,
                )
            quarantine = official.quarantine
            obj_type, commit_body = quarantine.get_object(command.new_id)
            if obj_type != "commit":
                return receive_pack_result(
                    command.ref,
                    outcome="rejected",
                    message="puppyone-rejected: pushed ref must point at a commit object",
                    capabilities=command.capabilities,
                )
            commit = decode_commit(commit_body)
            tree_id = commit.get("tree", "")
            if not tree_id:
                return receive_pack_result(
                    command.ref,
                    outcome="rejected",
                    message="puppyone-rejected: commit has no tree",
                    capabilities=command.capabilities,
                )
            parents = commit.get("parents") or []
            if len(parents) > 1:
                return receive_pack_result(
                    command.ref,
                    outcome="rejected",
                    message="puppyone-rejected: client merge commits are not supported; "
                            "fetch and rebase onto the remote main branch",
                    capabilities=command.capabilities,
                )

            changed_paths = quarantine.changed_paths(command.old_id, command.new_id)
            excluded_paths = _excluded_changed_paths(
                changed_paths,
                scope_path,
                scope_excludes,
            )
            if excluded_paths:
                return receive_pack_result(
                    command.ref,
                    outcome="rejected",
                    message=(
                        "puppyone-rejected: submission touches paths outside "
                        f"its scope or excluded paths: {', '.join(excluded_paths[:3])}"
                        f"{'…' if len(excluded_paths) > 3 else ''}"
                    ),
                    capabilities=command.capabilities,
                    stderr_lines=[
                        "PuppyOne: this push touches paths outside the scope "
                        "advertised by this remote.",
                    ],
                )

            # GAP-3: a push to any ref other than the scope head is stored as
            # a named ref in version_refs WITHOUT advancing the scope head.
            # An independent branch/tag is not fast-forward-comparable to main
            # and does not go through the scope-head write engine; we just
            # promote its objects (so the ref is durable + fetchable) and
            # record the pointer. Landing to the scope head is a separate
            # merge (push refs/heads/main).
            if command.ref != ACCESS_POINT_MAIN_REF:
                # Branch/tag pushes must respect the same scope-ownership
                # boundary as main (the main path enforces this inside the
                # write engine via validate_scope_bound_files; the named-ref
                # path skips submit, so check here). Otherwise a scoped
                # client could promote objects for paths owned by a sibling/
                # child scope under a branch ref. This subsumes the excludes
                # check above (ownership + excludes).
                out_of_scope = validate_scope_bound_files(
                    repo, scope_path, changed_paths, scope_excludes,
                )
                if out_of_scope:
                    return receive_pack_result(
                        command.ref,
                        outcome="rejected",
                        message=(
                            "puppyone-rejected: branch/tag push touches paths "
                            "outside this scope or excluded: "
                            f"{', '.join(out_of_scope[:3])}"
                            f"{'…' if len(out_of_scope) > 3 else ''}"
                        ),
                        capabilities=command.capabilities,
                        stderr_lines=[
                            "PuppyOne: a branch/tag may only touch paths owned "
                            "by the scope advertised by this remote.",
                        ],
                    )
                return _store_named_ref(
                    quarantine=quarantine,
                    official=official,
                    official_ref_updated=official_ref_updated,
                    project_id=project_id,
                    scope_path=scope_path,
                    actor=actor,
                    command=command,
                )

            current_scope_hash, current_head_commit_id = _get_scope_state(
                repo,
                scope_path,
            )
            git_view_head = resolve_git_view_head(repo, scope_path, scope_excludes)
            if git_view_head.health == "current_corrupt":
                return receive_pack_result(
                    command.ref,
                    outcome="rejected",
                    message=(
                        "puppyone-rejected: current Git view is corrupt; "
                        "restore or repair the current version before pushing"
                    ),
                    capabilities=command.capabilities,
                    stderr_lines=[
                        "PuppyOne: the current Access Point tree cannot be "
                        "projected into a valid Git repository.",
                        "PuppyOne: restore a healthy version or repair the "
                        "damaged current paths before retrying.",
                    ],
                )
            expected_old_id = "" if command.old_id == ZERO_ID else command.old_id
            if expected_old_id != (git_view_head.head or ""):
                return receive_pack_result(
                    command.ref,
                    outcome="rejected",
                    message=(
                        "puppyone-rejected: non-fast-forward update rejected"
                    ),
                    capabilities=command.capabilities,
                    stderr_lines=_NON_FAST_FORWARD_REMOTE_LINES,
                )
            if (
                git_view_head.head
                and not _is_fast_forward_commit(
                    quarantine,
                    git_view_head.head,
                    command.new_id,
                    commit,
                )
            ):
                return receive_pack_result(
                    command.ref,
                    outcome="rejected",
                    message=(
                        "puppyone-rejected: non-fast-forward update rejected"
                    ),
                    capabilities=command.capabilities,
                    stderr_lines=_NON_FAST_FORWARD_REMOTE_LINES,
                )

            promote_objects = quarantine.promote_reachable
            proposed_tree_id = tree_id
            # Git clients compare against the Git-visible projected HEAD. The
            # write engine CAS still protects the root, but root-first scopes
            # can be projected from the root tree before a scope-state cache
            # row exists. In that case there is no canonical scope head yet, so
            # keep the Git-visible base instead of translating it to "".
            canonical_base_commit_id = current_head_commit_id
            engine_base_commit_id = current_head_commit_id or expected_old_id
            if scope_excludes:
                # The merged tree is built against the canonical scope tree
                # (preserving hidden files) using the pushed blob hashes for
                # visible paths. The merged tree is written to the canonical
                # object store, but the BLOBS it references stay in quarantine
                # until publish succeeds — matching the non-excluded path's
                # promote-after-validate invariant. If submit_git_tree rejects
                # (CAS lost, conflict, etc.), the merged tree becomes an
                # unreachable orphan and object_gc will collect it.
                proposed_tree_id = _canonical_tree_for_excluded_scope_push(
                    repo,
                    quarantine,
                    current_scope_hash,
                    tree_id,
                    changed_paths,
                )
                engine_base_commit_id = current_head_commit_id or expected_old_id

            # E5: refuse LFS pointer blobs with a clear message before the
            # publish pipeline rather than silently committing them and
            # leaving readers staring at a 200-byte pointer file. Check only
            # changed small blobs; large blobs are never read into Python for
            # this guard.
            lfs_paths = detect_lfs_pointer_blobs(
                quarantine,
                tree_id,
                changed_paths,
            )
            if lfs_paths:
                return receive_pack_result(
                    command.ref,
                    outcome="rejected",
                    message=(
                        "puppyone-rejected: Git LFS is not supported on this "
                        f"remote (LFS pointers detected at: {', '.join(lfs_paths[:3])}"
                        f"{'…' if len(lfs_paths) > 3 else ''})"
                    ),
                    capabilities=command.capabilities,
                    stderr_lines=[
                        "PuppyOne: this repository does not terminate Git LFS.",
                        "PuppyOne: disable LFS for the affected paths "
                        "(`git lfs untrack <pattern>`) and push the actual file",
                        "PuppyOne: bytes, OR use the PuppyOne object upload API "
                        "for large binaries.",
                    ],
                )

            result = await submit_git_tree(
                repo_manager,
                project_id=project_id,
                scope_path=scope_path,
                actor=actor,
                base_commit_id=engine_base_commit_id,
                proposed_tree_id=proposed_tree_id,
                changed_paths=changed_paths,
                promote_objects=promote_objects,
                client_commit_id=command.new_id,
                message=commit.get("message", "") or "git push",
                scope_excludes=scope_excludes,
                defer_projection=True,
                audit_detail={
                    "source_channel": "access_git",
                    "protocol": "git",
                    "service": "receive-pack",
                    "ref": command.ref,
                    "old_commit_id": command.old_id if command.old_id != ZERO_ID else "",
                    "git_visible_old_commit_id": expected_old_id,
                    "canonical_base_commit_id": canonical_base_commit_id,
                    "engine_base_commit_id": engine_base_commit_id,
                    "git_view_health": git_view_head.health,
                    "git_view_history_cut": git_view_head.history_cut,
                    "remote_commit_id": command.new_id,
                    **(audit_detail or {}),
                },
            )
        if result.status == "pending":
            return receive_pack_result(
                command.ref,
                outcome="pending_resolution",
                message=(
                    f"puppyone-pending: review required "
                    f"(pending_conflict_id={result.pending_conflict_id})"
                ),
                capabilities=command.capabilities,
                stderr_lines=[
                    "PuppyOne: this push touched files that need manual review.",
                    f"PuppyOne: pending_conflict_id={result.pending_conflict_id}",
                    "PuppyOne: open the conflict in the PuppyOne UI to accept or "
                    "reject the resolution, then retry the push.",
                ],
            )
    except CrossScopeSubmissionError as exc:
        return receive_pack_result(
            command.ref,
            outcome="rejected",
            message=f"puppyone-rejected: {exc}",
            capabilities=command.capabilities,
            stderr_lines=[
                "PuppyOne: this push spans multiple scopes — split it across "
                "the corresponding scope remotes and retry.",
            ],
        )
    except NonFastForwardSubmissionError:
        return receive_pack_result(
            command.ref,
            outcome="rejected",
            message="puppyone-rejected: non-fast-forward update rejected",
            capabilities=command.capabilities,
            stderr_lines=_NON_FAST_FORWARD_REMOTE_LINES,
        )
    except Exception as exc:
        log_error(f"[GitReceivePack] submission failed: {type(exc).__name__}: {exc!r}")
        error_text = str(exc) or type(exc).__name__
        return receive_pack_result(
            command.ref,
            outcome="rejected",
            message=f"puppyone-rejected: {error_text}",
            capabilities=command.capabilities,
        )

    _ = result
    if official_ref_updated:
        return _official_receive_pack_response(official.output)
    return receive_pack_result(
        command.ref,
        outcome="committed",
        message="puppyone-committed",
        capabilities=command.capabilities,
    )


_LFS_POINTER_PREAMBLE = b"version https://git-lfs.github.com/spec/v1"
_NON_FAST_FORWARD_REMOTE_LINES = [
    "PuppyOne-Code: NON_FAST_FORWARD",
    "PuppyOne: Updates were rejected because the remote contains work that "
    "you do not have locally.",
    "PuppyOne: Fetch first, then rebase your work onto the remote main branch before "
    "pushing again.",
    "PuppyOne: Do not force push. PuppyOne Cloud only accepts fast-forward "
    "updates to main.",
]


def _is_fast_forward_commit(
    quarantine,
    current_head_commit_id: str,
    new_commit_id: str,
    decoded_commit: dict,
) -> bool:
    if current_head_commit_id == new_commit_id:
        return True
    if current_head_commit_id in (decoded_commit.get("parents") or []):
        return True
    return quarantine.commit_is_ancestor_or_same(
        current_head_commit_id,
        new_commit_id,
    )


def detect_lfs_pointer_blobs(
    quarantine,
    tree_id: str,
    paths: list[str],
) -> list[str]:
    """Return changed paths whose content is a Git LFS pointer.

    LFS pointers are short (<200 byte) text files with a fixed first
    line. PuppyOne does NOT terminate LFS today; we reject these with
    a clear error so the client can disable LFS for this remote or
    push the actual content. Silently committing the pointer would
    leave the real bytes unreachable through ``puppyone fs cat``.
    """

    flagged: list[str] = []
    for path in paths:
        blob_id = quarantine.blob_id_for_path(tree_id, path)
        if not blob_id:
            continue
        try:
            if quarantine.object_size(blob_id) > 256:
                continue
            blob_type, content = quarantine.get_object(blob_id)
        except Exception:
            continue
        if blob_type != "blob":
            continue
        head = content[:len(_LFS_POINTER_PREAMBLE)]
        if head == _LFS_POINTER_PREAMBLE:
            flagged.append(path)
    return flagged


def _excluded_changed_paths(
    paths: list[str],
    scope_path: str,
    scope_excludes: list[str],
) -> list[str]:
    if not scope_excludes:
        return []
    scope_norm = normalize_path(scope_path)
    excludes = [normalize_path(item) for item in scope_excludes]
    rejected: list[str] = []
    for path in paths:
        rel = normalize_path(path)
        full_path = f"{scope_norm}/{rel}" if scope_norm else rel
        if is_path_excluded(full_path, excludes):
            rejected.append(path)
    return rejected


def _canonical_tree_for_excluded_scope_push(
    repo,
    quarantine,
    current_scope_hash: str,
    pushed_tree_id: str,
    changed_paths: list[str],
) -> str:
    """Merge a filtered Git view push back into the canonical scope tree.

    Access Points with excludes advertise a filtered Git view. A client's new
    commit tree therefore does not contain hidden files. Publishing that tree
    directly would drop the hidden content; comparing it to the canonical head
    as a normal Git ancestor would also reject valid visible-only changes.
    Official Git already enforced fast-forward against the filtered view, so
    here we apply only the visible changed paths onto the canonical scope tree.

    Does NOT promote quarantined objects — the caller wires
    ``quarantine.promote_reachable`` as the engine's post-CAS callback so
    blob promotion happens only after publish succeeds. The merged tree
    written here references quarantined blob hashes; those references
    become live the moment promotion runs. If publish is rejected the
    tree becomes an unreachable orphan and object_gc collects it.
    """

    full_blob_ids: dict[str, str] = (
        tree_mod.tree_to_flat(repo.store, current_scope_hash)
        if current_scope_hash
        else {}
    )
    # A1-1: carry blob modes so a visible-only push doesn't downgrade hidden
    # OR changed executables/symlinks to 100644 when the canonical tree is
    # rebuilt. Unchanged paths keep the canonical mode; changed paths take
    # the mode from the client's pushed tree.
    full_modes: dict[str, bytes] = (
        tree_mod.tree_path_modes(repo.store, current_scope_hash)
        if current_scope_hash
        else {}
    )
    for raw_path in changed_paths:
        path = normalize_path(raw_path)
        if not path:
            continue
        blob_id = quarantine.blob_id_for_path(pushed_tree_id, path)
        if blob_id:
            full_blob_ids[path] = blob_id
            full_modes[path] = quarantine.mode_for_path(pushed_tree_id, path)
        else:
            full_blob_ids.pop(path, None)
            full_modes.pop(path, None)
    return _build_tree_from_blob_ids(repo.store, full_blob_ids, full_modes)


def _build_tree_from_blob_ids(
    store, files: dict[str, str], modes: dict[str, bytes] | None = None,
) -> str:
    modes = modes or {}
    nested: dict = {}
    for path, blob_id in files.items():
        clean = normalize_path(path)
        if not clean or not blob_id:
            continue
        parts = [part for part in clean.split("/") if part]
        node = nested
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        # leaf carries (blob_id, mode) so executable/symlink modes survive
        # the excluded-scope canonical rebuild (A1-1).
        node[parts[-1]] = (blob_id, modes.get(path, MODE_FILE))
    return _write_blob_id_tree(store, nested)


def _write_blob_id_tree(store, node: dict) -> str:
    entries: list[TreeEntry] = []
    for name, value in sorted(node.items()):
        if isinstance(value, dict):
            entries.append(TreeEntry(
                name=name,
                mode=MODE_DIR,
                sha1_hex=_write_blob_id_tree(store, value),
            ))
        else:
            blob_id, mode = value
            entries.append(TreeEntry(name=name, mode=mode, sha1_hex=blob_id))
    return store.put_tree(encode_tree(entries))


def _get_scope_state(repo, scope_path: str) -> tuple[str, str]:
    get_state = getattr(repo, "get_scope_state", None)
    if callable(get_state):
        scope_hash, head_commit_id = get_state(scope_path)
        return scope_hash or "", head_commit_id or ""
    return (
        repo.get_scope_hash(scope_path) or "",
        repo.get_scope_head_commit_id(scope_path) or "",
    )


def _named_ref_exclude_roots(
    repo, project_id, scope_path, scope_excludes, command, *, named_refs=None,
):
    """Already-promoted commit boundary for a branch/tag push (GAP-3).

    A branch/tag almost always descends from the scope head and/or existing
    stored refs, whose object closure is already promoted and therefore
    ABSENT from the receive-pack quarantine (Git only sends the new
    objects). Without an exclude boundary, ``promote_reachable`` walks from
    the pushed commit straight into that shared history and fails with
    "git cat-file: could not get object info". Excluding the scope head and
    existing ref tips bounds the walk to exactly the new objects the client
    sent — the same trick the fast-forward main push uses via ``old_id``.

    For ``refs/heads/main`` this returns precisely the historical value
    (``[old_id]`` when non-zero, else ``[]``), so the main path is
    unchanged.
    """
    excludes: list[str] = []
    if command.old_id != ZERO_ID and is_object_id(command.old_id):
        excludes.append(command.old_id)
    if command.ref == ACCESS_POINT_MAIN_REF:
        return excludes
    try:
        gv = resolve_git_view_head(repo, scope_path, scope_excludes)
        if gv and gv.head and is_object_id(gv.head):
            excludes.append(gv.head)
    except Exception as exc:  # noqa: BLE001 — bound best-effort; never block push setup
        log_error(f"[GitReceivePack] scope-head exclude lookup failed: {exc}")
    if named_refs is not None:
        # The write path supplies its already-required strict snapshot. Never
        # repeat the lookup or mistake a failed lookup for an empty namespace.
        excludes.extend(cid for cid in named_refs.values() if is_object_id(cid))
    else:
        # Retain this helper's best-effort optimization contract for older
        # callers. Omitting exclusions merely copies more objects; this is
        # NOT the receive admission/ref snapshot (which is fail-closed).
        try:
            from src.version_engine.infrastructure.supabase.version_ref_repository import (
                VersionRefStore,
            )
            for row in VersionRefStore().list_refs(project_id, scope_path):
                cid = row.get("commit_id")
                if cid and is_object_id(cid):
                    excludes.append(cid)
        except Exception as exc:  # noqa: BLE001
            log_error(f"[GitReceivePack] version_refs exclude lookup failed: {exc}")
    return list(dict.fromkeys(excludes))


def _store_named_ref(
    *,
    quarantine,
    official,
    official_ref_updated: bool,
    project_id: str,
    scope_path: str,
    actor: str,
    command,
):
    """Persist a branch/tag push as a version_refs row (GAP-3 Phase 1b).

    The commit's reachable objects are promoted into the canonical store
    so the ref is durable and fetchable, then the pointer is recorded.
    The scope head (``version_scope_state``) is deliberately untouched.
    """
    from src.version_engine.infrastructure.supabase.version_ref_repository import (
        VersionRefStore,
    )

    quarantine.promote_reachable()
    stored = VersionRefStore().set_ref(
        project_id=project_id,
        scope_path=scope_path,
        ref_name=command.ref,
        commit_id=command.new_id,
        created_by=actor,
    )
    if not stored:
        return receive_pack_result(
            command.ref,
            outcome="rejected",
            message="puppyone-rejected: failed to store named ref",
            capabilities=command.capabilities,
        )
    if official_ref_updated and official.output:
        return _official_receive_pack_response(official.output)
    return receive_pack_result(
        command.ref,
        outcome="committed",
        message=(
            "puppyone-committed: stored as a named ref (not landed to the "
            "scope head; push refs/heads/main to land)"
        ),
        capabilities=command.capabilities,
    )


def _ref_writability(ref: str) -> tuple[bool, str]:
    """Decide whether a pushed ref is writable on this scope remote.

    Three accepted shapes (GAP-3):
      - ``refs/heads/main`` — the scope head; lands through the write
        engine and advances ``version_scope_state``.
      - other ``refs/heads/*`` and ``refs/tags/*`` — stored as named refs
        in ``version_refs`` WITHOUT advancing the scope head. A stored ref
        is a durable, fetchable pointer to a promoted commit; landing it to
        the scope head is a separate merge step (push ``main``).

    Other namespaces (``refs/notes/*``, pseudo-refs, …) stay rejected.
    """

    if ref == ACCESS_POINT_MAIN_REF:
        return True, ""
    if ref.startswith(("refs/heads/", "refs/tags/")):
        return True, ""
    return False, (
        f"ref {ref!r} is not writable on this scope remote; "
        f"only refs/heads/* and refs/tags/* are accepted."
    )


def parse_receive_pack_request(
    body: bytes,
    *,
    allow_empty: bool = False,
) -> ReceiveCommand | None:
    payloads, pack_offset = read_pkt_lines(body)
    commands: list[tuple[str, str, str]] = []
    capabilities: set[str] = set()
    for payload in payloads:
        line = payload.rstrip(b"\n")
        if line.startswith(b"shallow "):
            continue
        command_line, separator, capability_line = line.partition(b"\0")
        line = command_line
        if separator:
            capabilities.update(
                item
                for item in capability_line.decode("ascii", errors="ignore").split()
                if item
            )
        parts = line.decode("ascii", errors="replace").split()
        if len(parts) != 3:
            raise ValueError("malformed receive-pack command")
        old_id, new_id, ref = parts
        if not (is_object_id(old_id) and is_object_id(new_id)):
            raise ValueError("malformed object id in receive-pack command")
        commands.append((old_id, new_id, ref))

    if not commands:
        if allow_empty:
            return None
        raise ValueError("receive-pack request has no ref update")
    if len(commands) > 1:
        raise ValueError("Puppyone Git remotes accept one scope-bound ref update per push")

    old_id, new_id, ref = commands[0]
    return ReceiveCommand(
        old_id=old_id,
        new_id=new_id,
        ref=ref,
        pack=body[pack_offset:],
        capabilities=capabilities,
    )


def parse_receive_pack_request_file(
    path: Path,
    *,
    allow_empty: bool = False,
) -> ReceiveCommand | None:
    return parse_receive_pack_request(
        _read_receive_pack_header(path),
        allow_empty=allow_empty,
    )


def _read_receive_pack_header(path: Path) -> bytes:
    header = bytearray()
    with path.open("rb") as handle:
        while True:
            raw_len = handle.read(4)
            if len(raw_len) < 4:
                raise ValueError("truncated pkt-line")
            header.extend(raw_len)
            try:
                size = int(raw_len, 16)
            except ValueError as exc:
                raise ValueError("invalid pkt-line length") from exc
            if size == 0:
                return bytes(header)
            if size < 4:
                raise ValueError("invalid pkt-line size")
            payload = handle.read(size - 4)
            if len(payload) < size - 4:
                raise ValueError("truncated pkt-line payload")
            header.extend(payload)


def _official_receive_pack_response(content: bytes) -> Response:
    return Response(
        content=content,
        media_type="application/x-git-receive-pack-result",
        headers={"Cache-Control": "no-cache"},
    )


_OUTCOMES_OK = frozenset({"committed"})
_OUTCOMES_REJECTED = frozenset({"rejected", "pending_resolution"})


def receive_pack_result(
    ref: str,
    *,
    outcome: str | None = None,
    message: str,
    capabilities: set[str] | None = None,
    stderr_lines: list[str] | None = None,
) -> Response:
    """Build a receive-pack response for one of the three V1 outcomes.

    Implements 01-version-engine.md §10.3:
      * ``committed`` → standard ``ok <ref>``
      * ``rejected``  → ``ng <ref> <reason>`` (split spans / engine error)
      * ``pending_resolution`` → ``ng`` plus an explicit reason tagged so
        tooling can recognise "needs human review" vs "real reject".

    When the client negotiated ``side-band`` / ``side-band-64k``, any
    ``stderr_lines`` are emitted on channel 2 so ``git push`` prints them
    as ``remote: ...`` lines before the rejection summary.

    """

    if outcome is None:
        raise ValueError("receive_pack_result requires outcome=")

    is_ok = outcome in _OUTCOMES_OK
    if not is_ok and outcome not in _OUTCOMES_REJECTED:
        raise ValueError(f"unknown receive-pack outcome: {outcome!r}")

    report_lines = [pkt_line(b"unpack ok\n")]
    if is_ok:
        report_lines.append(pkt_line(f"ok {ref}\n".encode("utf-8")))
    else:
        safe_message = message.replace("\n", " ")[:800]
        report_lines.append(pkt_line(f"ng {ref} {safe_message}\n".encode("utf-8")))
    report_lines.append(flush_pkt())
    report_status = b"".join(report_lines)

    use_sideband = bool(
        capabilities
        and ("side-band-64k" in capabilities or "side-band" in capabilities)
    )
    if not use_sideband:
        content = report_status
    else:
        chunks: list[bytes] = []
        # Channel 2 = stderr ("remote: ..." in the git client).
        for line in stderr_lines or []:
            safe = line.replace("\n", " ")[:900]
            chunks.append(pkt_line(b"\x02" + safe.encode("utf-8") + b"\n"))
        # Channel 1 = data (the report-status payload).
        chunks.append(pkt_line(b"\x01" + report_status))
        chunks.append(flush_pkt())
        content = b"".join(chunks)

    return Response(
        content=content,
        media_type="application/x-git-receive-pack-result",
        headers={"Cache-Control": "no-cache"},
    )
