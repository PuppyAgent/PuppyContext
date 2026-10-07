"""
Template loader — discovers built-in templates from the filesystem.

Each template lives under ``builtin/<id>/`` with:

- ``manifest.toml`` — metadata (id, name, description, icon, version, ...)
- ``content/``      — the file tree that gets seeded into a new project

Loading happens once at module import. Adding a new built-in template means
dropping a folder under ``builtin/`` — no code change required.

See ``README.md`` in this package for the full manifest schema and authoring
conventions.

Why bytes (not str) for file contents? It keeps the loader uniform across
Markdown / JSON / future binary attachments (images, PDFs), and matches what
``ProductOperationAdapter.bulk_write`` expects on the write path.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from .schema import ProjectTemplate

_PACKAGE_DIR = Path(__file__).parent
_BUILTIN_DIR = _PACKAGE_DIR / "builtin"

_REQUIRED_FIELDS = ("id", "name", "description", "icon")


# ── Manifest + content readers ──────────────────────────────────────


def _read_manifest(template_dir: Path) -> dict:
    manifest_path = template_dir / "manifest.toml"
    if not manifest_path.is_file():
        raise ValueError(f"Template {template_dir.name!r}: missing manifest.toml")
    with manifest_path.open("rb") as f:
        return tomllib.load(f)


def _read_content(template_dir: Path) -> dict[str, bytes]:
    content_dir = template_dir / "content"
    if not content_dir.is_dir():
        raise ValueError(f"Template {template_dir.name!r}: missing content/ directory")

    files: dict[str, bytes] = {}
    for path in sorted(content_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(content_dir).as_posix()
        files[rel] = path.read_bytes()

    if not files:
        raise ValueError(f"Template {template_dir.name!r}: content/ is empty")
    return files


def _build_template(template_dir: Path) -> ProjectTemplate:
    manifest = _read_manifest(template_dir)

    missing = [k for k in _REQUIRED_FIELDS if k not in manifest]
    if missing:
        raise ValueError(f"Template {template_dir.name!r}: manifest missing fields {missing}")

    files = _read_content(template_dir)

    return ProjectTemplate(
        id=manifest["id"],
        name=manifest["name"],
        description=manifest["description"],
        icon=manifest["icon"],
        files=files,
        version=manifest.get("version", "1.0.0"),
        author=manifest.get("author"),
        tags=tuple(manifest.get("tags", [])),
        order=manifest.get("order", 100),
        category=manifest.get("category"),
        cover=manifest.get("cover"),
        screenshots=tuple(manifest.get("screenshots", [])),
        long_description=manifest.get("long_description"),
    )


# ── Discovery ──────────────────────────────────────────────────────


def _discover_builtin() -> dict[str, ProjectTemplate]:
    """Walk ``builtin/`` and load every template directory."""
    if not _BUILTIN_DIR.is_dir():
        return {}

    templates: dict[str, ProjectTemplate] = {}
    for entry in sorted(_BUILTIN_DIR.iterdir()):
        if not entry.is_dir() or entry.name.startswith((".", "_")):
            continue
        tmpl = _build_template(entry)
        if tmpl.id in templates:
            raise ValueError(f"Duplicate template id: {tmpl.id!r}")
        templates[tmpl.id] = tmpl
    return templates


# Loaded once at import. Insertion order = filesystem sort order; the public
# `list_templates()` re-sorts by `order` field for the picker UI.
TEMPLATES: dict[str, ProjectTemplate] = _discover_builtin()


# ── Public API ─────────────────────────────────────────────────────


def get_template(template_id: str) -> ProjectTemplate | None:
    return TEMPLATES.get(template_id)


def _infer_node_type(path: str) -> str:
    """Infer rendering node type for the preview grid."""
    if path.endswith("/"):
        return "folder"
    if path.endswith(".md"):
        return "markdown"
    if path.endswith(".json"):
        return "json"
    return "file"


def _build_preview(files: dict[str, bytes], limit: int = 6) -> list[dict]:
    """Top-level preview of a template's structure (folders + root files)."""
    seen: list[tuple[str, str]] = []
    seen_set: set[str] = set()

    for path in files:
        head = path.split("/", 1)[0]
        is_folder = "/" in path
        display = head + ("/" if is_folder else "")
        if display in seen_set:
            continue
        seen_set.add(display)
        seen.append((display, "folder" if is_folder else _infer_node_type(path)))
        if len(seen) >= limit:
            break

    return [{"name": name, "type": ntype} for name, ntype in seen]


def list_templates() -> list[dict]:
    """Return template card metadata (without file contents) for the gallery."""
    sorted_templates = sorted(TEMPLATES.values(), key=lambda t: (t.order, t.id))
    return [
        {
            "id": t.id,
            "name": t.name,
            "description": t.description,
            "icon": t.icon,
            "category": t.category,
            "cover": t.cover,
            "author": t.author,
            "tags": list(t.tags),
            "preview": _build_preview(t.files),
        }
        for t in sorted_templates
    ]


def _pick_preview_doc(files: dict[str, bytes], limit: int = 20000) -> dict | None:
    """Choose a representative document to render on the detail page: a
    top-level README.md, else the first markdown file, else the first file.
    Returns {path, content} with content decoded as text (truncated)."""
    md_files = [p for p in files if p.lower().endswith(".md")]
    readme = next((p for p in md_files if p.lower().rsplit("/", 1)[-1] == "readme.md"), None)
    target = readme or (md_files[0] if md_files else None)
    if target is None:
        target = next(iter(files), None)
    if target is None:
        return None
    return {
        "path": target,
        "content": files[target].decode("utf-8", errors="replace")[:limit],
    }


def get_template_detail(template_id: str) -> dict | None:
    """Full template metadata + a rich preview (file tree + a rendered doc) for
    the marketplace detail page. No raw file bytes are exposed."""
    t = get_template(template_id)
    if t is None:
        return None
    return {
        "id": t.id,
        "name": t.name,
        "description": t.description,
        "icon": t.icon,
        "category": t.category,
        "cover": t.cover,
        "screenshots": list(t.screenshots),
        "long_description": t.long_description,
        "author": t.author,
        "tags": list(t.tags),
        "version": t.version,
        "file_tree": sorted(t.files.keys()),
        "preview": _build_preview(t.files),
        "preview_doc": _pick_preview_doc(t.files),
    }


async def seed_template_content(
    project_id: str,
    template_id: str,
    created_by: str,
) -> dict:
    """Write a template's files into a project through the L3 command service."""
    from src.platform.project.write_lease import build_leased_worker_write_commands

    tmpl = get_template(template_id)
    if tmpl is None:
        return {"error": f"Unknown template: {template_id}"}

    commands = build_leased_worker_write_commands(
        project_id=project_id,
        user_id=created_by,
        operation_key=f"template:{project_id}:{template_id}",
    )
    await commands.bulk_write(
        project_id,
        tmpl.files,
        actor=created_by,
        message=f"template: {tmpl.name}",
    )

    return {"template": template_id, "files": list(tmpl.files.keys())}
