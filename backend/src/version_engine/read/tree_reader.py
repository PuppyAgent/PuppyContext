"""Shared Git file DTOs and presentation helpers for native readers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.infra.file_formats import detect_mime, detect_node_type

if TYPE_CHECKING:
    pass

# `detect_type` is re-exported (alias of `detect_node_type`) so the
# many existing imports of `tree_reader.detect_type` keep working.
# All format knowledge lives in `src.infra.file_formats`.
detect_type = detect_node_type
__all__ = [
    "VersionBlobRead",
    "VersionEntry",
    "detect_mime",
    "detect_type",
]


_ENTRYPOINT_FILE_NAMES = {"readme.md", "start here.md"}


@dataclass
class VersionEntry:
    """A single entry (file or directory) in a version tree."""

    name: str
    path: str
    type: str  # "folder" | "json" | "markdown" | "file"
    content_hash: str | None = None
    size_bytes: int | None = None
    mime_type: str | None = None
    children_count: int | None = None
    integrity_status: str = "ok"
    created_at: str | None = None
    modified_at: str | None = None
    git_mode: str | None = None


def _entry_sort_key(entry: VersionEntry) -> tuple[int, str]:
    name = entry.name.lower()
    if entry.type != "folder" and name in _ENTRYPOINT_FILE_NAMES:
        return (0, name)
    if entry.type == "folder":
        return (1, name)
    return (2, name)


@dataclass
class VersionBlobRead:
    """Bytes read from a Git blob, plus the full decoded blob size."""

    content: bytes
    total_size: int
    content_hash: str
    ranged: bool = False
