"""Access Point scoped FS CLI API.

This router exposes POSIX-like file operations through an admitted Access
Surface credential and its resolved repository view.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json as _json
import re
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel as _BaseModel

from src.common_schemas import ApiResponse
from src.exceptions import ErrorCode
from src.platform.repository_target.auth_context import repository_view_from_auth
from src.platform.repository_target.models import repository_target_scope_id
from src.platform.upload.policy import (
    PER_FILE_MAX_BYTES as POLICY_PER_FILE_MAX_BYTES,
)
from src.platform.upload.policy import (
    path_has_blocked_segment,
)
from src.version_engine.adapters.product.commands import VersionWriteCommandService
from src.version_engine.adapters.product.operation_adapter import ProductOperationAdapter
from src.version_engine.admission.channel_pause import enforce_channel_pause
from src.version_engine.admission.connector_policy import admit_cli_fs_command
from src.version_engine.admission.permission import (
    ensure_mode_writable,
    ensure_repo_readable,
    is_mode_writable,
)
from src.version_engine.admission.repo_facade import (
    RepositoryViewResolutionError,
    repo_facade_from_auth,
)
from src.version_engine.admission.validation import (
    validate_limit,
    validate_path,
)
from src.version_engine.bootstrap.dependencies import (
    get_product_operation_adapter,
    get_version_write_command_service,
)
from src.version_engine.entrypoints.http.access_point import resolve_access_point
from src.version_engine.entrypoints.http.schemas import (
    CopyRequest,
    MkdirRequest,
    MoveRequest,
    RemoveRequest,
    RmdirRequest,
    TouchRequest,
    WriteFileRequest,
)
from src.version_engine.scoped_fs.capabilities import scoped_fs_capabilities
from src.version_engine.scoped_fs.context import ScopedFsContext
from src.version_engine.scoped_fs.errors import ScopedFsError
from src.version_engine.scoped_fs.indexed_grep import (
    IndexedGrepError,
    run_indexed_grep_payload,
)
from src.version_engine.scoped_fs.service import ScopedFsService
from src.version_engine.write_engine.engine import ConcurrentMutationError

router = APIRouter(prefix="/ap-fs", tags=["access-point-fs"])

_RECURSIVE_DEFAULT_LIMIT = 5000
_RECURSIVE_MAX_LIMIT = 50000
_GREP_DEFAULT_LIMIT = 1000
_GREP_MAX_LIMIT = 20000
_GREP_DEFAULT_FILE_LIMIT = 5000
_GREP_MAX_FILE_LIMIT = 50000
_GREP_DEFAULT_BYTE_LIMIT = 16 * 1024 * 1024
_GREP_MAX_BYTE_LIMIT = 256 * 1024 * 1024
_GREP_PATTERN_MAX_CHARS = 2048
_BINARY_SAMPLE_BYTES = 4096


def _is_not_found_error(exc: Exception) -> bool:
    """Normalize object-store not-found wrappers for the admin repair path."""

    message = str(exc).lower()
    return any(token in message for token in ("not found", "nosuchkey", "404", "does not exist"))


_TEXT_MIME_EXACT = frozenset(
    {
        "application/dart",
        "application/graphql",
        "application/javascript",
        "application/json",
        "application/sql",
        "application/toml",
        "application/typescript",
        "application/vnd.coffeescript",
        "application/x-bat",
        "application/x-csh",
        "application/x-ipynb+json",
        "application/x-ndjson",
        "application/x-php",
        "application/x-powershell",
        "application/x-sh",
        "application/x-subrip",
        "application/x-tcl",
        "application/x-tex",
        "application/xml",
        "application/yaml",
        "image/svg+xml",
    }
)
_TEXT_BASENAMES = frozenset(
    {
        ".babelrc",
        ".dockerignore",
        ".env",
        ".eslintrc",
        ".gitattributes",
        ".gitignore",
        ".npmrc",
        ".nvmrc",
        ".prettierrc",
        ".python-version",
        ".tool-versions",
        "Dockerfile",
        "Makefile",
        "README",
    }
)
_TEXT_BASENAMES_LOWER = frozenset(name.lower() for name in _TEXT_BASENAMES)


def _normalize_access_key(x_access_key: str | None) -> str:
    key = (x_access_key or "").strip()
    if not key:
        raise HTTPException(status_code=401, detail="X-Access-Key header is required")
    return key


async def _resolve_auth(
    x_access_key: str | None,
    x_puppyone_user: str | None,
    x_puppy_client: str | None = None,
    command: str | None = None,
) -> tuple[str, dict, dict]:
    project_id, auth = await asyncio.to_thread(
        resolve_access_point,
        _normalize_access_key(x_access_key),
    )

    bound_identity = auth.get("_user_identity", "")
    if bound_identity:
        if not x_puppyone_user:
            raise HTTPException(
                status_code=401,
                detail="X-PuppyOne-User header required: key is bound to a specific user",
            )
        if x_puppyone_user != bound_identity:
            raise HTTPException(
                status_code=401,
                detail="User identity mismatch: key is bound to a different user",
            )

    # Build scope_backend to compute carved_excludes (GAP-4 fix).
    # SupabaseClient() is a singleton so this does not open a new connection.
    from src.infra.supabase.client import SupabaseClient  # local import avoids circulars
    from src.version_engine.infrastructure.supabase.scope_repository import SupabaseScopeBackend

    scope_backend = SupabaseScopeBackend(SupabaseClient(), project_id)
    try:
        facade = repo_facade_from_auth(
            project_id, auth, kind="access_point", scope_backend=scope_backend
        )
    except RepositoryViewResolutionError as exc:
        raise HTTPException(
            status_code=503,
            detail="Repository view is temporarily unavailable",
            headers={"X-PuppyOne-Error-Code": str(ErrorCode.REPOSITORY_STORAGE_UNAVAILABLE.value)},
        ) from exc
    view = repository_view_from_auth(auth)
    scope_path = validate_path(facade.scope_path)
    mode = facade.mode
    ensure_repo_readable(facade)

    # /ap-fs is the PuppyOne FS CLI command surface. It is governed by the
    # built-in CLI access surface.
    normalized_client = (x_puppy_client or "").strip().lower()
    if not normalized_client:
        raise HTTPException(
            status_code=400,
            detail="X-Puppy-Client header required",
        )
    if normalized_client != "cli":
        raise HTTPException(
            status_code=400,
            detail="AP-FS requires X-Puppy-Client: cli",
        )
    if command:
        admit_cli_fs_command(auth, command, normalized_client, log_prefix="[AP-FS]")
    else:
        enforce_channel_pause(auth, normalized_client, log_prefix="[AP-FS]")

    normalized_scope = {
        "id": repository_target_scope_id(view.target) or auth.get("agent"),
        "repo_id": facade.repo_id,
        "repo_kind": facade.kind,
        "repo_ref": facade.ref,
        "object_store_scope": facade.object_store_scope,
        "path": scope_path,
        "mode": mode,
        "exclude": list(facade.excludes),
    }
    return project_id, auth, normalized_scope


def _ensure_writable(scope: dict) -> None:
    ensure_mode_writable(str(scope.get("mode", "r")))


def _upload_max_bytes_for_project(project_id: str) -> int:
    from src.platform.entitlements.service import EntitlementService
    from src.platform.project.repository import ProjectRepositorySupabase

    project = ProjectRepositorySupabase().get_by_id(project_id)
    if project is None:
        return POLICY_PER_FILE_MAX_BYTES
    limit = EntitlementService().enforced_limit_value(
        project.org_id,
        "upload.max_single_file_bytes",
    )
    return int(limit) if limit is not None else POLICY_PER_FILE_MAX_BYTES


def _fs_error(
    status_code: int,
    error_code: str,
    message: str,
    *,
    path: str | None = None,
) -> HTTPException:
    detail: dict[str, Any] = {
        "error_code": error_code,
        "message": message,
    }
    if path is not None:
        detail["path"] = path
    return HTTPException(status_code=status_code, detail=detail)


def _clean_relative(path: str | None) -> str:
    raw = (path or "").strip()
    if raw in ("", "/", "."):
        return ""
    return validate_path(raw)


def _join_scope(scope_path: str, relative_path: str) -> str:
    if not scope_path:
        return relative_path
    if not relative_path:
        return scope_path
    return f"{scope_path}/{relative_path}"


def _relative_to_scope(full_path: str, scope_path: str) -> str:
    clean = full_path.strip("/")
    scope = scope_path.strip("/")
    if not scope:
        return clean
    if clean == scope:
        return ""
    prefix = f"{scope}/"
    if clean.startswith(prefix):
        return clean[len(prefix) :]
    return clean


def _matches_exclude(relative_path: str, excludes: list[Any]) -> bool:
    rel = relative_path.strip("/")
    if not rel:
        return False
    segments = rel.split("/")
    for item in excludes:
        pattern = str(item).strip("/")
        if not pattern:
            continue
        if "/" in pattern:
            if rel == pattern or rel.startswith(f"{pattern}/"):
                return True
        elif pattern in segments:
            return True
    return False


def _assert_not_excluded(relative_path: str, scope: dict) -> None:
    if _matches_exclude(relative_path, scope.get("exclude") or []):
        raise HTTPException(
            status_code=403, detail=f"Path is excluded from this access point: {relative_path}"
        )


def _assert_upload_policy(relative_path: str) -> None:
    """PUP-3 defense-in-depth: reject paths whose segments match the
    hardcoded blocklist (``.git``, ``node_modules``, …).

    Read-only operations don't call this — only the write endpoints
    (upload, write_file, mkdir, touch). The product contract is in
    ``docs/proposals/PUP-3-folder-upload-policy.md``.
    """
    is_blocked, seg = path_has_blocked_segment(relative_path)
    if is_blocked:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "policy_blocked",
                "segment": seg,
                "path": relative_path,
                "message": (
                    f"Path '{relative_path}' contains the blocked segment "
                    f"'{seg}' (PUP-3 folder-upload policy)."
                ),
            },
        )


def _entry_to_scoped_response(entry, scope: dict) -> dict:
    rel_path = _relative_to_scope(entry.path, scope["path"])
    return {
        "name": entry.name,
        "path": rel_path,
        "version_path": entry.path,
        "type": entry.type,
        "content_hash": entry.content_hash,
        "size_bytes": entry.size_bytes,
        "mime_type": entry.mime_type,
        "children_count": entry.children_count,
        "integrity_status": getattr(entry, "integrity_status", "ok"),
        "created_at": getattr(entry, "created_at", None),
        "modified_at": getattr(entry, "modified_at", None),
    }


def _is_hidden_path(path: str) -> bool:
    return any(part.startswith(".") for part in path.strip("/").split("/") if part)


def _filter_entries(entries: list, scope: dict, *, include_hidden: bool = False) -> list:
    filtered = []
    excludes = list(scope.get("exclude") or [])
    for entry in entries:
        rel_path = _relative_to_scope(entry.path, scope["path"])
        if not include_hidden and _is_hidden_path(rel_path):
            continue
        if _matches_exclude(rel_path, excludes):
            continue
        filtered.append(entry)
    return filtered


def _filter_directories(entries: list) -> list:
    return [entry for entry in entries if entry.type == "folder"]


def _scope_payload(scope: dict) -> dict:
    return {
        "id": scope.get("id", ""),
        "repo_id": scope.get("repo_id", ""),
        "repo_kind": scope.get("repo_kind", ""),
        "repo_ref": scope.get("repo_ref", "refs/heads/main"),
        "path": scope["path"],
        "mode": scope["mode"],
        "exclude": scope.get("exclude") or [],
    }


def _query_bool(value: Any, default: bool = False) -> bool:
    return value if isinstance(value, bool) else default


def _query_int(value: Any, default: int) -> int:
    return value if isinstance(value, int) else default


def _query_optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) else None


def _query_limited_int(value: Any, default: int, maximum: int) -> int:
    return validate_limit(_query_int(value, default), default=default, maximum=maximum)


def _operator(auth: dict) -> str:
    return f"access_point:{auth.get('agent', 'unknown')}"


def _scoped_fs_context(
    *,
    access_key: str | None,
    project_id: str,
    auth: dict,
    scope: dict,
) -> ScopedFsContext:
    scope_id = str(scope.get("id") or auth.get("agent") or "")
    return ScopedFsContext(
        api_key=(access_key or "").strip(),
        endpoint_id=scope_id,
        endpoint_name=str(auth.get("name") or scope_id),
        project_id=project_id,
        user_id=_operator(auth),
        scope_id=scope_id,
        scope_path=str(scope.get("path") or ""),
        mode="rw" if is_mode_writable(str(scope.get("mode", "r"))) else "ro",
        exclude=list(scope.get("exclude") or []),
        allowed_tools=None,
        channel="cli",
    )


def _scoped_fs_error(exc: ScopedFsError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.message},
    )


def _ops_stat(
    ops: ProductOperationAdapter,
    project_id: str,
    scope: dict,
    rel_path: str,
    *,
    include_size: bool = False,
):
    scoped = getattr(ops, "stat_in_scope", None)
    if scoped is not None:
        return scoped(
            project_id,
            scope["path"],
            rel_path,
            include_size=include_size,
        )
    return ops.stat(
        project_id,
        _join_scope(scope["path"], rel_path),
        include_size=include_size,
    )


def _ops_list_dir(
    ops: ProductOperationAdapter,
    project_id: str,
    scope: dict,
    rel_path: str,
    *,
    include_size: bool = False,
):
    scoped = getattr(ops, "list_dir_in_scope", None)
    if scoped is not None:
        return scoped(
            project_id,
            scope["path"],
            rel_path,
            include_size=include_size,
        )
    return ops.list_dir(
        project_id,
        _join_scope(scope["path"], rel_path),
        include_size=include_size,
    )


def _ops_list_tree(
    ops: ProductOperationAdapter,
    project_id: str,
    scope: dict,
    rel_path: str,
    max_depth: int,
    *,
    include_size: bool = False,
    max_entries: int | None = None,
):
    scoped = getattr(ops, "list_tree_in_scope", None)
    if scoped is not None:
        return scoped(
            project_id,
            scope["path"],
            rel_path,
            max_depth=max_depth,
            include_size=include_size,
            max_entries=max_entries,
        )
    return ops.list_tree(
        project_id,
        _join_scope(scope["path"], rel_path),
        max_depth=max_depth,
        include_size=include_size,
        max_entries=max_entries,
    )


def _ops_read_file(ops: ProductOperationAdapter, project_id: str, scope: dict, rel_path: str):
    scoped = getattr(ops, "read_file_in_scope", None)
    if scoped is not None:
        return scoped(project_id, scope["path"], rel_path)
    return ops.read_file(project_id, _join_scope(scope["path"], rel_path))


def _ops_read_file_range(
    ops: ProductOperationAdapter,
    project_id: str,
    scope: dict,
    rel_path: str,
    *,
    start: int = 0,
    limit: int | None = None,
):
    scoped = getattr(ops, "read_file_range_in_scope", None)
    if scoped is not None:
        return scoped(
            project_id,
            scope["path"],
            rel_path,
            start=start,
            limit=limit,
        )
    return ops.read_file_range(
        project_id,
        _join_scope(scope["path"], rel_path),
        start=start,
        limit=limit,
    )


def _looks_text_entry(entry) -> bool:
    if getattr(entry, "type", "") in {"json", "markdown"}:
        return True
    mime = (getattr(entry, "mime_type", "") or "").lower()
    if mime.startswith("text/"):
        return True
    if mime in _TEXT_MIME_EXACT:
        return True
    base = _basename(getattr(entry, "path", "") or getattr(entry, "name", ""))
    return base.lower() in _TEXT_BASENAMES_LOWER


def _looks_binary(content: bytes) -> bool:
    sample = content[:_BINARY_SAMPLE_BYTES]
    return b"\x00" in sample


def _decode_grep_text(content: bytes) -> str:
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return content.decode("utf-8", errors="replace")


def _split_grep_globs(value: str | None) -> list[str]:
    if not isinstance(value, str) or not value:
        return []
    return [item.strip() for item in str(value).splitlines() if item.strip()]


def _matches_grep_glob(path: str, pattern: str) -> bool:
    clean = path.strip("/")
    base = _basename(clean)
    return fnmatch.fnmatchcase(clean, pattern) or fnmatch.fnmatchcase(base, pattern)


def _matches_any_grep_glob(path: str, patterns: list[str]) -> bool:
    return any(_matches_grep_glob(path, pattern) for pattern in patterns)


def _matches_exclude_dir_glob(path: str, patterns: list[str]) -> bool:
    parts = [part for part in path.strip("/").split("/")[:-1] if part]
    return any(fnmatch.fnmatchcase(part, pattern) for part in parts for pattern in patterns)


def _grep_file_depth(path: str, root_path: str = "") -> int:
    clean = path.strip("/")
    root = root_path.strip("/")
    if root and clean == root:
        return 0
    rel = clean[len(root) + 1 :] if root and clean.startswith(f"{root}/") else clean
    parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
    if not parent:
        return 0
    return len([part for part in parent.split("/") if part])


def _grep_matcher(pattern: str, *, regex: bool, ignore_case: bool):
    if len(pattern) > _GREP_PATTERN_MAX_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"grep pattern exceeds {_GREP_PATTERN_MAX_CHARS} characters",
        )

    if regex:
        flags = re.IGNORECASE if ignore_case else 0
        try:
            compiled = re.compile(pattern, flags)
        except re.error as exc:
            raise HTTPException(status_code=400, detail=f"Invalid regex: {exc}") from exc

        def _match(line: str) -> list[tuple[int, int]]:
            spans: list[tuple[int, int]] = []
            for match in compiled.finditer(line):
                start, end = match.start(), match.end()
                if start == end:
                    continue
                spans.append((start, end))
            return spans

        return _match

    needle = pattern.casefold() if ignore_case else pattern

    def _fixed_match(line: str) -> list[tuple[int, int]]:
        if needle == "":
            return [(0, 0)]
        haystack = line.casefold() if ignore_case else line
        spans: list[tuple[int, int]] = []
        start_at = 0
        while True:
            index = haystack.find(needle, start_at)
            if index < 0:
                break
            spans.append((index, index + len(pattern)))
            start_at = index + max(len(needle), 1)
        return spans

    return _fixed_match


def _basename(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1]


def _destination_child_path(directory_rel: str, old_rel: str) -> str:
    child_name = _basename(old_rel)
    clean_dir = directory_rel.rstrip("/")
    return f"{clean_dir}/{child_name}" if clean_dir else child_name


def _dirname(path: str) -> str:
    clean = path.strip("/")
    if not clean or "/" not in clean:
        return ""
    return clean.rsplit("/", 1)[0]


def _is_descendant_destination(old_rel: str, new_rel: str) -> bool:
    old_clean = old_rel.strip("/")
    new_clean = new_rel.strip("/")
    return bool(old_clean and new_clean.startswith(f"{old_clean}/"))


def _resolve_copy_move_destination(
    project_id: str,
    scope: dict,
    ops: ProductOperationAdapter,
    old_rel: str,
    new_rel: str,
    *,
    target_directory: bool,
    no_target_directory: bool,
) -> tuple[str, str, Any | None]:
    if target_directory and no_target_directory:
        raise HTTPException(
            status_code=400,
            detail="target_directory and no_target_directory cannot both be true",
        )

    new_full = _join_scope(scope["path"], new_rel)
    new_entry = _ops_stat(ops, project_id, scope, new_rel)

    if target_directory:
        if new_entry is None or new_entry.type != "folder":
            raise _fs_error(
                400,
                "NOT_A_DIRECTORY",
                f"Not a directory: {new_rel or '.'}",
                path=new_rel,
            )
        new_rel = _destination_child_path(new_rel, old_rel)
        _assert_not_excluded(new_rel, scope)
        new_full = _join_scope(scope["path"], new_rel)
        new_entry = _ops_stat(ops, project_id, scope, new_rel)
    elif new_entry and new_entry.type == "folder" and not no_target_directory:
        new_rel = _destination_child_path(new_rel, old_rel)
        _assert_not_excluded(new_rel, scope)
        new_full = _join_scope(scope["path"], new_rel)
        new_entry = _ops_stat(ops, project_id, scope, new_rel)

    return new_rel, new_full, new_entry


def _is_directory_empty(
    project_id: str, rel_path: str, scope: dict, ops: ProductOperationAdapter
) -> bool:
    return len(_ops_list_dir(ops, project_id, scope, rel_path)) == 0


def _rmdir_chain(
    project_id: str, rel_path: str, scope: dict, ops: ProductOperationAdapter, *, parents: bool
) -> list[str]:
    """Return deepest-first empty directory chain removable by rmdir."""
    entry = _ops_stat(ops, project_id, scope, rel_path)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"No such file or directory: {rel_path}")
    if entry.type != "folder":
        raise HTTPException(status_code=400, detail=f"Not a directory: {rel_path}")
    if not _is_directory_empty(project_id, rel_path, scope, ops):
        raise HTTPException(status_code=400, detail=f"Directory not empty: {rel_path}")

    removable = [rel_path]
    if not parents:
        return removable

    child_rel = rel_path
    parent_rel = _dirname(rel_path)
    while parent_rel:
        _assert_not_excluded(parent_rel, scope)
        parent = _ops_stat(ops, project_id, scope, parent_rel)
        if parent is None or parent.type != "folder":
            break
        remaining = [
            e
            for e in _ops_list_dir(ops, project_id, scope, parent_rel)
            if e.path.strip("/") != _join_scope(scope["path"], child_rel).strip("/")
        ]
        if remaining:
            break
        removable.append(parent_rel)
        child_rel = parent_rel
        parent_rel = _dirname(parent_rel)

    return removable


def _attach_timestamps(
    project_id: str,
    entries: list,
    ops: ProductOperationAdapter,
    *,
    extra_paths: list[str] | None = None,
) -> None:
    paths = [entry.path for entry in entries]
    if extra_paths:
        paths.extend(extra_paths)
    timestamps = ops.get_path_timestamps(project_id, paths)
    for entry in entries:
        data = timestamps.get(entry.path.strip("/")) or {}
        entry.created_at = data.get("created_at") or None
        entry.modified_at = data.get("modified_at") or None


@router.get("/semantics", response_model=ApiResponse)
async def semantics(
    x_access_key: str | None = Header(None, alias="X-Access-Key"),
    x_puppyone_user: str | None = Header(None, alias="X-PuppyOne-User"),
    x_puppy_client: str | None = Header(None, alias="X-Puppy-Client"),
):
    project_id, _auth, scope = await _resolve_auth(
        x_access_key,
        x_puppyone_user,
        x_puppy_client,
        command="semantics",
    )
    data = scoped_fs_capabilities(
        writable=is_mode_writable(str(scope.get("mode", "r"))),
    )
    data["project_id"] = project_id
    data["scope"] = _scope_payload(scope)
    return ApiResponse.success(data=data)


@router.get("/ls", response_model=ApiResponse)
async def list_dir(
    path: str = Query("", description="Path relative to the access point scope"),
    include_hidden: bool = Query(False, description="Include entries whose names begin with '.'"),
    include_size: bool = Query(False, description="Include file sizes by reading file blobs"),
    include_times: bool = Query(
        False, description="Include timestamps derived from version history"
    ),
    x_access_key: str | None = Header(None, alias="X-Access-Key"),
    x_puppyone_user: str | None = Header(None, alias="X-PuppyOne-User"),
    x_puppy_client: str | None = Header(None, alias="X-Puppy-Client"),
    ops: ProductOperationAdapter = Depends(get_product_operation_adapter),
):
    project_id, _auth, scope = await _resolve_auth(
        x_access_key,
        x_puppyone_user,
        x_puppy_client,
        command="ls",
    )
    ops = ops.for_grant(_auth["_runtime_grant"])
    rel_path = _clean_relative(path)
    _assert_not_excluded(rel_path, scope)

    full_path = _join_scope(scope["path"], rel_path)
    target = _ops_stat(ops, project_id, scope, rel_path, include_size=include_size)
    if target is None and rel_path:
        raise HTTPException(status_code=404, detail=f"Path not found: {rel_path}")
    target_type = target.type if target else ""
    if target and target.type != "folder":
        # POSIX ls on a file lists that file itself, not the parent directory.
        entries = [target]
    else:
        entries = _filter_entries(
            _ops_list_dir(ops, project_id, scope, rel_path, include_size=include_size),
            scope,
            include_hidden=include_hidden,
        )
    if include_times:
        _attach_timestamps(project_id, entries, ops, extra_paths=[full_path])
    return ApiResponse.success(
        data={
            "path": rel_path,
            "version_path": full_path,
            "scope": _scope_payload(scope),
            "target_type": target_type,
            "entries": [_entry_to_scoped_response(e, scope) for e in entries],
            "head_commit_id": ops.get_head_commit_id(project_id),
        }
    )


@router.get("/tree", response_model=ApiResponse)
async def tree(
    path: str = Query("", description="Path relative to the access point scope"),
    max_depth: int = Query(-1, description="Maximum recursion depth, -1 = unlimited"),
    limit: int = Query(
        _RECURSIVE_DEFAULT_LIMIT,
        description="Maximum entries returned before truncation",
    ),
    include_hidden: bool = Query(False, description="Include entries whose names begin with '.'"),
    include_size: bool = Query(False, description="Include file sizes by reading file blobs"),
    include_times: bool = Query(
        False, description="Include timestamps derived from version history"
    ),
    directories_only: bool = Query(False, description="Only include directories"),
    x_access_key: str | None = Header(None, alias="X-Access-Key"),
    x_puppyone_user: str | None = Header(None, alias="X-PuppyOne-User"),
    x_puppy_client: str | None = Header(None, alias="X-Puppy-Client"),
    ops: ProductOperationAdapter = Depends(get_product_operation_adapter),
):
    project_id, _auth, scope = await _resolve_auth(
        x_access_key,
        x_puppyone_user,
        x_puppy_client,
        command="tree",
    )
    ops = ops.for_grant(_auth["_runtime_grant"])
    rel_path = _clean_relative(path)
    _assert_not_excluded(rel_path, scope)
    max_depth = _query_int(max_depth, -1)
    include_hidden = _query_bool(include_hidden)
    include_size = _query_bool(include_size)
    include_times = _query_bool(include_times)
    directories_only = _query_bool(directories_only)
    safe_limit = validate_limit(
        _query_int(limit, _RECURSIVE_DEFAULT_LIMIT),
        default=_RECURSIVE_DEFAULT_LIMIT,
        maximum=_RECURSIVE_MAX_LIMIT,
    )

    full_path = _join_scope(scope["path"], rel_path)
    target = _ops_stat(ops, project_id, scope, rel_path, include_size=include_size)
    if target is None and rel_path:
        raise HTTPException(status_code=404, detail=f"Path not found: {rel_path}")
    target_type = target.type if target else ""
    if target and target.type != "folder":
        entries = [] if directories_only else [target]
        truncated = False
    else:
        entries = _filter_entries(
            _ops_list_tree(
                ops,
                project_id,
                scope,
                rel_path,
                max_depth=max_depth,
                include_size=include_size,
                max_entries=safe_limit + 1,
            ),
            scope,
            include_hidden=include_hidden,
        )
        truncated = len(entries) > safe_limit
        if truncated:
            entries = entries[:safe_limit]
        if directories_only:
            entries = _filter_directories(entries)
    if include_times:
        _attach_timestamps(project_id, entries, ops, extra_paths=[full_path])
    response_entries = [_entry_to_scoped_response(e, scope) for e in entries]
    return ApiResponse.success(
        data={
            "path": rel_path,
            "version_path": full_path,
            "scope": _scope_payload(scope),
            "target_type": target_type,
            "directories_only": directories_only,
            "limit": safe_limit,
            "returned_count": len(response_entries),
            "complete": not truncated,
            "truncated": truncated,
            "truncation_reason": "entry_limit_exceeded" if truncated else "",
            "entries": response_entries,
            "head_commit_id": ops.get_head_commit_id(project_id),
        }
    )


# Legacy S3-scan grep — KEEP. Used by canonical ``GET /grep`` as the
# authoritative fallback when the indexed core is not fresh enough. The
# trade-off is: this path can find content the indexer hasn't yet
# processed, at the cost of an O(scope size) S3 read + Python
# regex scan. See ``docs/proposals/PUP-cloud-grep.md``.
@router.get("/grep", response_model=ApiResponse)
async def grep(
    pattern: str = Query(..., description="Fixed string or regex pattern to match"),
    path: str = Query("", description="File or directory path relative to the access point scope"),
    ref: str = Query(
        "",
        description="Reserved; leave empty to read the native repository HEAD.",
    ),
    regex: bool = Query(False, description="Treat pattern as a regular expression"),
    ignore_case: bool = Query(False, description="Case-insensitive matching"),
    invert_match: bool = Query(False, description="Select non-matching lines"),
    only_matching: bool = Query(False, description="Return only the matching text"),
    include_hidden: bool = Query(False, description="Include entries whose names begin with '.'"),
    include: str = Query("", description="Newline-separated file glob patterns to include"),
    exclude: str = Query("", description="Newline-separated file glob patterns to exclude"),
    exclude_dir: str = Query(
        "", description="Newline-separated directory glob patterns to exclude"
    ),
    max_depth: int = Query(
        -1, description="Maximum recursion depth for directories, -1 = unlimited"
    ),
    max_count: int = Query(
        0, description="Maximum matching lines returned per file, 0 = unlimited"
    ),
    require_file_list: bool = Query(
        False, description="Require every candidate file in files[], including zero-match files"
    ),
    before_context: int = Query(0, description="Context lines before each match"),
    after_context: int = Query(0, description="Context lines after each match"),
    include_offsets: bool = Query(False, description="Include byte offsets in match metadata"),
    limit: int = Query(_GREP_DEFAULT_LIMIT, description="Maximum matching lines returned"),
    max_files: int = Query(_GREP_DEFAULT_FILE_LIMIT, description="Maximum file candidates scanned"),
    max_bytes: int = Query(
        _GREP_DEFAULT_BYTE_LIMIT, description="Maximum decoded text bytes scanned"
    ),
    x_access_key: str | None = Header(None, alias="X-Access-Key"),
    x_puppyone_user: str | None = Header(None, alias="X-PuppyOne-User"),
    x_puppy_client: str | None = Header(None, alias="X-Puppy-Client"),
    ops: ProductOperationAdapter = Depends(get_product_operation_adapter),
):
    project_id, _auth, scope = await _resolve_auth(
        x_access_key,
        x_puppyone_user,
        x_puppy_client,
        command="grep",
    )
    ops = ops.for_grant(_auth["_runtime_grant"])
    rel_path = _clean_relative(path)
    _assert_not_excluded(rel_path, scope)
    max_depth = _query_int(max_depth, -1)
    include_hidden = _query_bool(include_hidden)
    invert_match = _query_bool(invert_match)
    only_matching = _query_bool(only_matching)
    include_offsets = _query_bool(include_offsets)
    require_file_list = _query_bool(require_file_list)
    per_file_limit = max(0, _query_int(max_count, 0))
    before_context = min(max(0, _query_int(before_context, 0)), 100)
    after_context = min(max(0, _query_int(after_context, 0)), 100)
    include_patterns = _split_grep_globs(include)
    exclude_patterns = _split_grep_globs(exclude)
    exclude_dir_patterns = _split_grep_globs(exclude_dir)
    safe_limit = _query_limited_int(limit, _GREP_DEFAULT_LIMIT, _GREP_MAX_LIMIT)
    safe_file_limit = _query_limited_int(max_files, _GREP_DEFAULT_FILE_LIMIT, _GREP_MAX_FILE_LIMIT)
    safe_byte_limit = _query_limited_int(max_bytes, _GREP_DEFAULT_BYTE_LIMIT, _GREP_MAX_BYTE_LIMIT)
    match_line = _grep_matcher(pattern, regex=regex, ignore_case=ignore_case)

    if isinstance(ref, str) and ref:
        raise HTTPException(
            status_code=410,
            detail={
                "code": "snapshot_ref_retired",
                "message": "This filesystem endpoint reads native repository HEAD.",
            },
        )

    full_path = _join_scope(scope["path"], rel_path)
    target = _ops_stat(ops, project_id, scope, rel_path)
    if target is None and rel_path:
        raise HTTPException(status_code=404, detail=f"Path not found: {rel_path}")
    target_type = target.type if target and target.type != "folder" else "folder"

    if not require_file_list:
        indexed_result = _try_indexed_grep_legacy_response(
            project_id=project_id,
            scope=scope,
            ops=ops,
            body=_GrepIndexedRequest(
                pattern=pattern,
                path=rel_path,
                regex=regex,
                ignore_case=ignore_case,
                invert_match=invert_match,
                only_matching=only_matching,
                before_context=before_context,
                after_context=after_context,
                limit=safe_limit,
                per_file_limit=per_file_limit,
                candidate_limit=safe_file_limit,
            ),
            rel_path=rel_path,
            version_path=full_path,
            target_type=target_type,
            include_hidden=include_hidden,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
            exclude_dir_patterns=exclude_dir_patterns,
            max_depth=max_depth,
            include_offsets=include_offsets,
            safe_file_limit=safe_file_limit,
            safe_byte_limit=safe_byte_limit,
        )
        if indexed_result is not None:
            return ApiResponse.success(data=indexed_result)

    truncated = False
    truncation_reason = ""

    def mark_truncated(reason: str) -> None:
        nonlocal truncated, truncation_reason
        truncated = True
        if not truncation_reason:
            truncation_reason = reason

    if target and target.type != "folder":
        candidates = [target]
    else:
        tree_entries = _filter_entries(
            _ops_list_tree(
                ops,
                project_id,
                scope,
                rel_path,
                max_depth=max_depth,
                include_size=False,
                max_entries=safe_file_limit + 1,
            ),
            scope,
            include_hidden=include_hidden,
        )
        if len(tree_entries) > safe_file_limit:
            mark_truncated("file_limit_exceeded")
            tree_entries = tree_entries[:safe_file_limit]
        candidates = [entry for entry in tree_entries if entry.type != "folder"]

    matches: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    content_cache: dict[str, bytes] = {}
    scanned_files = 0
    scanned_bytes = 0
    skipped = {
        "non_text": 0,
        "binary": 0,
        "too_large": 0,
        "read_errors": 0,
    }

    for entry in candidates:
        rel_entry_path = _relative_to_scope(entry.path, scope["path"])
        if _matches_exclude(rel_entry_path, scope.get("exclude") or []):
            continue
        if include_patterns and not _matches_any_grep_glob(rel_entry_path, include_patterns):
            continue
        if exclude_patterns and _matches_any_grep_glob(rel_entry_path, exclude_patterns):
            continue
        if exclude_dir_patterns and _matches_exclude_dir_glob(rel_entry_path, exclude_dir_patterns):
            continue
        if not _looks_text_entry(entry):
            skipped["non_text"] += 1
            continue

        content_hash = getattr(entry, "content_hash", None) or ""
        try:
            if content_hash and content_hash in content_cache:
                content = content_cache[content_hash]
            else:
                content = _ops_read_file(ops, project_id, scope, rel_entry_path)
                if content_hash:
                    content_cache[content_hash] = content
        except FileNotFoundError:
            skipped["read_errors"] += 1
            mark_truncated("read_error")
            continue
        except Exception:
            skipped["read_errors"] += 1
            mark_truncated("read_error")
            continue

        if _looks_binary(content):
            skipped["binary"] += 1
            continue
        if scanned_bytes + len(content) > safe_byte_limit:
            skipped["too_large"] += 1
            mark_truncated("byte_limit_exceeded")
            break

        scanned_files += 1
        scanned_bytes += len(content)
        text = _decode_grep_text(content)
        need_line_offsets = include_offsets or before_context > 0 or after_context > 0
        if need_line_offsets:
            raw_lines = text.splitlines(keepends=True)
            line_items: list[tuple[str, int | None]] = []
            byte_cursor = 0
            for raw_line in raw_lines:
                clean_line = raw_line.rstrip("\r\n")
                line_items.append((clean_line, byte_cursor))
                byte_cursor += len(raw_line.encode("utf-8"))
            if text and not raw_lines:
                line_items.append((text, 0))
        else:
            line_items = [(line, None) for line in text.splitlines()]

        file_match_count = 0
        for line_number, (line_text, line_byte_offset) in enumerate(line_items, start=1):
            spans = match_line(line_text)
            matched = bool(spans)
            if invert_match:
                matched = not matched
            if not matched:
                continue
            if only_matching and not invert_match and spans:
                output_spans = spans
            else:
                first_span = spans[0] if spans else (None, None)
                output_spans = [first_span]

            for match_start, match_end in output_spans:
                match_text = (
                    line_text[match_start:match_end]
                    if isinstance(match_start, int) and isinstance(match_end, int)
                    else ""
                )
                match_byte_offset = None
                if isinstance(line_byte_offset, int):
                    match_byte_offset = (
                        line_byte_offset + len(line_text[:match_start].encode("utf-8"))
                        if isinstance(match_start, int)
                        else line_byte_offset
                    )
                before_lines = []
                if before_context:
                    start_index = max(0, line_number - 1 - before_context)
                    for ctx_index in range(start_index, line_number - 1):
                        before_lines.append(
                            {
                                "line_number": ctx_index + 1,
                                "line_text": line_items[ctx_index][0],
                                "byte_offset": line_items[ctx_index][1],
                            }
                        )
                after_lines = []
                if after_context:
                    end_index = min(len(line_items), line_number + after_context)
                    for ctx_index in range(line_number, end_index):
                        after_lines.append(
                            {
                                "line_number": ctx_index + 1,
                                "line_text": line_items[ctx_index][0],
                                "byte_offset": line_items[ctx_index][1],
                            }
                        )
                matches.append(
                    {
                        "path": rel_entry_path,
                        "version_path": _join_scope(scope["path"], rel_entry_path),
                        "line_number": line_number,
                        "line_text": line_text,
                        "match_start": match_start,
                        "match_end": match_end,
                        "match_text": match_text,
                        "byte_offset": line_byte_offset,
                        "match_byte_offset": match_byte_offset,
                        "before_context": before_lines,
                        "after_context": after_lines,
                        "content_hash": content_hash or None,
                    }
                )
                file_match_count += 1
                if len(matches) >= safe_limit:
                    mark_truncated("result_limit_exceeded")
                    break
                if per_file_limit and file_match_count >= per_file_limit:
                    break
            if truncated and truncation_reason == "result_limit_exceeded":
                break
            if per_file_limit and file_match_count >= per_file_limit:
                break
        files.append(
            {
                "path": rel_entry_path,
                "version_path": _join_scope(scope["path"], rel_entry_path),
                "match_count": file_match_count,
                "content_hash": content_hash or None,
            }
        )
        if truncated and truncation_reason == "result_limit_exceeded":
            break

    scope_head_commit_id = ops.get_scope_head_commit_id(project_id, scope["path"])
    matched_files = len([item for item in files if item.get("match_count", 0) > 0])
    return ApiResponse.success(
        data={
            "pattern": pattern,
            "path": rel_path,
            "version_path": full_path,
            "scope": _scope_payload(scope),
            "target_type": target_type,
            "regex": regex,
            "ignore_case": ignore_case,
            "invert_match": invert_match,
            "only_matching": only_matching,
            "include_offsets": include_offsets,
            "require_file_list": require_file_list,
            "include": include_patterns,
            "exclude": exclude_patterns,
            "exclude_dir": exclude_dir_patterns,
            "limit": safe_limit,
            "max_count": per_file_limit,
            "before_context": before_context,
            "after_context": after_context,
            "max_files": safe_file_limit,
            "max_bytes": safe_byte_limit,
            "returned_count": len(matches),
            "matched_files": matched_files,
            "candidate_files": len(candidates),
            "scanned_files": scanned_files,
            "scanned_bytes": scanned_bytes,
            "skipped": skipped,
            "complete": not truncated,
            "truncated": truncated,
            "truncation_reason": truncation_reason,
            "files": files,
            "matches": matches,
            "head_commit_id": scope_head_commit_id,
            "scope_head_commit_id": scope_head_commit_id,
        }
    )


# ────────────────────────────────────────────────────────────────────
# Indexed grep (server-side fast path)
# ────────────────────────────────────────────────────────────────────
#
# Contract: ``docs/proposals/PUP-cloud-grep.md``.
#
# This endpoint queries the ``version_text_index`` GIN indexes
# (tsvector + pg_trgm) and recovers per-line offsets in the
# application layer. ``GET /grep`` is the canonical AP-FS grep
# operation and owns the index-readiness/fallback policy; this endpoint
# remains as the raw indexed contract for diagnostics and targeted tests.


class _GrepIndexedRequest(_BaseModel):
    """Body for ``POST /ap-fs/grep-indexed``.

    Flags mirror a useful subset of the legacy ``GET /grep``
    parameters; we drop the file-walk flags (``include`` /
    ``exclude_dir`` / ``max_depth`` / ``max_files`` / ``max_bytes``)
    because the index is the scan boundary now — anything in scope
    that the indexer hasn't stored is by definition not findable
    via this path and the caller falls back to the legacy endpoint.
    """

    pattern: str
    path: str = ""
    regex: bool = False
    ignore_case: bool = False
    word_match: bool = False
    invert_match: bool = False
    only_matching: bool = False
    before_context: int = 0
    after_context: int = 0
    limit: int = _GREP_DEFAULT_LIMIT
    per_file_limit: int = 0
    candidate_limit: int = 2000


def _run_grep_indexed_payload(
    *,
    project_id: str,
    scope: dict,
    ops: ProductOperationAdapter,
    body: _GrepIndexedRequest,
) -> dict[str, Any]:
    try:
        return run_indexed_grep_payload(
            project_id=project_id,
            scope_path=scope["path"],
            excludes=scope.get("exclude") or [],
            ops=ops,
            pattern=body.pattern,
            path=body.path,
            regex=body.regex,
            ignore_case=body.ignore_case,
            word_match=body.word_match,
            invert_match=body.invert_match,
            only_matching=body.only_matching,
            before_context=body.before_context,
            after_context=body.after_context,
            limit=body.limit,
            per_file_limit=body.per_file_limit,
            candidate_limit=body.candidate_limit,
            pattern_max_chars=_GREP_PATTERN_MAX_CHARS,
            max_limit=_GREP_MAX_LIMIT,
        )
    except IndexedGrepError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


def _try_indexed_grep_legacy_response(
    *,
    project_id: str,
    scope: dict,
    ops: ProductOperationAdapter,
    body: _GrepIndexedRequest,
    rel_path: str,
    version_path: str,
    target_type: str,
    include_hidden: bool,
    include_patterns: list[str],
    exclude_patterns: list[str],
    exclude_dir_patterns: list[str],
    max_depth: int,
    include_offsets: bool,
    safe_file_limit: int,
    safe_byte_limit: int,
) -> dict[str, Any] | None:
    try:
        envelope = _run_grep_indexed_payload(
            project_id=project_id,
            scope=scope,
            ops=ops,
            body=body,
        )
    except HTTPException:
        raise
    except Exception:
        return None

    if envelope.get("index_status") != "indexed":
        return None

    matches: list[dict[str, Any]] = []
    files_by_path: dict[str, dict[str, Any]] = {}
    skipped_by_filter = 0
    for hit in envelope.get("hits") or []:
        hit_path = str(hit.get("path") or "")
        hit_rel_path = _relative_to_scope(hit_path, scope["path"])
        if not include_hidden and _is_hidden_path(hit_rel_path):
            skipped_by_filter += 1
            continue
        if max_depth >= 0 and _grep_file_depth(hit_rel_path, rel_path) > max_depth:
            skipped_by_filter += 1
            continue
        if include_patterns and not _matches_any_grep_glob(hit_rel_path, include_patterns):
            skipped_by_filter += 1
            continue
        if exclude_patterns and _matches_any_grep_glob(hit_rel_path, exclude_patterns):
            skipped_by_filter += 1
            continue
        if exclude_dir_patterns and _matches_exclude_dir_glob(hit_rel_path, exclude_dir_patterns):
            skipped_by_filter += 1
            continue
        content_hash = hit.get("content_hash") or None
        line_text = str(hit.get("match") or "")
        col = max(0, int(hit.get("col") or 1) - 1)
        match_payload: dict[str, Any] = {
            "path": hit_rel_path,
            "version_path": _join_scope(scope["path"], hit_rel_path),
            "line_number": hit.get("line") or 0,
            "line_text": line_text,
            "match_start": col,
            "match_end": col + len(line_text),
            "match_text": line_text,
            "byte_offset": None,
            "match_byte_offset": col if include_offsets else None,
            "before_context": [{"line_text": text} for text in (hit.get("context_before") or [])],
            "after_context": [{"line_text": text} for text in (hit.get("context_after") or [])],
            "content_hash": content_hash,
        }
        matches.append(match_payload)
        file_payload = files_by_path.setdefault(
            hit_rel_path,
            {
                "path": hit_rel_path,
                "version_path": _join_scope(scope["path"], hit_rel_path),
                "match_count": 0,
                "content_hash": content_hash,
            },
        )
        file_payload["match_count"] += 1

    scope_head_commit_id = ops.get_scope_head_commit_id(project_id, scope["path"])
    truncated = bool(envelope.get("truncated"))
    truncation_reason = "indexed_truncated" if truncated else ""
    skipped = {
        "non_text": 0,
        "binary": 0,
        "too_large": 0,
        "read_errors": 0,
    }
    if skipped_by_filter:
        skipped["filtered"] = skipped_by_filter

    return {
        "pattern": body.pattern,
        "path": rel_path,
        "version_path": version_path,
        "scope": _scope_payload(scope),
        "target_type": target_type,
        "regex": body.regex,
        "ignore_case": body.ignore_case,
        "invert_match": body.invert_match,
        "only_matching": body.only_matching,
        "include_offsets": include_offsets,
        "include": include_patterns,
        "exclude": exclude_patterns,
        "exclude_dir": exclude_dir_patterns,
        "limit": envelope.get("limit") or body.limit,
        "max_count": envelope.get("per_file_limit") or body.per_file_limit,
        "before_context": body.before_context,
        "after_context": body.after_context,
        "max_files": safe_file_limit,
        "max_bytes": safe_byte_limit,
        "returned_count": len(matches),
        "matched_files": len(files_by_path),
        "candidate_files": envelope.get("candidates_examined") or 0,
        "scanned_files": 0,
        "scanned_bytes": 0,
        "skipped": skipped,
        "complete": not truncated,
        "truncated": truncated,
        "truncation_reason": truncation_reason,
        "files": list(files_by_path.values()),
        "matches": matches,
        "head_commit_id": scope_head_commit_id,
        "scope_head_commit_id": scope_head_commit_id,
        "index_status": envelope.get("index_status"),
        "index_freshness": envelope.get("index_freshness"),
        "search_backend": "indexed",
    }


@router.post("/grep-indexed", response_model=ApiResponse)
async def grep_indexed(
    body: _GrepIndexedRequest,
    x_access_key: str | None = Header(None, alias="X-Access-Key"),
    x_puppyone_user: str | None = Header(None, alias="X-PuppyOne-User"),
    x_puppy_client: str | None = Header(None, alias="X-Puppy-Client"),
    ops: ProductOperationAdapter = Depends(get_product_operation_adapter),
):
    """Indexed grep against the server's text index.

    Returns hits keyed by ``content_hash`` for diagnostics and raw
    indexed consumers. The response also carries ``index_status`` so
    callers can tell whether the index is authoritative:

      - ``indexed`` — index is at HEAD; results are authoritative.
      - ``stale``   — index is behind HEAD.
      - ``missing`` — no rows for this scope.
    """
    project_id, _auth, scope = await _resolve_auth(
        x_access_key,
        x_puppyone_user,
        x_puppy_client,
        command="grep",
    )
    ops = ops.for_grant(_auth["_runtime_grant"])
    return ApiResponse.success(
        data=_run_grep_indexed_payload(
            project_id=project_id,
            scope=scope,
            ops=ops,
            body=body,
        )
    )


# ────────────────────────────────────────────────────────────────────
# (Removed) ``POST /ap-fs/search`` — semantic / hybrid search via
# Turbopuffer + RRF fusion used to live here. Scoped out 2026-05-25:
# PuppyOne CLI is a cloud-disk operations surface (analogue: ``aws s3``),
# not a research tool. Semantic search belongs in the product UI,
# where the user has the context to interpret embedding scores.
#
# Kept:  ``version_text_index`` table, the post-commit indexer, and
#        ``POST /ap-fs/grep-indexed`` above. These power cloud-side
#        literal / regex grep at scale (pg_trgm + tsvector GIN).
# Gone:  ``_SearchRequest`` schema, ``_run_semantic_channel`` helper,
#        the ``/search`` endpoint, and the CLI ``puppyone fs search``
#        command.
# Unchanged: the underlying Turbopuffer pipeline (``SearchService``).
#            It's just no longer exposed through ap-fs.
# ────────────────────────────────────────────────────────────────────


@router.get("/cat", response_model=ApiResponse)
async def read_file(
    path: str = Query(..., description="File path relative to the access point scope"),
    structured: bool = Query(False, description="Parse JSON files into structured content"),
    x_access_key: str | None = Header(None, alias="X-Access-Key"),
    x_puppyone_user: str | None = Header(None, alias="X-PuppyOne-User"),
    x_puppy_client: str | None = Header(None, alias="X-Puppy-Client"),
    ops: ProductOperationAdapter = Depends(get_product_operation_adapter),
):
    project_id, _auth, scope = await _resolve_auth(
        x_access_key,
        x_puppyone_user,
        x_puppy_client,
        command="cat",
    )
    ops = ops.for_grant(_auth["_runtime_grant"])
    rel_path = _clean_relative(path)
    if not rel_path:
        raise HTTPException(status_code=400, detail="File path is required")
    _assert_not_excluded(rel_path, scope)

    full_path = _join_scope(scope["path"], rel_path)
    try:
        content = _ops_read_file(ops, project_id, scope, rel_path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"File not found: {rel_path}")

    from src.version_engine.read.tree_reader import detect_type

    node_type = detect_type(full_path)
    content_json = None
    content_text = content.decode("utf-8", errors="replace")
    if structured and node_type == "json":
        try:
            content_json = _json.loads(content_text)
            content_text = None
        except ValueError:
            pass

    return ApiResponse.success(
        data={
            "path": rel_path,
            "version_path": full_path,
            "scope": _scope_payload(scope),
            "type": node_type,
            "content": content_json,
            "content_text": content_text,
            "head_commit_id": ops.get_head_commit_id(project_id),
        }
    )


@router.get("/raw")
async def raw_file(
    path: str = Query(..., description="File path relative to the access point scope"),
    start: int = Query(0, ge=0, description="Start byte offset"),
    limit: int | None = Query(None, ge=0, description="Maximum bytes to return"),
    x_access_key: str | None = Header(None, alias="X-Access-Key"),
    x_puppyone_user: str | None = Header(None, alias="X-PuppyOne-User"),
    x_puppy_client: str | None = Header(None, alias="X-Puppy-Client"),
    ops: ProductOperationAdapter = Depends(get_product_operation_adapter),
):
    project_id, _auth, scope = await _resolve_auth(
        x_access_key,
        x_puppyone_user,
        x_puppy_client,
        command="download",
    )
    ops = ops.for_grant(_auth["_runtime_grant"])
    rel_path = _clean_relative(path)
    if not rel_path:
        raise HTTPException(status_code=400, detail="File path is required")
    _assert_not_excluded(rel_path, scope)
    start = _query_int(start, 0)
    limit = _query_optional_int(limit)

    try:
        if hasattr(ops, "read_file_range_in_scope") or hasattr(ops, "read_file_range"):
            blob = _ops_read_file_range(
                ops,
                project_id,
                scope,
                rel_path,
                start=start,
                limit=limit,
            )
            chunk = blob.content
            total = blob.total_size
        else:
            content = _ops_read_file(ops, project_id, scope, rel_path)
            total = len(content)
            end = total if limit is None else min(total, start + limit)
            chunk = content[start:end]
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"File not found: {rel_path}")

    headers = {
        "Content-Length": str(len(chunk)),
        "Accept-Ranges": "bytes",
        "X-Puppyone-Path": rel_path,
        "X-Puppyone-Size": str(total),
    }
    if start or limit is not None:
        if chunk:
            range_end = start + len(chunk) - 1
            headers["Content-Range"] = f"bytes {start}-{range_end}/{total}"
        else:
            headers["Content-Range"] = f"bytes */{total}"

    return Response(
        content=chunk,
        media_type="application/octet-stream",
        headers=headers,
    )


@router.post("/upload", response_model=ApiResponse)
async def upload_file(
    request: Request,
    path: str = Query(..., description="Destination path relative to the access point scope"),
    base_commit_id: str | None = Query(None, description="Expected current scope head"),
    message: str = Query("", description="Commit message"),
    x_access_key: str | None = Header(None, alias="X-Access-Key"),
    x_puppyone_user: str | None = Header(None, alias="X-PuppyOne-User"),
    x_puppy_client: str | None = Header(None, alias="X-Puppy-Client"),
    commands: VersionWriteCommandService = Depends(get_version_write_command_service),
):
    project_id, auth, scope = await _resolve_auth(
        x_access_key,
        x_puppyone_user,
        x_puppy_client,
        command="upload",
    )
    commands = VersionWriteCommandService(commands.ops.for_grant(auth["_runtime_grant"]))
    _ensure_writable(scope)
    rel_path = _clean_relative(path)
    if not rel_path:
        raise HTTPException(status_code=400, detail="File path is required")
    _assert_not_excluded(rel_path, scope)
    _assert_upload_policy(rel_path)

    per_file_max_bytes = await asyncio.to_thread(
        _upload_max_bytes_for_project,
        project_id,
    )
    request_headers = getattr(request, "headers", {})
    content_length_header = request_headers.get("content-length")
    if content_length_header:
        try:
            content_length = int(content_length_header)
        except ValueError:
            content_length = -1
        if content_length > per_file_max_bytes:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"File body is {content_length} bytes; per-file cap is "
                    f"{per_file_max_bytes} bytes."
                ),
            )

    content = await request.body()
    if len(content) > per_file_max_bytes:
        raise HTTPException(
            status_code=400,
            detail=(
                f"File body is {len(content)} bytes; per-file cap is {per_file_max_bytes} bytes."
            ),
        )
    full_path = _join_scope(scope["path"], rel_path)
    try:
        outcome = await commands.write_bytes(
            project_id,
            rel_path,
            content,
            actor=_operator(auth),
            scope=scope["path"],
            message=message or f"ap upload {rel_path}",
            base_commit_id=base_commit_id,
            defer_projection=True,
            source_channel="access_cli",
        )
        result = outcome.result
    except ConcurrentMutationError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return ApiResponse.success(
        data={
            "path": rel_path,
            "version_path": full_path,
            "scope": _scope_payload(scope),
            "commit_id": result.commit_id,
            "size_bytes": outcome.size_bytes,
        }
    )


@router.get("/stat", response_model=ApiResponse)
async def stat(
    path: str = Query("", description="Path relative to the access point scope"),
    x_access_key: str | None = Header(None, alias="X-Access-Key"),
    x_puppyone_user: str | None = Header(None, alias="X-PuppyOne-User"),
    x_puppy_client: str | None = Header(None, alias="X-Puppy-Client"),
    ops: ProductOperationAdapter = Depends(get_product_operation_adapter),
):
    project_id, _auth, scope = await _resolve_auth(
        x_access_key,
        x_puppyone_user,
        x_puppy_client,
        command="stat",
    )
    ops = ops.for_grant(_auth["_runtime_grant"])
    rel_path = _clean_relative(path)
    _assert_not_excluded(rel_path, scope)

    full_path = _join_scope(scope["path"], rel_path)
    scope_head_commit_id = ops.get_scope_head_commit_id(project_id, scope["path"])
    if not rel_path:
        return ApiResponse.success(
            data={
                "path": "",
                "version_path": full_path,
                "scope": _scope_payload(scope),
                "exists": True,
                "type": "folder",
                "name": _basename(scope["path"]) if scope["path"] else "",
                "content_hash": "",
                "size_bytes": 0,
                "mime_type": "inode/directory",
                "children_count": None,
                "integrity_status": "ok",
                "head_commit_id": scope_head_commit_id,
                "scope_head_commit_id": scope_head_commit_id,
                "metadata_source": "scope_state",
                "timestamp_source": "scope_state",
                "compatibility": {
                    "mode": "pseudo",
                    "uid": "pseudo",
                    "gid": "pseudo",
                    "device": "not_modeled",
                    "inode": "not_modeled",
                    "links": "pseudo",
                },
            }
        )

    head_commit_id = ops.get_head_commit_id(project_id)
    entry = _ops_stat(ops, project_id, scope, rel_path, include_size=True)
    if not entry:
        return ApiResponse.success(
            data={
                "path": rel_path,
                "version_path": full_path,
                "scope": _scope_payload(scope),
                "exists": False,
                "type": "",
                "name": "",
                "head_commit_id": head_commit_id,
                "scope_head_commit_id": scope_head_commit_id,
            }
        )
    _attach_timestamps(project_id, [entry], ops, extra_paths=[full_path])
    data = _entry_to_scoped_response(entry, scope)
    data["exists"] = True
    data["scope"] = _scope_payload(scope)
    data["head_commit_id"] = head_commit_id
    data["scope_head_commit_id"] = scope_head_commit_id
    data["metadata_source"] = "version_tree"
    data["timestamp_source"] = "version_history"
    data["compatibility"] = {
        "mode": "pseudo",
        "uid": "pseudo",
        "gid": "pseudo",
        "device": "not_modeled",
        "inode": "not_modeled",
        "links": "pseudo",
    }
    return ApiResponse.success(data=data)


@router.post("/write", response_model=ApiResponse)
async def write_file(
    body: WriteFileRequest,
    x_access_key: str | None = Header(None, alias="X-Access-Key"),
    x_puppyone_user: str | None = Header(None, alias="X-PuppyOne-User"),
    x_puppy_client: str | None = Header(None, alias="X-Puppy-Client"),
    commands: VersionWriteCommandService = Depends(get_version_write_command_service),
):
    project_id, auth, scope = await _resolve_auth(
        x_access_key,
        x_puppyone_user,
        x_puppy_client,
        command="write",
    )
    commands = VersionWriteCommandService(commands.ops.for_grant(auth["_runtime_grant"]))
    _ensure_writable(scope)
    rel_path = _clean_relative(body.path)
    if not rel_path:
        raise HTTPException(status_code=400, detail="File path is required")
    _assert_not_excluded(rel_path, scope)
    _assert_upload_policy(rel_path)

    try:
        outcome = await commands.write_file(
            project_id,
            rel_path,
            body.content,
            node_type=body.node_type,
            actor=_operator(auth),
            scope=scope["path"],
            message=body.message,
            default_message_prefix="ap write",
            base_commit_id=body.base_commit_id,
            defer_projection=True,
            source_channel="access_cli",
        )
        result = outcome.result
    except ConcurrentMutationError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    full_path = _join_scope(scope["path"], outcome.path)
    return ApiResponse.success(
        data={
            "path": outcome.path,
            "version_path": full_path,
            "scope": _scope_payload(scope),
            "commit_id": result.commit_id,
            "merged": False,
            "conflicts": 0,
        }
    )


@router.post("/mkdir", response_model=ApiResponse)
async def mkdir(
    body: MkdirRequest,
    x_access_key: str | None = Header(None, alias="X-Access-Key"),
    x_puppyone_user: str | None = Header(None, alias="X-PuppyOne-User"),
    x_puppy_client: str | None = Header(None, alias="X-Puppy-Client"),
    commands: VersionWriteCommandService = Depends(get_version_write_command_service),
):
    project_id, auth, scope = await _resolve_auth(
        x_access_key,
        x_puppyone_user,
        x_puppy_client,
        command="mkdir",
    )
    commands = VersionWriteCommandService(commands.ops.for_grant(auth["_runtime_grant"]))
    _ensure_writable(scope)
    rel_path = _clean_relative(body.path)
    if not rel_path:
        raise HTTPException(status_code=400, detail="Directory path is required")
    _assert_not_excluded(rel_path, scope)
    _assert_upload_policy(rel_path)

    full_path = _join_scope(scope["path"], rel_path)
    existing = _ops_stat(commands.ops, project_id, scope, rel_path)
    if existing is not None:
        if existing.type == "folder" and body.parents:
            return ApiResponse.success(
                data={
                    "path": rel_path,
                    "version_path": full_path,
                    "scope": _scope_payload(scope),
                    "commit_id": "",
                    "created": False,
                }
            )
        raise HTTPException(status_code=400, detail=f"File exists: {rel_path}")

    parent_rel = _dirname(rel_path)
    if parent_rel and not body.parents:
        parent = _ops_stat(commands.ops, project_id, scope, parent_rel)
        if parent is None:
            raise HTTPException(status_code=404, detail=f"No such file or directory: {parent_rel}")
        if parent.type != "folder":
            raise HTTPException(status_code=400, detail=f"Not a directory: {parent_rel}")

    try:
        outcome = await commands.mkdir(
            project_id,
            rel_path,
            actor=_operator(auth),
            scope=scope["path"],
            message=f"ap mkdir {rel_path}",
            base_commit_id=body.base_commit_id,
            defer_projection=True,
            source_channel="access_cli",
        )
        result = outcome.result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except ConcurrentMutationError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return ApiResponse.success(
        data={
            "path": rel_path,
            "version_path": full_path,
            "scope": _scope_payload(scope),
            "commit_id": result.commit_id,
        }
    )


@router.post("/touch", response_model=ApiResponse)
async def touch(
    body: TouchRequest,
    x_access_key: str | None = Header(None, alias="X-Access-Key"),
    x_puppyone_user: str | None = Header(None, alias="X-PuppyOne-User"),
    x_puppy_client: str | None = Header(None, alias="X-Puppy-Client"),
    commands: VersionWriteCommandService = Depends(get_version_write_command_service),
):
    project_id, auth, scope = await _resolve_auth(
        x_access_key,
        x_puppyone_user,
        x_puppy_client,
        command="touch",
    )
    commands = VersionWriteCommandService(commands.ops.for_grant(auth["_runtime_grant"]))
    _ensure_writable(scope)
    rel_paths = [_clean_relative(p) for p in (body.paths or [body.path])]
    rel_paths = [p for p in rel_paths if p]
    if not rel_paths:
        raise HTTPException(status_code=400, detail="File path is required")

    existing_files: list[str] = []
    missing_files: list[str] = []
    for rel_path in rel_paths:
        _assert_not_excluded(rel_path, scope)
        _assert_upload_policy(rel_path)
        existing = _ops_stat(commands.ops, project_id, scope, rel_path)
        if existing is not None:
            if existing.type == "folder":
                raise HTTPException(status_code=400, detail=f"Is a directory: {rel_path}")
            existing_files.append(rel_path)
        else:
            missing_files.append(rel_path)

    results_by_path: dict[str, dict] = {}
    base_used = False
    if existing_files:
        try:
            outcome = await commands.touch(
                project_id,
                existing_files,
                actor=_operator(auth),
                scope=scope["path"],
                message=(
                    f"ap touch {existing_files[0]}"
                    if len(existing_files) == 1
                    else f"ap touch {len(existing_files)} files"
                ),
                base_commit_id=body.base_commit_id,
                defer_projection=True,
                source_channel="access_cli",
            )
            result = outcome.result
            base_used = True
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except ConcurrentMutationError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        for rel_path in existing_files:
            results_by_path[rel_path] = {
                "path": rel_path,
                "version_path": _join_scope(scope["path"], rel_path),
                "commit_id": result.commit_id,
                "created": False,
                "touched": True,
            }

    for index, rel_path in enumerate(missing_files):
        full_path = _join_scope(scope["path"], rel_path)
        try:
            outcome = await commands.write_bytes(
                project_id,
                rel_path,
                b"",
                actor=_operator(auth),
                scope=scope["path"],
                message=f"ap touch {rel_path}",
                base_commit_id=body.base_commit_id if index == 0 and not base_used else None,
                defer_projection=True,
                source_channel="access_cli",
            )
            result = outcome.result
        except ConcurrentMutationError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        results_by_path[rel_path] = {
            "path": rel_path,
            "version_path": full_path,
            "commit_id": result.commit_id,
            "created": True,
            "touched": True,
        }

    results = [results_by_path[p] for p in rel_paths]

    data = {
        "paths": rel_paths,
        "results": results,
        "scope": _scope_payload(scope),
    }
    if len(results) == 1:
        data.update(results[0])
    return ApiResponse.success(data=data)


@router.post("/mv", response_model=ApiResponse)
async def move(
    body: MoveRequest,
    x_access_key: str | None = Header(None, alias="X-Access-Key"),
    x_puppyone_user: str | None = Header(None, alias="X-PuppyOne-User"),
    x_puppy_client: str | None = Header(None, alias="X-Puppy-Client"),
    commands: VersionWriteCommandService = Depends(get_version_write_command_service),
):
    project_id, auth, scope = await _resolve_auth(
        x_access_key,
        x_puppyone_user,
        x_puppy_client,
        command="mv",
    )
    commands = VersionWriteCommandService(commands.ops.for_grant(auth["_runtime_grant"]))
    _ensure_writable(scope)
    old_rel = _clean_relative(body.old_path)
    new_rel = _clean_relative(body.new_path)
    if not old_rel:
        raise HTTPException(status_code=400, detail="Both old_path and new_path are required")
    _assert_not_excluded(old_rel, scope)
    _assert_not_excluded(new_rel, scope)
    # PUP-3: block writes landing inside a blocklisted path. The source
    # side is intentionally permissive so users can move/copy legacy
    # content OUT of a blocked directory while cleaning up.
    _assert_upload_policy(new_rel)

    old_full = _join_scope(scope["path"], old_rel)
    old_entry = _ops_stat(commands.ops, project_id, scope, old_rel)
    if old_entry is None:
        raise _fs_error(404, "NOT_FOUND", f"Path not found: {old_rel}", path=old_rel)

    new_rel, new_full, new_entry = _resolve_copy_move_destination(
        project_id,
        scope,
        commands.ops,
        old_rel,
        new_rel,
        target_directory=body.target_directory,
        no_target_directory=body.no_target_directory,
    )
    if _is_descendant_destination(old_rel, new_rel):
        raise _fs_error(
            400,
            "INVALID_MOVE_DESTINATION",
            f"Cannot move {old_rel} into its own subtree: {new_rel}",
            path=new_rel,
        )

    if new_entry is not None:
        if body.no_clobber:
            return ApiResponse.success(
                data={
                    "old_path": old_rel,
                    "new_path": new_rel,
                    "old_version_path": old_full,
                    "new_version_path": new_full,
                    "scope": _scope_payload(scope),
                    "commit_id": "",
                    "skipped": True,
                    "reason": "destination exists",
                }
            )
        if body.no_target_directory and new_entry.type == "folder":
            raise _fs_error(400, "IS_DIRECTORY", f"Is a directory: {new_rel}", path=new_rel)
        if new_entry.type == "folder" and old_entry.type != "folder":
            raise _fs_error(400, "IS_DIRECTORY", f"Is a directory: {new_rel}", path=new_rel)
        if new_entry.type != "folder" and old_entry.type == "folder":
            raise _fs_error(400, "NOT_A_DIRECTORY", f"Not a directory: {new_rel}", path=new_rel)

    try:
        outcome = await commands.move(
            project_id,
            old_rel,
            new_rel,
            actor=_operator(auth),
            scope=scope["path"],
            message=body.message or f"ap move {old_rel} -> {new_rel}",
            base_commit_id=body.base_commit_id,
            defer_projection=True,
            source_channel="access_cli",
        )
        result = outcome.result
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ConcurrentMutationError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    return ApiResponse.success(
        data={
            "old_path": old_rel,
            "new_path": new_rel,
            "old_version_path": old_full,
            "new_version_path": new_full,
            "scope": _scope_payload(scope),
            "commit_id": result.commit_id,
            "skipped": False,
        }
    )


@router.post("/cp", response_model=ApiResponse)
async def copy(
    body: CopyRequest,
    x_access_key: str | None = Header(None, alias="X-Access-Key"),
    x_puppyone_user: str | None = Header(None, alias="X-PuppyOne-User"),
    x_puppy_client: str | None = Header(None, alias="X-Puppy-Client"),
    commands: VersionWriteCommandService = Depends(get_version_write_command_service),
):
    project_id, auth, scope = await _resolve_auth(
        x_access_key,
        x_puppyone_user,
        x_puppy_client,
        command="cp",
    )
    commands = VersionWriteCommandService(commands.ops.for_grant(auth["_runtime_grant"]))
    _ensure_writable(scope)
    old_rel = _clean_relative(body.old_path)
    new_rel = _clean_relative(body.new_path)
    if not old_rel:
        raise HTTPException(status_code=400, detail="Both old_path and new_path are required")
    _assert_not_excluded(old_rel, scope)
    _assert_not_excluded(new_rel, scope)
    # PUP-3: block writes landing inside a blocklisted path. The source
    # side is intentionally permissive so users can move/copy legacy
    # content OUT of a blocked directory while cleaning up.
    _assert_upload_policy(new_rel)

    old_full = _join_scope(scope["path"], old_rel)
    old_entry = _ops_stat(commands.ops, project_id, scope, old_rel)
    if old_entry is None:
        raise _fs_error(404, "NOT_FOUND", f"Path not found: {old_rel}", path=old_rel)
    if old_entry.type == "folder" and not body.recursive:
        raise _fs_error(400, "IS_DIRECTORY", f"Is a directory: {old_rel}", path=old_rel)

    new_rel, new_full, new_entry = _resolve_copy_move_destination(
        project_id,
        scope,
        commands.ops,
        old_rel,
        new_rel,
        target_directory=body.target_directory,
        no_target_directory=body.no_target_directory,
    )

    if new_entry is not None:
        if body.no_clobber:
            return ApiResponse.success(
                data={
                    "old_path": old_rel,
                    "new_path": new_rel,
                    "old_version_path": old_full,
                    "new_version_path": new_full,
                    "scope": _scope_payload(scope),
                    "commit_id": "",
                    "skipped": True,
                    "reason": "destination exists",
                }
            )
        if body.no_target_directory and new_entry.type == "folder":
            raise _fs_error(400, "IS_DIRECTORY", f"Is a directory: {new_rel}", path=new_rel)
        if new_entry.type == "folder" and old_entry.type != "folder":
            raise _fs_error(400, "IS_DIRECTORY", f"Is a directory: {new_rel}", path=new_rel)
        if new_entry.type != "folder" and old_entry.type == "folder":
            raise _fs_error(400, "NOT_A_DIRECTORY", f"Not a directory: {new_rel}", path=new_rel)

    try:
        outcome = await commands.copy(
            project_id,
            old_rel,
            new_rel,
            actor=_operator(auth),
            scope=scope["path"],
            message=body.message or f"ap copy {old_rel} -> {new_rel}",
            base_commit_id=body.base_commit_id,
            defer_projection=True,
            source_channel="access_cli",
        )
        result = outcome.result
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ConcurrentMutationError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    return ApiResponse.success(
        data={
            "old_path": old_rel,
            "new_path": new_rel,
            "old_version_path": old_full,
            "new_version_path": new_full,
            "scope": _scope_payload(scope),
            "commit_id": result.commit_id,
            "skipped": False,
        }
    )


@router.post("/rmdir", response_model=ApiResponse)
async def rmdir(
    body: RmdirRequest,
    x_access_key: str | None = Header(None, alias="X-Access-Key"),
    x_puppyone_user: str | None = Header(None, alias="X-PuppyOne-User"),
    x_puppy_client: str | None = Header(None, alias="X-Puppy-Client"),
    commands: VersionWriteCommandService = Depends(get_version_write_command_service),
):
    project_id, auth, scope = await _resolve_auth(
        x_access_key,
        x_puppyone_user,
        x_puppy_client,
        command="rmdir",
    )
    commands = VersionWriteCommandService(commands.ops.for_grant(auth["_runtime_grant"]))
    _ensure_writable(scope)
    rel_paths = [_clean_relative(p) for p in (body.paths or [body.path])]
    rel_paths = [p for p in rel_paths if p]
    if not rel_paths:
        raise HTTPException(status_code=400, detail="Cannot remove the access point root")

    remove_paths: list[str] = []
    seen: set[str] = set()
    for rel_path in rel_paths:
        _assert_not_excluded(rel_path, scope)
        for candidate in _rmdir_chain(
            project_id,
            rel_path,
            scope,
            commands.ops,
            parents=body.parents,
        ):
            if candidate not in seen:
                remove_paths.append(candidate)
                seen.add(candidate)

    try:
        outcome = await commands.delete(
            project_id,
            remove_paths,
            actor=_operator(auth),
            scope=scope["path"],
            message=(
                f"ap rmdir {remove_paths[0]}"
                if len(remove_paths) == 1
                else f"ap rmdir {len(remove_paths)} directories"
            ),
            base_commit_id=body.base_commit_id,
            defer_projection=True,
            source_channel="access_cli",
        )
        result = outcome.result
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except ConcurrentMutationError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    data = {
        "paths": rel_paths,
        "removed_paths": remove_paths,
        "version_paths": [_join_scope(scope["path"], p) for p in remove_paths],
        "scope": _scope_payload(scope),
        "commit_id": result.commit_id,
        "removed": True,
    }
    if len(rel_paths) == 1:
        data["path"] = rel_paths[0]
        data["version_path"] = _join_scope(scope["path"], rel_paths[0])
    return ApiResponse.success(data=data)


@router.post("/rm", response_model=ApiResponse)
async def remove(
    body: RemoveRequest,
    x_access_key: str | None = Header(None, alias="X-Access-Key"),
    x_puppyone_user: str | None = Header(None, alias="X-PuppyOne-User"),
    x_puppy_client: str | None = Header(None, alias="X-Puppy-Client"),
    commands: VersionWriteCommandService = Depends(get_version_write_command_service),
):
    project_id, auth, scope = await _resolve_auth(
        x_access_key,
        x_puppyone_user,
        x_puppy_client,
        command="rm",
    )
    commands = VersionWriteCommandService(commands.ops.for_grant(auth["_runtime_grant"]))
    _ensure_writable(scope)
    rel_paths = [_clean_relative(p) for p in (body.paths or [body.path])]
    rel_paths = [p for p in rel_paths if p]
    if not rel_paths:
        raise HTTPException(status_code=400, detail="Cannot remove the access point root")

    existing_paths: list[str] = []
    missing_paths: list[str] = []
    for rel_path in rel_paths:
        _assert_not_excluded(rel_path, scope)
        entry = _ops_stat(commands.ops, project_id, scope, rel_path)
        if entry is None:
            missing_paths.append(rel_path)
            continue
        if entry.type == "folder" and not body.recursive:
            raise HTTPException(status_code=400, detail=f"Is a directory: {rel_path}")
        existing_paths.append(rel_path)

    if missing_paths and not body.force:
        raise HTTPException(status_code=404, detail=f"Path not found: {missing_paths[0]}")
    if not existing_paths:
        data = {
            "paths": rel_paths,
            "version_paths": [_join_scope(scope["path"], p) for p in rel_paths],
            "scope": _scope_payload(scope),
            "commit_id": "",
            "removed": False,
        }
        if len(rel_paths) == 1:
            data["path"] = rel_paths[0]
            data["version_path"] = _join_scope(scope["path"], rel_paths[0])
        return ApiResponse.success(data=data)

    try:
        outcome = await commands.delete(
            project_id,
            existing_paths,
            actor=_operator(auth),
            scope=scope["path"],
            message=(
                f"ap delete {existing_paths[0]}"
                if len(existing_paths) == 1
                else f"ap delete {len(existing_paths)} paths"
            ),
            base_commit_id=body.base_commit_id,
            defer_projection=True,
            source_channel="access_cli",
        )
        result = outcome.result
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except ConcurrentMutationError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    data = {
        "paths": existing_paths,
        "version_paths": [_join_scope(scope["path"], p) for p in existing_paths],
        "scope": _scope_payload(scope),
        "commit_id": result.commit_id,
        "removed": True,
    }
    if len(existing_paths) == 1:
        data["path"] = existing_paths[0]
        data["version_path"] = _join_scope(scope["path"], existing_paths[0])
    return ApiResponse.success(data=data)


# ── H2/H4/H5: fs_path_index-backed find + admin rebuild ──────────────


@router.get("/find", response_model=ApiResponse)
async def find_index(
    name: str = Query("", description="fnmatch-style glob over the basename"),
    iname: str = Query("", description="case-insensitive fnmatch-style glob over the basename"),
    path: str = Query("", description="Subpath under the scope to narrow the search"),
    path_glob: str = Query("", description="fnmatch-style glob over the scope-relative path"),
    mime: str = Query("", description="Optional mime-type prefix filter (e.g. 'text/')"),
    type_: str = Query(
        "any",
        alias="type",
        description="'file', 'folder', or 'any'",
    ),
    conditions: str = Query("", description="JSON array of find predicates from CLI/MCP surfaces"),
    mindepth: int = Query(0, description="Minimum result depth relative to the search root"),
    max_depth: int = Query(
        -1, description="Maximum result depth relative to the search root, -1 = unlimited"
    ),
    include_hidden: bool = Query(True, description="Include entries whose names begin with '.'"),
    limit: int = Query(1000, ge=1, le=50000),
    x_access_key: str | None = Header(None, alias="X-Access-Key"),
    x_puppyone_user: str | None = Header(None, alias="X-PuppyOne-User"),
    x_puppy_client: str | None = Header(None, alias="X-Puppy-Client"),
    ops: ProductOperationAdapter = Depends(get_product_operation_adapter),
):
    """Canonical scoped find.

    The route owns traversal, filtering, truncation, and future
    index/fallback policy. CLI and MCP clients should map their surface
    syntax into this request shape rather than filtering locally.
    """

    project_id, auth, scope = await _resolve_auth(
        x_access_key,
        x_puppyone_user,
        x_puppy_client,
        command="find",
    )
    ops = ops.for_grant(auth["_runtime_grant"])
    name_value = name if isinstance(name, str) else ""
    iname_value = iname if isinstance(iname, str) else ""
    path_glob_value = path_glob if isinstance(path_glob, str) else ""
    mime_value = mime if isinstance(mime, str) else ""
    type_value = type_ if isinstance(type_, str) else "any"
    conditions_value = conditions if isinstance(conditions, str) else ""
    if mime_value:
        # The previous route exposed a file-index-only mime filter. It is not
        # part of the canonical FS find contract; keep the error explicit so
        # callers do not assume MCP/CLI parity for it.
        raise HTTPException(
            status_code=400, detail="mime filtering is not supported by canonical find"
        )
    service = ScopedFsService(ops=ops, commands=None)  # type: ignore[arg-type]
    ctx = _scoped_fs_context(
        access_key=x_access_key,
        project_id=project_id,
        auth=auth,
        scope=scope,
    )
    try:
        result = await service.find(
            ctx,
            path=_clean_relative(path),
            name=name_value,
            iname=iname_value,
            path_glob=path_glob_value,
            type=type_value,
            conditions=conditions_value,
            mindepth=mindepth,
            max_depth=max_depth,
            limit=limit,
            include_hidden=_query_bool(include_hidden, True),
        )
    except ScopedFsError as exc:
        raise _scoped_fs_error(exc) from exc
    result["scope"] = _scope_payload(scope)
    result["head_commit_id"] = ops.get_head_commit_id(project_id)
    return ApiResponse.success(data=result)


@router.post("/admin/object-integrity", response_model=ApiResponse)
async def retired_object_repair():
    raise HTTPException(status_code=410, detail="Physical object repair is an operator task")


@router.post("/admin/fs-index/rebuild", response_model=ApiResponse)
@router.post("/admin/text-index/rebuild", response_model=ApiResponse)
async def rebuild_native_index():
    raise HTTPException(
        status_code=410, detail="Native indexes rebuild automatically from ref changes"
    )
