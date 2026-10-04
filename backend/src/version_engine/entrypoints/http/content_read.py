"""Content Read API — ls, cat, stat, tree, raw, download."""

from __future__ import annotations

import base64
import binascii
import json as _json
import re
from contextlib import contextmanager
from typing import Annotated
from urllib.parse import quote
from zipfile import ZIP_DEFLATED

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from zipstream import ZipStream

from src.common_schemas import ApiResponse
from src.infra.supabase.client import SupabaseClient
from src.version_engine.bootstrap.dependencies import get_product_operation_adapter
from src.version_engine.domain.errors import (
    NativeObjectNotFoundError,
    ObjectNotFoundError,
    PathNotFoundError,
    VersionReadError,
)
from src.version_engine.entrypoints.http.content_helpers import (
    display_git_path, ensure_project_access, entry_to_response, git_path_b64,
)
from src.version_engine.entrypoints.http.download_token import (
    DEFAULT_TTL_SECONDS,
    DownloadTokenError,
    issue_token,
    verify_token,
)
from src.version_engine.entrypoints.http.schemas import (
    ListDirResponse,
    ReadFileResponse,
    StatResponse,
    TreeResponse,
)
from src.version_engine.admission.validation import MAX_PATH_LENGTH, validate_path
from src.version_engine.adapters.product.operation_adapter import ProductOperationAdapter
from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser
from src.platform.authorization.dependencies import get_authorization_service
from src.platform.authorization.service import AuthorizationService

read_router = APIRouter()
_LEGACY_ROOT_ID_RE = re.compile(r"^[0-9a-f]{16}$")


def _read_request_path(path: str, encoded: str | None) -> str:
    if encoded is None:
        return validate_path(path)
    if path:
        raise HTTPException(400, "Provide path or path_bytes_b64, not both")
    # Preserve the text API's character ceiling (UTF-8 uses at most four
    # bytes per character) while bounding decoding before allocating raw bytes.
    if len(encoded) > ((MAX_PATH_LENGTH * 4 + 2) // 3) * 4:
        raise HTTPException(400, "Invalid byte path")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(400, "Invalid byte path") from exc
    decoded = raw.decode("utf-8", "surrogateescape")
    if len(decoded) > MAX_PATH_LENGTH or b"\x00" in raw or (
        raw and any(part in {b"", b".", b".."} for part in raw.split(b"/"))
    ):
        raise HTTPException(400, "Invalid byte path")
    return decoded


@contextmanager
def _product_read(ops, project_id, grant, *, byte_path=False):
    try:
        with ops.open_read(project_id, grant) as read:
            revision = read.get_read_revision(project_id)
            if byte_path and revision is None:
                raise HTTPException(400, "Byte paths require native repository authority")
            yield read, revision
    except (HTTPException, ObjectNotFoundError, FileNotFoundError, PathNotFoundError, VersionReadError):
        raise
    except PermissionError as exc:
        raise HTTPException(403, "Repository action denied") from exc
    except Exception as exc:
        # A grant and a pin establish provenance, not ongoing authority. SQL
        # may deny an actor after the PDP check (including after lock waits).
        from postgrest.exceptions import APIError
        if isinstance(exc, APIError) and exc.message == "repository_action_denied":
            raise HTTPException(403, "Repository action denied") from exc
        raise HTTPException(503, "Repository read is temporarily unavailable") from exc


def _is_marked_irrecoverable(project_id: str) -> bool:
    """Classify a known root-loss incident without exposing it to browser roles."""

    try:
        response = (
            SupabaseClient().client
            .table("version_project_root_integrity_incidents")
            .select("status")
            .eq("project_id", project_id)
            .eq("status", "irrecoverable")
            .limit(1)
            .execute()
        )
        return bool(response.data)
    except Exception:
        # Incident classification must never mask the original integrity
        # failure if a rollout has not yet applied the incident migration.
        return False


def _has_unsupported_legacy_root(project_id: str) -> bool:
    """Return true only for the retired 16-hex root identifier format.

    This is consulted after an object read already failed.  It makes the
    terminal state explicit without reintroducing legacy-object compatibility
    or weakening the canonical 40-hex incident table constraint.
    """

    try:
        response = (
            SupabaseClient().client
            .table("projects")
            .select("version_root_hash")
            .eq("id", project_id)
            .limit(1)
            .execute()
        )
        rows = response.data or []
        root_hash = str(rows[0].get("version_root_hash") or "") if rows else ""
        return bool(_LEGACY_ROOT_ID_RE.fullmatch(root_hash))
    except Exception:
        return False


def _raise_storage_integrity_error(
    project_id: str, path: str, exc: ObjectNotFoundError,
) -> None:
    path = display_git_path(path)
    target = path or "project root"
    native = isinstance(exc, NativeObjectNotFoundError)
    if not native and (_is_marked_irrecoverable(project_id) or _has_unsupported_legacy_root(project_id)):
        raise HTTPException(
            status_code=410,
            detail={
                "code": "VERSION_STORAGE_IRRECOVERABLE",
                "message": (
                    "This project's historical version content is unavailable and "
                    "cannot be recovered from the configured storage."
                ),
                "path": path,
            },
        ) from exc
    raise HTTPException(
        status_code=500,
        detail={
            "code": "VERSION_STORAGE_INTEGRITY_ERROR",
            "message": (
                f"Canonical repository bytes are unavailable while reading {target}."
                if native else
                "Version storage integrity error while reading "
                f"{target}. Run the version object namespace migration/repair."
            ),
            "path": path,
        },
    ) from exc


def _raise_directory_not_found(path: str, exc: PathNotFoundError) -> None:
    path = display_git_path(path)
    target = path or "project root"
    raise HTTPException(
        status_code=404,
        detail={
            "code": "DIRECTORY_NOT_FOUND",
            "message": f"Directory not found: {target}",
            "path": path,
        },
    ) from exc


def _raise_version_read_error(path: str, exc: VersionReadError) -> None:
    path = display_git_path(path)
    target = path or "project root"
    raise HTTPException(
        status_code=502,
        detail={
            "code": "VERSION_READ_UNAVAILABLE",
            "message": f"Version tree temporarily unavailable while reading {target}.",
            "path": path,
        },
    ) from exc


@read_router.get(
    "/{project_id}/ls",
    response_model=ApiResponse[ListDirResponse],
    summary="List directory contents",
)
def list_dir(
    project_id: str,
    path: str = Query("", description="Directory path, empty = root directory"),
    path_bytes_b64: Annotated[str | None, Query()] = None,
    ops: ProductOperationAdapter = Depends(get_product_operation_adapter),
    authorization: AuthorizationService = Depends(get_authorization_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    grant = ensure_project_access(authorization, current_user, project_id)
    clean_path = _read_request_path(path, path_bytes_b64)

    try:
        with _product_read(ops, project_id, grant, byte_path=path_bytes_b64 is not None) as (read, revision):
            entries = read.list_dir(project_id, clean_path)
            head_commit_id = read.get_head_commit_id(project_id)
    except ObjectNotFoundError as exc:
        _raise_storage_integrity_error(project_id, clean_path, exc)
    except PathNotFoundError as exc:
        _raise_directory_not_found(clean_path, exc)
    except VersionReadError as exc:
        _raise_version_read_error(clean_path, exc)
    return ApiResponse.success(data=ListDirResponse(
        path=display_git_path(clean_path),
        path_bytes_b64=git_path_b64(clean_path) if revision else None,
        repository_revision=revision,
        entries=[entry_to_response(e) for e in entries],
        head_commit_id=head_commit_id,
    ))


@read_router.get(
    "/{project_id}/cat",
    response_model=ApiResponse[ReadFileResponse],
    summary="Read file contents",
)
def read_file(
    project_id: str,
    path: str = Query("", description="File path, or provide path_bytes_b64"),
    path_bytes_b64: Annotated[str | None, Query()] = None,
    ops: ProductOperationAdapter = Depends(get_product_operation_adapter),
    authorization: AuthorizationService = Depends(get_authorization_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    grant = ensure_project_access(authorization, current_user, project_id)
    clean_path = _read_request_path(path, path_bytes_b64)

    try:
        with _product_read(ops, project_id, grant, byte_path=path_bytes_b64 is not None) as (read, revision):
            content = read.read_file(project_id, clean_path)
            head_commit_id = read.get_head_commit_id(project_id)
    except ObjectNotFoundError as exc:
        _raise_storage_integrity_error(project_id, clean_path, exc)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"File not found: {display_git_path(clean_path)}")

    from src.version_engine.read.tree_reader import detect_type
    node_type = detect_type(clean_path)

    content_json = None
    content_text = None

    decoded_text = content.decode("utf-8", errors="replace")

    if node_type == "json":
        content_text = decoded_text
        try:
            content_json = _json.loads(decoded_text)
        except ValueError:
            content_json = None
    else:
        content_text = decoded_text

    return ApiResponse.success(data=ReadFileResponse(
        path=display_git_path(clean_path),
        path_bytes_b64=git_path_b64(clean_path) if revision else None,
        repository_revision=revision,
        type=node_type,
        content=content_json,
        content_text=content_text,
        content_hash=None,
        head_commit_id=head_commit_id,
    ))


@read_router.get(
    "/{project_id}/raw",
    summary="Serve raw file bytes with correct Content-Type",
)
def raw_file(
    project_id: str,
    request: Request,
    path: str = Query("", description="File path, or provide path_bytes_b64"),
    path_bytes_b64: Annotated[str | None, Query()] = None,
    ops: ProductOperationAdapter = Depends(get_product_operation_adapter),
    authorization: AuthorizationService = Depends(get_authorization_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    grant = ensure_project_access(authorization, current_user, project_id)
    clean_path = _read_request_path(path, path_bytes_b64)

    try:
        with _product_read(ops, project_id, grant, byte_path=path_bytes_b64 is not None) as (read, _revision):
            content = read.read_file(project_id, clean_path)
            entry = read.stat(project_id, clean_path)
    except ObjectNotFoundError as exc:
        _raise_storage_integrity_error(project_id, clean_path, exc)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"File not found: {display_git_path(clean_path)}")
    from src.version_engine.read.tree_reader import detect_mime
    mime = detect_mime(clean_path) if entry else "application/octet-stream"

    filename = clean_path.rsplit("/", 1)[-1] if "/" in clean_path else clean_path
    return _serve_file_bytes(
        request=request,
        content=content,
        media_type=mime,
        filename=filename,
        disposition="inline",
        cache_control="private, max-age=3600",
    )


def _content_disposition_inline(filename: str) -> str:
    """Build an inline Content-Disposition that supports UTF-8 names."""
    return _content_disposition(filename, "inline")


def _content_disposition(filename: str, disposition: str) -> str:
    display = display_git_path(filename)
    ascii_name = display.encode("ascii", errors="replace").decode("ascii")
    fallback = "".join(c if 32 <= ord(c) < 127 and c not in {'"', "\\"} else "_" for c in ascii_name)
    encoded = quote(display, safe="")
    return f'{disposition}; filename="{fallback}"; filename*=UTF-8\'\'{encoded}'


def _parse_byte_range(range_header: str, total: int) -> tuple[int, int] | None:
    """Parse a single HTTP bytes range.

    Returns inclusive `(start, end)` or None for invalid/unsupported
    ranges. Multi-range responses are intentionally unsupported:
    browsers only need single-range media seeks for audio/video.
    """
    if not range_header.startswith("bytes=") or "," in range_header:
        return None

    spec = range_header.removeprefix("bytes=").strip()
    if "-" not in spec:
        return None

    start_raw, end_raw = spec.split("-", 1)
    try:
        if start_raw == "":
            suffix_len = int(end_raw)
            if suffix_len <= 0:
                return None
            start = max(total - suffix_len, 0)
            end = total - 1
        else:
            start = int(start_raw)
            end = int(end_raw) if end_raw else total - 1
            if start < 0 or end < start:
                return None
            end = min(end, total - 1)
    except ValueError:
        return None

    if total <= 0 or start >= total:
        return None
    return start, end


def _serve_file_bytes(
    *,
    request: Request,
    content: bytes,
    media_type: str,
    filename: str,
    disposition: str,
    cache_control: str,
) -> Response:
    """Serve bytes with optional single-range support.

    This makes signed inline media URLs work with native browser
    audio/video controls. The current version object-store read still
    materializes the blob before slicing; the browser no longer has
    to `fetch -> blob` the whole file before playback starts, and it
    can issue normal seek/range requests.
    """
    total = len(content)
    if disposition == "attachment":
        content_disposition = _content_disposition_attachment(filename)
    else:
        content_disposition = _content_disposition_inline(filename)

    base_headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": cache_control,
        "Content-Disposition": content_disposition,
    }

    range_header = request.headers.get("range")
    if range_header:
        parsed = _parse_byte_range(range_header, total)
        if parsed is None:
            return Response(
                status_code=416,
                headers={
                    **base_headers,
                    "Content-Range": f"bytes */{total}",
                },
            )

        start, end = parsed
        body = content[start : end + 1]
        return Response(
            content=body,
            status_code=206,
            media_type=media_type,
            headers={
                **base_headers,
                "Content-Length": str(len(body)),
                "Content-Range": f"bytes {start}-{end}/{total}",
            },
        )

    return Response(
        content=content,
        media_type=media_type,
        headers={
            **base_headers,
            "Content-Length": str(total),
        },
    )


def _content_disposition_attachment(filename: str) -> str:
    """Build a `Content-Disposition: attachment` header value that handles
    non-ASCII filenames per RFC 5987 (so e.g. Chinese folder names download
    with the right name across browsers)."""
    return _content_disposition(filename, "attachment")


# ─────────────────────────────────────────────────────────────────────────
# Download — two-step: POST /sign (auth via Bearer) → GET /download (auth via token)
#
# Why split: a plain `<a href download>` opens as a top-level navigation
# and the browser cannot attach `Authorization: Bearer ...`. Without
# token-in-URL, the only alternatives are:
#   1) `fetch → blob → URL.createObjectURL → click <a>` — kills the
#      browser's native download manager (no progress bar, no pause/cancel,
#      whole zip held in tab memory).
#   2) cookie auth — would require introducing a separate auth scheme just
#      for downloads.
# A signed URL with a 5-min HMAC token gives us streaming + native
# browser download without breaking the bearer-token auth model.
# ─────────────────────────────────────────────────────────────────────────


class DownloadSignRequest(BaseModel):
    path: str = Field(..., description="File or folder path within the project")


class DownloadSignResponse(BaseModel):
    url: str = Field(..., description="Pre-signed download URL (valid for ~5 minutes)")
    expires_at: int = Field(..., description="Unix timestamp when the token expires")


class InlineSignRequest(BaseModel):
    path: str = Field(..., description="File path within the project")


class InlineSignResponse(BaseModel):
    url: str = Field(..., description="Pre-signed inline preview URL (valid for ~5 minutes)")
    expires_at: int = Field(..., description="Unix timestamp when the token expires")


@read_router.post(
    "/{project_id}/download/sign",
    response_model=ApiResponse[DownloadSignResponse],
    summary="Mint a signed URL for downloading a file or folder",
)
def sign_download(
    project_id: str,
    body: DownloadSignRequest,
    authorization: AuthorizationService = Depends(get_authorization_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Authenticated step. Caller proves project access via the normal
    Bearer flow; we hand back a token that the browser can use for a
    plain `<a download>` navigation."""
    ensure_project_access(authorization, current_user, project_id)
    clean_path = validate_path(body.path)

    token, expires_at = issue_token(
        project_id=project_id,
        path=clean_path,
        user_id=current_user.user_id,
    )

    url = (
        f"/api/v1/content/{project_id}/download"
        f"?path={quote(clean_path, safe='')}&token={quote(token, safe='')}"
    )

    return ApiResponse.success(
        data=DownloadSignResponse(url=url, expires_at=expires_at)
    )


@read_router.post(
    "/{project_id}/inline/sign",
    response_model=ApiResponse[InlineSignResponse],
    summary="Mint a signed URL for inline media/PDF preview",
)
def sign_inline(
    project_id: str,
    body: InlineSignRequest,
    authorization: AuthorizationService = Depends(get_authorization_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    """Authenticated step for embedding protected files in native
    browser elements (`<audio>`, `<video>`, `<iframe>`).

    These elements cannot attach our Bearer header, so we issue the
    same short-lived HMAC token as downloads but return an inline
    endpoint instead of an attachment endpoint.
    """
    ensure_project_access(authorization, current_user, project_id)
    clean_path = validate_path(body.path)

    token, expires_at = issue_token(
        project_id=project_id,
        path=clean_path,
        user_id=current_user.user_id,
    )

    url = (
        f"/api/v1/content/{project_id}/inline"
        f"?path={quote(clean_path, safe='')}&token={quote(token, safe='')}"
    )

    return ApiResponse.success(
        data=InlineSignResponse(url=url, expires_at=expires_at)
    )


@read_router.get(
    "/{project_id}/inline",
    summary="Serve a token-authenticated file inline for native previews",
)
def inline_file(
    project_id: str,
    request: Request,
    path: str = Query(..., description="File path"),
    token: str = Query(..., description="Signed token from /inline/sign"),
    ops: ProductOperationAdapter = Depends(get_product_operation_adapter),
):
    try:
        claims = verify_token(token)
    except DownloadTokenError as exc:
        raise HTTPException(status_code=401, detail=f"invalid token: {exc}")

    clean_path = validate_path(path)
    if claims.project_id != project_id or claims.path != clean_path:
        raise HTTPException(status_code=403, detail="token does not match request")

    try:
        entry = ops.stat(project_id, clean_path)
    except ObjectNotFoundError as exc:
        _raise_storage_integrity_error(project_id, clean_path, exc)
    if not entry or entry.type == "folder":
        raise HTTPException(status_code=404, detail=f"File not found: {clean_path}")

    try:
        content = ops.read_file(project_id, clean_path)
    except ObjectNotFoundError as exc:
        _raise_storage_integrity_error(project_id, clean_path, exc)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"File not found: {clean_path}")

    from src.version_engine.read.tree_reader import detect_mime
    mime = detect_mime(clean_path) or "application/octet-stream"
    filename = entry.name or clean_path.rsplit("/", 1)[-1] or "preview"

    return _serve_file_bytes(
        request=request,
        content=content,
        media_type=mime,
        filename=filename,
        disposition="inline",
        cache_control="private, no-store",
    )


@read_router.get(
    "/{project_id}/download",
    summary="Download a file or folder (folders are streamed as zip)",
)
def download(
    project_id: str,
    request: Request,
    path: str = Query(..., description="File or folder path"),
    token: str = Query(..., description="Signed token from /download/sign"),
    ops: ProductOperationAdapter = Depends(get_product_operation_adapter),
):
    """Token-authenticated streaming download.

    - **Files**: streamed back as raw bytes with `Content-Disposition: attachment`.
    - **Folders**: walked via `ProductOperationAdapter.list_tree` and packed into a
      `zipstream-ng` `ZipStream`. We yield chunks as they're produced so
      the browser's native download manager picks up the response
      immediately and shows real byte progress (instead of waiting for
      the whole zip to be buffered server-side).

    Hidden entries are excluded from folder archives.
    """
    try:
        claims = verify_token(token)
    except DownloadTokenError as exc:
        raise HTTPException(status_code=401, detail=f"invalid token: {exc}")

    # Defense in depth: query params must match what the token was issued for.
    # Without this, a token issued for `notes/secret.md` could be replayed
    # against `?path=notes/public.md` (same project, same user, but different
    # asset). The token already pins both, so we just enforce equality here.
    clean_path = validate_path(path)
    if claims.project_id != project_id or claims.path != clean_path:
        raise HTTPException(status_code=403, detail="token does not match request")

    try:
        entry = ops.stat(project_id, clean_path)
    except ObjectNotFoundError as exc:
        _raise_storage_integrity_error(project_id, clean_path, exc)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Path not found: {clean_path}")

    if entry.type == "folder":
        folder_name = entry.name or clean_path.rsplit("/", 1)[-1] or "root"
        prefix = clean_path.strip("/")
        prefix_with_slash = f"{prefix}/" if prefix else ""

        # Snapshot the tree once, up-front. Doing it inside the generator
        # would mean the request handler returns before list_tree runs,
        # which complicates error reporting (the headers are already on
        # the wire by then).
        try:
            entries = ops.list_tree(project_id, clean_path, max_depth=-1)
        except ObjectNotFoundError as exc:
            _raise_storage_integrity_error(project_id, clean_path, exc)
        except PathNotFoundError as exc:
            _raise_directory_not_found(clean_path, exc)
        except VersionReadError as exc:
            _raise_version_read_error(clean_path, exc)

        def chunks():
            # zipstream-ng's add()/mkdir() *queue* entries; all_files()
            # processes the queue and yields chunks as they're produced.
            # Calling all_files() after each add() means bytes leave the
            # server as soon as each file is zipped — which is what gives
            # the browser's native download manager real-time progress.
            # footer() writes the central directory + end-of-archive
            # record once all entries are done.
            zs = ZipStream(compress_type=ZIP_DEFLATED)
            for e in entries:
                if any(part.startswith(".") for part in e.path.strip("/").split("/") if part):
                    continue
                rel_path = e.path[len(prefix_with_slash):] if prefix_with_slash else e.path
                if not rel_path:
                    continue
                arcname = f"{folder_name}/{rel_path}"

                if e.type == "folder":
                    # mkdir() is the dedicated API for empty directory
                    # entries — keeps empty folders visible in the archive.
                    zs.mkdir(arcname)
                    yield from zs.all_files()
                    continue

                try:
                    content = ops.read_file(project_id, e.path)
                except ObjectNotFoundError:
                    # The pre-flight list_tree should have caught broken
                    # tree references. If a blob disappears between list and
                    # stream, fail that entry instead of silently corrupting
                    # the zip with partial content.
                    raise
                except FileNotFoundError:
                    continue
                zs.add(data=content, arcname=arcname)
                yield from zs.all_files()

            yield from zs.footer()

        return StreamingResponse(
            chunks(),
            media_type="application/zip",
            headers={
                "Content-Disposition": _content_disposition_attachment(f"{folder_name}.zip"),
                "Cache-Control": "private, no-store",
                # Hint to proxies/CDNs not to buffer the response, otherwise
                # the browser's progress bar won't move until the whole zip
                # is built.
                "X-Accel-Buffering": "no",
            },
        )

    # Single-file path: same bytes as /raw, but `attachment` so the
    # browser triggers a save dialog instead of trying to render it.
    try:
        content = ops.read_file(project_id, clean_path)
    except ObjectNotFoundError as exc:
        _raise_storage_integrity_error(project_id, clean_path, exc)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"File not found: {clean_path}")

    from src.version_engine.read.tree_reader import detect_mime
    mime = detect_mime(clean_path) or "application/octet-stream"
    filename = entry.name or clean_path.rsplit("/", 1)[-1] or "download"

    return _serve_file_bytes(
        request=request,
        content=content,
        media_type=mime,
        filename=filename,
        disposition="attachment",
        cache_control="private, no-store",
    )


# Suppress unused-import warning for DEFAULT_TTL_SECONDS — it's re-exported
# for any caller that wants to know our token TTL without hard-coding it.
_ = DEFAULT_TTL_SECONDS


@read_router.get(
    "/{project_id}/stat",
    response_model=ApiResponse[StatResponse],
    summary="Get file/directory info",
)
def stat(
    project_id: str,
    path: str = Query("", description="Path, or provide path_bytes_b64"),
    path_bytes_b64: Annotated[str | None, Query()] = None,
    ops: ProductOperationAdapter = Depends(get_product_operation_adapter),
    authorization: AuthorizationService = Depends(get_authorization_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    grant = ensure_project_access(authorization, current_user, project_id)
    clean_path = _read_request_path(path, path_bytes_b64)

    try:
        with _product_read(ops, project_id, grant, byte_path=path_bytes_b64 is not None) as (read, revision):
            head_commit_id = read.get_head_commit_id(project_id)
            scope_head_commit_id = read.get_scope_head_commit_id_for_path(project_id, clean_path)
            entry = read.stat(project_id, clean_path)
    except ObjectNotFoundError as exc:
        _raise_storage_integrity_error(project_id, clean_path, exc)
    if not entry:
        return ApiResponse.success(data=StatResponse(
            path=display_git_path(clean_path),
            path_bytes_b64=git_path_b64(clean_path) if revision else None,
            repository_revision=revision,
            type="",
            name="",
            exists=False,
            head_commit_id=head_commit_id,
            scope_head_commit_id=scope_head_commit_id,
        ))

    return ApiResponse.success(data=StatResponse(
        path=display_git_path(entry.path),
        path_bytes_b64=git_path_b64(entry.path) if revision else None,
        repository_revision=revision,
        git_mode=entry.git_mode,
        type=entry.type,
        name=display_git_path(entry.name),
        name_bytes_b64=git_path_b64(entry.name) if revision else None,
        content_hash=entry.content_hash,
        size_bytes=entry.size_bytes,
        mime_type=entry.mime_type,
        children_count=entry.children_count,
        integrity_status=entry.integrity_status,
        exists=True,
        head_commit_id=head_commit_id,
        scope_head_commit_id=scope_head_commit_id,
    ))


@read_router.get(
    "/{project_id}/tree",
    response_model=ApiResponse[TreeResponse],
    summary="Get full directory tree",
)
def full_tree(
    project_id: str,
    path: str = Query("", description="Starting path"),
    path_bytes_b64: Annotated[str | None, Query()] = None,
    max_depth: int = Query(-1, description="Maximum recursion depth, -1 = unlimited"),
    ops: ProductOperationAdapter = Depends(get_product_operation_adapter),
    authorization: AuthorizationService = Depends(get_authorization_service),
    current_user: CurrentUser = Depends(get_current_user),
):
    grant = ensure_project_access(authorization, current_user, project_id)
    clean_path = _read_request_path(path, path_bytes_b64)

    try:
        with _product_read(ops, project_id, grant, byte_path=path_bytes_b64 is not None) as (read, revision):
            entries = read.list_tree(project_id, clean_path, max_depth=max_depth)
            head_commit_id = read.get_head_commit_id(project_id)
    except ObjectNotFoundError as exc:
        _raise_storage_integrity_error(project_id, clean_path, exc)
    except PathNotFoundError as exc:
        _raise_directory_not_found(clean_path, exc)
    except VersionReadError as exc:
        _raise_version_read_error(clean_path, exc)
    return ApiResponse.success(data=TreeResponse(
        path=display_git_path(clean_path),
        path_bytes_b64=git_path_b64(clean_path) if revision else None,
        repository_revision=revision,
        entries=[entry_to_response(e) for e in entries],
        head_commit_id=head_commit_id,
    ))
