"""
Request-scoped agent sandbox write-back state.

Each AgentSandboxSession holds a InProcessVersionClient that was cloned once at
request start. Before the response ends, it pushes modified files through the
Write Engine and destroys the sandbox.

Lifecycle:
  1. Agent chat starts → clone version scope → mount in sandbox → register session
  2. Request completion → read changed files → client.push()
  3. Destroy sandbox; the next turn starts from the new canonical head
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from src.version_engine.adapters.batch.in_process_client import InProcessVersionClient


@dataclass
class SandboxFile:
    """A file to mount in a sandbox container.

    ``base_commit_id`` snapshots the Git commit this file was cloned at
    so write-back can be traced to a specific point-in-time snapshot.
    """
    path: str
    content: str | None = None
    s3_key: str | None = None
    content_type: str = "application/octet-stream"
    version_path: str | None = None
    node_type: str | None = None
    base_commit_id: str = ""


@dataclass
class SandboxData:
    """Prepared sandbox data from a version scope clone."""
    files: list[SandboxFile] = field(default_factory=list)
    node_type: str = "json"
    root_path: str = ""
    root_node_name: str = ""
    node_path_map: dict = field(default_factory=dict)


async def prepare_sandbox_data(
    ops,
    project_id: str,
    path: str,
) -> SandboxData:
    """Prepare files from version tree for sandbox mounting.

    Reads the version tree at `path` and returns SandboxFile objects
    suitable for sandbox_service.start_with_files().
    """

    entry = ops.stat(project_id, path)
    if not entry:
        raise ValueError(f"Path not found: {path}")

    node_type = entry.type or "json"
    node_name = entry.name or path.rsplit("/", 1)[-1]

    if node_type == "folder":
        children = ops.list_tree(project_id, path)
        files: list[SandboxFile] = []
        for child in children:
            if child.type == "folder":
                continue
            relative = child.path
            if relative.startswith(path + "/"):
                relative = relative[len(path) + 1:]
            try:
                raw = ops.read_file(project_id, child.path)
                text = raw.decode("utf-8", errors="replace")
            except Exception:
                continue
            files.append(SandboxFile(
                path=f"/workspace/{relative}",
                content=text,
                content_type="application/json" if child.type == "json" else "text/markdown" if child.type == "markdown" else "text/plain",
                version_path=child.path,
                node_type=child.type,
            ))
        return SandboxData(files=files, node_type="folder", root_path=path, root_node_name=node_name)

    raw = ops.read_file(project_id, path)
    text = raw.decode("utf-8", errors="replace")
    sf = SandboxFile(
        path=f"/workspace/{node_name}" if node_type != "json" else "/workspace/data.json",
        content=text,
        content_type="application/json" if node_type == "json" else "text/markdown" if node_type == "markdown" else "text/plain",
        version_path=path,
        node_type=node_type,
    )
    return SandboxData(files=[sf], node_type=node_type, root_path=path, root_node_name=node_name)


@dataclass
class AgentSandboxSession:
    sandbox_session_id: str
    chat_session_id: str
    agent_id: str
    version_client: InProcessVersionClient
    cloned_files: dict[str, bytes]
    scope_path: str
    created_at: float
    last_active: float
    readonly: bool = False
    project_id: str = ""
    parent_path: str = ""
    repo_manager: Any = None


async def _read_modified_files(
    sandbox_service,
    sandbox_session_id: str,
    original_files: dict[str, bytes],
    mount_path: str,
    scope_path: str,
) -> tuple[dict[str, bytes], list[str]]:
    """Read files from sandbox container, detect changes and deletions.

    Args:
        mount_path: Container path to scan (e.g. "/workspace" or "/workspace/data").
        scope_path: version tree prefix to prepend to relative paths.

    Returns:
        (modified, deleted) — modified is {version_path: content_bytes},
        deleted is [version_path, ...] for files removed from sandbox.
    """
    scan_path = mount_path or "/workspace"
    # Normalize scope_path: strip slashes to match version clone key format.
    scope_path = scope_path.strip("/") if scope_path else ""

    hash_result = await sandbox_service.exec(
        sandbox_session_id,
        f"find {scan_path} -type f -exec sha256sum {{}} \\; 2>/dev/null"
    )
    if not hash_result.get("success"):
        return {}, []

    original_hashes: dict[str, str] = {}
    for version_path, content in original_files.items():
        original_hashes[version_path] = hashlib.sha256(content).hexdigest()

    modified: dict[str, bytes] = {}
    seen_version_paths: set[str] = set()

    for line in (hash_result.get("output") or "").strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        current_hash, sandbox_path = parts

        if not sandbox_path.startswith(scan_path + "/"):
            continue

        relative = sandbox_path[len(scan_path) + 1:]
        if any(part.startswith(".") for part in relative.split("/")):
            continue

        version_path = f"{scope_path}/{relative}" if scope_path else relative
        seen_version_paths.add(version_path)

        if version_path in original_hashes and original_hashes[version_path] == current_hash:
            continue

        read_result = await sandbox_service.read_file(sandbox_session_id, sandbox_path)
        if not read_result.get("success"):
            continue

        content = read_result.get("content", "")
        if isinstance(content, (dict, list)):
            content_bytes = json.dumps(content, ensure_ascii=False, indent=2).encode("utf-8")
        elif isinstance(content, str):
            content_bytes = content.encode("utf-8")
        else:
            content_bytes = str(content).encode("utf-8")

        modified[version_path] = content_bytes

    # Detect deleted files: in original but no longer in sandbox
    deleted = [p for p in original_files if p not in seen_version_paths]

    return modified, deleted
