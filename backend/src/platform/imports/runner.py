"""Run one-time imports without creating persistent connector bindings."""

from __future__ import annotations

import json
import posixpath
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from src.provider._base import AuthRequirement, FetchResult
from src.platform.imports.providers import get_import_provider_registry, require_import_provider
from src.platform.imports.repository import ImportJob
from src.platform.project.write_lease import (
    ProjectWriteLease,
    ProjectWriteLeaseFactory,
    build_leased_worker_write_commands,
)

PhaseCallback = Callable[[str, int, str], Awaitable[None]]


@dataclass(frozen=True)
class ImportRunResult:
    path: str
    commit_id: str
    summary: str | None = None


def _normalize_path(path: str | None) -> str:
    if path is None:
        return ""
    value = str(path).replace("\\", "/").strip()
    while value.startswith("/"):
        value = value[1:]
    while value.endswith("/"):
        value = value[:-1]
    while "//" in value:
        value = value.replace("//", "/")
    if value in ("", "."):
        return ""
    clean = posixpath.normpath(value)
    if clean.startswith("../") or clean == "..":
        raise ValueError(f"Invalid import target path: {path!r}")
    return clean


def _join_mount_path(base_path: str | None, relative_path: str) -> str:
    base = _normalize_path(base_path)
    rel = _normalize_path(relative_path)
    if not rel:
        raise ValueError(f"Invalid connector file path: {relative_path!r}")
    return f"{base}/{rel}" if base else rel


def _to_bytes(content: Any) -> bytes:
    if isinstance(content, bytes):
        return content
    if isinstance(content, bytearray):
        return bytes(content)
    if isinstance(content, (dict, list)):
        return json.dumps(content, ensure_ascii=False, indent=2).encode("utf-8")
    if isinstance(content, str):
        return content.encode("utf-8")
    return str(content).encode("utf-8")


def _default_data_file(result: FetchResult) -> str:
    if result.node_type == "json":
        return "data.json"
    return "data.md"


def _result_mount_path(job: ImportJob, result: FetchResult) -> str:
    config = job.config or {}
    target = (
        job.target_path
        or config.get("target_path")
        or job.name
        or config.get("name")
        or result.node_name
        or job.provider
    )
    return _normalize_path(str(target))


class OneTimeImportRunner:
    """Execute a connector fetch and write the result as a one-time import."""

    def __init__(
        self,
        *,
        write_lease_factory: ProjectWriteLeaseFactory = ProjectWriteLease,
    ) -> None:
        self._write_lease_factory = write_lease_factory

    async def run(
        self,
        job: ImportJob,
        *,
        on_phase: PhaseCallback | None = None,
    ) -> ImportRunResult:
        registry = get_import_provider_registry()
        adapter = require_import_provider(registry, job.provider)

        spec = adapter.spec()
        config = dict(job.config or {})
        config["source_url"] = job.source_url
        if job.name and not config.get("name"):
            config["name"] = job.name
        if job.target_path and not config.get("target_path"):
            config["target_path"] = job.target_path

        if on_phase:
            await on_phase("fetching", 25, f"Fetching from {spec.display_name}")

        credentials = await registry.resolve_credentials(
            oauth_type=spec.oauth_type,
            user_id=job.created_by,
            required=spec.auth not in (AuthRequirement.NONE, AuthRequirement.OPTIONAL_OAUTH),
        )
        result = await adapter.fetch(config, credentials)

        if on_phase:
            await on_phase("writing", 75, "Writing files into the workspace")

        mount_path = _result_mount_path(job, result)
        actor = f"import:{job.provider}:{job.id}"

        commands = build_leased_worker_write_commands(
            write_lease_factory=self._write_lease_factory
        )

        if result.files is not None:
            files = {
                _join_mount_path(mount_path, rel_path): _to_bytes(content)
                for rel_path, content in result.files.items()
            }
            outcome = await commands.bulk_write(
                job.project_id,
                files,
                actor=actor,
                message=result.summary or f"Import from {spec.display_name}",
                source_channel="import",
            )
            written_path = mount_path
        else:
            data_file = config.get("data_file") or _default_data_file(result)
            file_path = _join_mount_path(mount_path, data_file)
            outcome = await commands.write_bytes(
                job.project_id,
                file_path,
                _to_bytes(result.content),
                actor=actor,
                message=result.summary or f"Import from {spec.display_name}",
                source_channel="import",
            )
            written_path = file_path

        return ImportRunResult(
            path=written_path,
            commit_id=outcome.result.commit_id,
            summary=result.summary,
        )
