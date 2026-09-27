"""Path and provider helpers for Integration.

Integration paths are product-root paths, not access scopes. A connection's
``target_path`` says where fetched data should land in the project tree.
"""

from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass
from typing import Any

from src.provider._base import FetchResult
from src.provider.materializers import MaterializedOutput
from src.provider.schemas import Sync


PROVIDER_ALIASES = {
    "calendar": "google_calendar",
    "docs": "google_docs",
    "sheets": "google_sheets",
    "drive": "google_drive",
    "search_console": "google_search_console",
    "gsc": "google_search_console",
}


@dataclass(frozen=True)
class SynchronizeWritePlan:
    files: dict[str, bytes]
    deleted: list[str]
    result_path: str
    message: str


def canonical_provider(provider: str) -> str:
    value = (provider or "").strip()
    return PROVIDER_ALIASES.get(value, value)


def normalize_path(path: str | None) -> str:
    if path is None:
        return ""
    value = str(path).strip().replace("\\", "/")
    while value.startswith("/"):
        value = value[1:]
    while value.endswith("/"):
        value = value[:-1]
    while "//" in value:
        value = value.replace("//", "/")
    if value in (".", "/"):
        return ""
    clean = posixpath.normpath(value)
    if clean in ("", "."):
        return ""
    if clean.startswith("../") or clean == "..":
        raise ValueError(f"Invalid integration path: {path!r}")
    return clean


def join_path(base_path: str | None, relative_path: str) -> str:
    base = normalize_path(base_path)
    rel = normalize_path(relative_path)
    if not rel:
        raise ValueError("Integration write path cannot be empty")
    return f"{base}/{rel}" if base else rel


def safe_filename(name: str | None, fallback: str = "data") -> str:
    value = str(name or "").strip().replace("\\", "/")
    value = value.split("/")[-1].strip()
    value = re.sub(r"[\x00-\x1f]", "", value)
    value = re.sub(r"[<>:\"|?*]", "-", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value or fallback


def has_extension(path: str | None) -> bool:
    value = normalize_path(path)
    if not value:
        return False
    name = value.rsplit("/", 1)[-1]
    return "." in name and not name.startswith(".") and not name.endswith(".")


def default_extension(node_type: str | None) -> str:
    if node_type == "json":
        return ".json"
    if node_type == "markdown":
        return ".md"
    return ".bin"


def default_data_file(node_type: str | None) -> str:
    return f"data{default_extension(node_type)}"


def filename_for_result(result: FetchResult) -> str:
    name = safe_filename(result.node_name, "data")
    if has_extension(name):
        return name
    return f"{name}{default_extension(result.node_type)}"


def to_bytes(content: Any) -> bytes:
    if isinstance(content, bytes):
        return content
    if isinstance(content, bytearray):
        return bytes(content)
    if isinstance(content, (dict, list)):
        return json.dumps(content, ensure_ascii=False, indent=2).encode("utf-8")
    if isinstance(content, str):
        return content.encode("utf-8")
    return str(content).encode("utf-8")


def plan_fetch_result(
    *,
    sync: Sync,
    result: FetchResult,
    target_exists_as_file: bool = False,
) -> SynchronizeWritePlan:
    """Translate connector output into project-root Version Engine writes."""

    config = dict(sync.config or {})
    target_path = normalize_path(
        config.get("target_path") or sync.path or result.node_name or sync.provider
    )
    if not target_path:
        target_path = safe_filename(sync.provider, "integration")

    legacy_data_file = config.get("data_file")
    target_kind = config.get("target_path_kind")
    exact_single_file = (
        target_exists_as_file
        or target_kind == "file"
        or has_extension(target_path)
    )

    if result.files is not None:
        files = {
            join_path(target_path, rel_path): to_bytes(content)
            for rel_path, content in result.files.items()
        }
        deleted: list[str] = []
        if legacy_data_file:
            placeholder = join_path(target_path, str(legacy_data_file))
            if placeholder not in files:
                deleted.append(placeholder)
        return SynchronizeWritePlan(
            files=files,
            deleted=deleted,
            result_path=target_path,
            message=result.summary or f"Sync from {sync.provider}",
        )

    if legacy_data_file:
        file_path = join_path(target_path, str(legacy_data_file))
    elif exact_single_file:
        file_path = target_path
    else:
        file_path = join_path(target_path, filename_for_result(result))

    return SynchronizeWritePlan(
        files={file_path: to_bytes(result.content)},
        deleted=[],
        result_path=file_path,
        message=result.summary or f"Sync from {sync.provider}",
    )


def plan_materialized_result(
    *,
    sync: Sync,
    materialized: MaterializedOutput,
) -> SynchronizeWritePlan:
    """Mount materializer-owned relative files under an Integration target path."""

    config = dict(sync.config or {})
    target_path = normalize_path(
        config.get("target_path") or sync.path or sync.provider
    )
    if not target_path:
        target_path = safe_filename(sync.provider, "integration")

    files = {
        join_path(target_path, rel_path): to_bytes(content)
        for rel_path, content in materialized.files.items()
    }
    deleted = [
        join_path(target_path, rel_path)
        for rel_path in materialized.deleted
    ]

    legacy_data_file = config.get("data_file")
    if legacy_data_file:
        placeholder = join_path(target_path, str(legacy_data_file))
        if placeholder not in files and placeholder not in deleted:
            deleted.append(placeholder)

    primary_path = materialized.primary_path or "index.json"
    return SynchronizeWritePlan(
        files=files,
        deleted=deleted,
        result_path=join_path(target_path, primary_path),
        message=materialized.summary or f"Sync from {sync.provider}",
    )
