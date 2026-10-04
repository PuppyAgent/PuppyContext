"""Git upload-pack/info-refs responses."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import Response, StreamingResponse

from src.version_engine.adapters.git._async_context import (
    enter_sync_context_off_loop,
)
from src.version_engine.adapters.git.object_quarantine import (
    GitViewCurrentCorruptError,
    receive_pack_advertisement_bare_repo,
    transport_bare_repo,
    upload_pack_bare_repo,
)
from src.version_engine.adapters.git.protocol import (
    flush_pkt,
    git_service_command,
    is_object_id,
    pkt_line,
    run_git,
)
from src.version_engine.adapters.git.refs import scope_named_refs
from src.utils.logger import log_error


# Chunk size for streaming the pack response off the git subprocess.
# 64 KiB keeps per-read overhead low without holding much on the heap.
_UPLOAD_PACK_STREAM_CHUNK = 64 * 1024


def _scope_version_refs(repo, scope_path: str) -> dict[str, str]:
    """Stored branch/tag refs for this scope as ``{ref_name: commit_id}``
    (GAP-3). Empty (and behaviour-preserving) when there is no project id,
    no refs, or the lookup fails — the advertise/serve paths then behave
    exactly as the single-branch transport always has.
    """
    project_id = getattr(repo, "_project_id", "") or ""
    if not project_id:
        return {}
    try:
        from src.version_engine.infrastructure.supabase.version_ref_repository import (
            VersionRefStore,
        )

        rows = VersionRefStore().list_refs(project_id, scope_path)
        return {
            r["ref_name"]: r["commit_id"]
            for r in rows
            if r.get("ref_name") and r.get("commit_id")
        }
    except Exception as exc:  # noqa: BLE001 — refs are additive; never break transport
        log_error(f"[upload-pack] version_refs lookup failed: {exc}")
        return {}


def info_refs_response(
    repo,
    service: str,
    scope_path: str,
    scope_excludes: list[str],
) -> Response:
    if service not in {"git-upload-pack", "git-receive-pack"}:
        raise HTTPException(status_code=400, detail="unsupported git service")

    if service == "git-receive-pack":
        repo_context = receive_pack_advertisement_bare_repo(
            repo,
            scope_path,
            scope_excludes,
            extra_refs=scope_named_refs(repo, scope_path, strict=True),
        )
    else:
        # Advertise the scope head AND any stored branch/tag refs (GAP-3).
        # upload_pack_bare_repo writes them into a per-request bare repo
        # (never the shared cache); with no stored refs it advertises
        # exactly refs/heads/main + HEAD as before.
        repo_context = upload_pack_bare_repo(
            repo,
            scope_path,
            scope_excludes,
            extra_refs=_scope_version_refs(repo, scope_path),
        )

    try:
        with repo_context as bare_dir:
            advertised = run_git([
                git_service_command(service),
                "--stateless-rpc",
                "--advertise-refs",
                str(bare_dir),
            ])
    except GitViewCurrentCorruptError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "GIT_VIEW_CURRENT_CORRUPT",
                "message": str(exc),
            },
        ) from exc
    return Response(
        content=b"".join([
            pkt_line(f"# service={service}\n".encode("ascii")),
            flush_pkt(),
            advertised,
        ]),
        media_type=f"application/x-{service}-advertisement",
        headers={"Cache-Control": "no-cache"},
    )


async def upload_pack_streaming_response(
    repo,
    scope_path: str,
    scope_excludes: list[str],
    request_path: Path,
) -> Response:
    """Stream a Git fetch/clone pack straight from ``git upload-pack``.

    The response pack for a large clone can be gigabytes. The previous
    implementation captured it into a single ``bytes`` via ``run_git`` and
    handed that to ``Response(content=...)``, so the whole pack sat on the
    Python heap twice and risked OOM under concurrent clones (GAP-1/14).

    Instead we drive ``git upload-pack`` with the request body fed from its
    on-disk spool (``request_path``) and pipe stdout to the client in
    chunks — neither the request nor the response is ever fully
    materialised in memory.

    On success ``request_path`` is owned by this response: the streaming
    generator deletes it once the pack has been sent (or the transfer is
    aborted). If this function RAISES before returning the response, the
    caller still owns ``request_path`` and must clean it up.
    """
    # The upload-pack request is the (small) want/have negotiation; read it
    # only to scope the bare repo to the requested tips. The large payload
    # is the RESPONSE, which is what we must avoid buffering.
    try:
        body = request_path.read_bytes()
    except OSError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"unreadable upload-pack request: {exc}",
        ) from exc

    cm = upload_pack_bare_repo(
        repo, scope_path, scope_excludes,
        wants=_upload_pack_wants(body),
        extra_refs=_scope_version_refs(repo, scope_path),
    )
    try:
        # Building the transport view takes a cross-process file lock. Never
        # acquire that blocking lock on the ASGI event-loop thread: a previous
        # StreamingResponse may need the same loop to advance its body and
        # release the lease, which otherwise deadlocks concurrent clone/fetch
        # requests. Keep the pre-response enter here so a corrupt current view
        # can still return HTTP 409 before any response bytes are sent.
        bare_dir = await enter_sync_context_off_loop(cm)
    except GitViewCurrentCorruptError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "GIT_VIEW_CURRENT_CORRUPT",
                "message": str(exc),
            },
        ) from exc

    return StreamingResponse(
        _stream_upload_pack(cm, bare_dir, request_path),
        media_type="application/x-git-upload-pack-result",
        headers={"Cache-Control": "no-cache"},
    )


def _stream_upload_pack(cm, bare_dir: Path, request_path: Path):
    """Yield the pack bytes from ``git upload-pack``, then tear everything
    down. Runs as the streaming-response body, so its ``finally`` is
    guaranteed to run on completion, client disconnect, or error."""
    proc = None
    stdin_f = None
    # stderr to a temp file, never a pipe: upload-pack can emit progress to
    # stderr and a full pipe buffer would deadlock against our stdout reads.
    stderr_f = tempfile.TemporaryFile()
    try:
        stdin_f = open(request_path, "rb")
        proc = subprocess.Popen(
            ["git", "upload-pack", "--stateless-rpc", str(bare_dir)],
            stdin=stdin_f,
            stdout=subprocess.PIPE,
            stderr=stderr_f,
        )
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(_UPLOAD_PACK_STREAM_CHUNK)
            if not chunk:
                break
            yield chunk
        proc.wait()
        if proc.returncode != 0:
            # The response is already 200 with bytes on the wire, so we
            # can't switch to an error status; log it server-side. The Git
            # client surfaces the aborted/truncated transfer to the user.
            stderr_f.seek(0)
            err = stderr_f.read().decode("utf-8", errors="replace").strip()
            log_error(f"[upload-pack] git exited {proc.returncode}: {err}")
    finally:
        if proc is not None:
            if proc.stdout is not None:
                try:
                    proc.stdout.close()
                except OSError:
                    pass
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        if stdin_f is not None:
            stdin_f.close()
        stderr_f.close()
        cm.__exit__(None, None, None)
        _unlink(request_path)


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log_error(f"[upload-pack] failed to remove spool {path}: {exc}")


def _upload_pack_wants(body: bytes) -> list[str]:
    wants: list[str] = []
    offset = 0
    while offset + 4 <= len(body):
        header = body[offset:offset + 4]
        offset += 4
        try:
            size = int(header.decode("ascii"), 16)
        except ValueError:
            break
        if size == 0:
            continue
        if size < 4:
            break
        payload_size = size - 4
        payload = body[offset:offset + payload_size]
        offset += payload_size
        if not payload.startswith(b"want "):
            continue
        parts = payload.decode("ascii", errors="ignore").strip().split()
        if len(parts) >= 2 and is_object_id(parts[1]):
            wants.append(parts[1])
    return wants
