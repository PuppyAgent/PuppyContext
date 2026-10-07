"""Materialization contracts for durable Integration sync outputs.

Connectors fetch provider-shaped content. Materializers convert that content
into Puppyone-owned workspace files under the connection mount path.
"""

from __future__ import annotations

import csv
import io
import posixpath
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from src.provider._base import FetchResult
from src.provider.schemas import MaterializationInput


FileContent = bytes | bytearray | str | dict[str, Any] | list[Any]


@dataclass(frozen=True)
class MaterializationSchema:
    """Static UI/API contract for how a provider lands in the workspace."""

    id: str
    version: int
    label: str
    description: str
    preview_paths: tuple[str, ...]
    managed: bool = True

    def ref(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
        }

    def to_dict(
        self,
        *,
        provider: str | None = None,
        latest: bool = False,
        latest_version: int | None = None,
    ) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "provider": provider,
            "label": self.label,
            "description": self.description,
            "preview_paths": list(self.preview_paths),
            "managed": self.managed,
            "latest": latest,
            "latest_version": latest_version or self.version,
            "upgrade_available": latest_version is not None and self.version < latest_version,
        }


@dataclass
class MaterializedOutput:
    """Relative files ready for the caller to publish at its authorized target."""

    files: dict[str, FileContent]
    deleted: list[str] = field(default_factory=list)
    summary: str | None = None
    primary_path: str | None = None
    content_hash: str | None = None


class SourceMaterializer(Protocol):
    provider: str
    schema: MaterializationSchema

    def materialize(self, result: FetchResult, sync: MaterializationInput) -> MaterializedOutput:
        ...


def ensure_mapping(content: Any) -> dict[str, Any]:
    return content if isinstance(content, dict) else {}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_name(value: Any, fallback: str = "item", max_length: int = 80) -> str:
    text = str(value or "").strip()
    if not text:
        text = fallback
    text = text.replace("\x00", "")
    text = text.replace("/", "-").replace("\\", "-")
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[<>:\"|?*]", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip(" .-")
    if not text:
        text = fallback
    return text[:max_length].rstrip(" .-") or fallback


def relative_path(*parts: Any) -> str:
    cleaned = []
    for part in parts:
        for segment in str(part or "").replace("\\", "/").split("/"):
            if segment.strip():
                cleaned.append(safe_name(segment, fallback="item"))
    path = posixpath.join(*cleaned) if cleaned else "item"
    normalized = posixpath.normpath(path)
    if normalized.startswith("../") or normalized in ("", ".", ".."):
        raise ValueError(f"Invalid materialized path: {path!r}")
    return normalized


def parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value or "").strip()
        if not text:
            return datetime.now(timezone.utc)
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def yaml_scalar(value: Any) -> str:
    text = str(value if value is not None else "").replace("\n", " ").strip()
    return text.replace('"', '\\"')


def frontmatter(metadata: dict[str, Any]) -> str:
    lines = ["---"]
    for key, value in metadata.items():
        if isinstance(value, (list, tuple)):
            joined = ", ".join(str(item) for item in value)
            lines.append(f'{key}: "{yaml_scalar(joined)}"')
        elif isinstance(value, bool):
            lines.append(f"{key}: {str(value).lower()}")
        elif isinstance(value, (int, float)):
            lines.append(f"{key}: {value}")
        else:
            lines.append(f'{key}: "{yaml_scalar(value)}"')
    lines.append("---")
    return "\n".join(lines)


def csv_text(headers: list[str], rows: list[dict[str, Any]]) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({header: row.get(header, "") for header in headers})
    return output.getvalue()


def source_meta(
    *,
    provider: str,
    schema: MaterializationSchema,
    result: FetchResult,
    content: dict[str, Any],
    sync: MaterializationInput,
    source_name: str | None = None,
) -> dict[str, Any]:
    source = sync.source
    return {
        "provider": provider,
        "schema": schema.id,
        "schema_version": schema.version,
        "managed_by": "puppyone",
        "source": source if isinstance(source, dict) else None,
        "source_name": source_name or content.get("account") or content.get("spreadsheet_title") or content.get("folder_name") or result.node_name,
        **sync.provenance,
        "synced_at": content.get("synced_at") or utc_now_iso(),
        "content_hash": result.content_hash,
    }
