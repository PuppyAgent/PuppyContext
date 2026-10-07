"""Rebuild derived indexes and notify from native ref events, with fenced retries.

Only the maintenance worker uses the internal read-pin capability. API readers
continue to require their current Human/Runtime grant. Event claims never grant
object publication rights. A stale projection cannot overwrite a newer ref view.
"""

from __future__ import annotations

import asyncio
import json
import uuid

from src.infra.search.text_indexer import _chunk_text, _decode_for_index
from src.utils.logger import log_warning
from src.version_engine.infrastructure.supabase.ref_authority_repository import (
    RefAuthorityRepository,
)
from src.version_engine.read.native_tree_reader import NativeTreeReader
from src.version_engine.read.repository_snapshot import RepositorySnapshot
from src.version_engine.storage.backends.s3 import S3StorageBackend


def rebuild_projection(manager, project, token):
    control = RefAuthorityRepository(manager._supabase.client)
    pin = str(uuid.uuid4())
    actor = "maintenance:native-projection"
    wire = control.begin_read(project, actor, pin)
    backend = S3StorageBackend(manager._s3, project, supabase=manager._supabase)
    snapshot = RepositorySnapshot(
        control,
        backend,
        project_id=project,
        actor=actor,
        pin=pin,
        wire=wire,
        max_bytes=256 * 1024**2,
    )
    try:
        reader = NativeTreeReader(snapshot)
        paths, chunks, indexed, budget = [], [], set(), 0
        for entry in reader.list_tree(project):
            if entry.type in {"folder", "gitlink"}:
                continue
            # SQL text cannot represent arbitrary Git path bytes. Such names
            # remain fully accessible through Git and the native Product API.
            try:
                entry.path.encode("utf-8")
            except UnicodeEncodeError:
                continue
            body = reader.read_file(project, entry.path)
            row = dict(
                path=entry.path, oid=entry.content_hash, bytes=len(body), mime=entry.mime_type or ""
            )
            paths.append(row)
            budget += len(json.dumps(row).encode())
            if budget > 32 * 1024**2:
                raise ValueError("native projection budget exceeded; index remains stale")
            if entry.content_hash in indexed:
                continue
            indexed.add(entry.content_hash)
            text = _decode_for_index(body)
            if text is None:
                continue
            for index, line, content in _chunk_text(text):
                row = dict(
                    path=entry.path,
                    oid=entry.content_hash,
                    chunk_idx=index,
                    line_start=line,
                    text=content,
                )
                budget += len(json.dumps(row).encode())
                if budget > 32 * 1024**2:
                    raise ValueError("native projection budget exceeded; index remains stale")
                chunks.append(row)
        head = reader.get_head_commit_id(project)
        control.call(
            "replace_native_projection",
            p_project=project,
            p_token=token,
            p_sequence=snapshot.ref_sequence,
            p_head=head,
            p_paths=paths,
            p_chunks=chunks,
        )
        return head, snapshot.ref_sequence
    finally:
        snapshot.close()


async def process_native_events(*, repo_manager=None, limit=20):
    from src.version_engine.bootstrap.dependencies import build_worker_version_engine_container
    from src.version_engine.derived.notifications import NotificationManager

    manager = repo_manager or build_worker_version_engine_container().repo_manager
    control = RefAuthorityRepository(manager._supabase.client)
    token = str(uuid.uuid4())
    rows = await asyncio.to_thread(
        control.call, "claim_native_projection_events", p_token=token, p_limit=limit
    )
    processed = 0
    for row in rows:
        project = row["project_id"]
        try:
            head, sequence = await asyncio.to_thread(rebuild_projection, manager, project, token)
            await NotificationManager.get().broadcast_commit_update(
                project,
                "",
                commit_id=head,
                pushed_by="repository",
                changes=[],
                message="Repository refs changed",
            )
            await asyncio.to_thread(
                control.call,
                "finish_native_projection_events",
                p_project=project,
                p_token=token,
                p_sequence=sequence,
            )
            processed += 1
        except Exception as exc:
            log_warning(f"[native-projection] retry project={project}: {type(exc).__name__}")
        finally:
            await asyncio.to_thread(
                control.call, "release_native_projection_events", p_project=project, p_token=token
            )
    return processed
