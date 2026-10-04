"""Read-only product tree projection of one admitted native repository snapshot.

No legacy root/Scope fallback, repair, transport materialization or mutable cache.
The caller owns the snapshot lifetime. Gitlinks are external; symlinks are blobs,
never filesystem traversal. Raw names and modes remain intact in the projection.
"""
from __future__ import annotations

import base64

from src.version_engine.domain.errors import PathNotFoundError
from src.version_engine.read.repository_snapshot import RepositorySnapshot
from src.version_engine.read.tree_reader import (
    VersionBlobRead,
    VersionEntry,
    detect_mime,
    detect_type,
)
from src.version_engine.write_engine.git_object_format import TreeEntry, decode_tree


class NativeTreeReader:
    def __init__(self, snapshot: RepositorySnapshot, *, selector: bytes = b"HEAD"):
        self.snapshot = snapshot
        self.revision = snapshot.revision(selector)

    def _check_project(self, project_id):
        if project_id != self.snapshot.project_id:
            raise PermissionError("repository read Project binding mismatch")
        self.snapshot.check_live()

    def _tree(self, oid):
        self.snapshot.check_live()
        if oid == self.snapshot.empty_tree:
            return []
        kind, body = self.snapshot.object(oid)
        if kind != "tree":
            raise ValueError("expected a repository tree")
        return decode_tree(body, object_format=self.snapshot.object_format)

    def _entry(self, path) -> TreeEntry | None:
        parts = [part for part in path.split("/") if part]
        oid = self.revision.tree_oid
        for i, part in enumerate(parts):
            entry = next((e for e in self._tree(oid) if e.name == part), None)
            if entry is None:
                return None
            if i == len(parts) - 1:
                return entry
            if not entry.is_dir:
                return None
            oid = entry.sha1_hex
        return None

    def _directory(self, path):
        if not path:
            return self.revision.tree_oid
        entry = self._entry(path)
        if entry is None or not entry.is_dir:
            raise PathNotFoundError(f"directory not found: {path}")
        return entry.sha1_hex

    def _blob(self, oid):
        kind, body = self.snapshot.object(oid)
        if kind != "blob":
            raise ValueError("expected a repository blob")
        return body

    def _project(self, entry, parent_path, *, include_size=False):
        path = f"{parent_path}/{entry.name}" if parent_path else entry.name
        if entry.is_dir:
            return VersionEntry(name=entry.name, path=path, type="folder",
                                children_count=len(self._tree(entry.sha1_hex)),
                                size_bytes=0 if include_size else None,
                                git_mode=entry.mode.decode("ascii"))
        if entry.is_gitlink:
            return VersionEntry(name=entry.name, path=path, type="gitlink", content_hash=entry.sha1_hex,
                                git_mode=entry.mode.decode("ascii"))
        return VersionEntry(name=entry.name, path=path, type=detect_type(entry.name),
                            content_hash=entry.sha1_hex, mime_type=detect_mime(entry.name),
                            size_bytes=len(self._blob(entry.sha1_hex)) if include_size else None,
                            integrity_status="ok" if include_size else "unknown",
                            git_mode=entry.mode.decode("ascii"))

    def get_root_hash(self, project_id):
        self._check_project(project_id)
        return self.revision.tree_oid

    def get_head_commit_id(self, project_id):
        self._check_project(project_id)
        return self.revision.commit_oid or ""

    def get_read_revision(self, project_id):
        self._check_project(project_id)
        revision = self.revision
        try:
            target_ref = revision.ref_name.decode("utf-8")
        except UnicodeDecodeError:
            target_ref = None
        return {
            "repository_profile": "native", "object_format": self.snapshot.object_format,
            "generation": self.snapshot.generation, "ref_sequence": self.snapshot.ref_sequence,
            "target_ref": target_ref, "target_ref_b64": base64.b64encode(revision.ref_name).decode(),
            "expected_oid": revision.expected.oid, "tree_oid": revision.tree_oid,
            "head_guard": revision.head_guard.wire(self.snapshot.object_format) if revision.head_guard else None,
        }

    def get_scope_head_commit_id_for_path(self, project_id, path):
        self._check_project(project_id)
        # A Project snapshot is not a Scope-lineage mapping. Do not report a
        # legacy historical Scope row as the base of this native tree.
        return ""

    def list_dir(self, project_id, path="", *, include_size=False):
        self._check_project(project_id)
        return [self._project(entry, path, include_size=include_size)
                for entry in self._tree(self._directory(path))]

    def stat(self, project_id, path, *, include_size=False):
        self._check_project(project_id)
        if not path:
            return VersionEntry(name="", path="", type="folder", git_mode="40000")
        entry = self._entry(path)
        if entry is None:
            return None
        parent = path.rpartition("/")[0]
        return self._project(entry, parent, include_size=include_size)

    def read_file(self, project_id, path):
        self._check_project(project_id)
        entry = self._entry(path)
        if entry is None or entry.is_dir or entry.is_gitlink:
            raise FileNotFoundError(f"File not found: {path}")
        return self._blob(entry.sha1_hex)

    def read_file_range(self, project_id, path, *, start=0, limit=None):
        self._check_project(project_id)
        if start < 0 or (limit is not None and limit < 0):
            raise ValueError("negative file range")
        entry = self._entry(path)
        if entry is None or entry.is_dir or entry.is_gitlink:
            raise FileNotFoundError(f"File not found: {path}")
        body = self._blob(entry.sha1_hex)
        return VersionBlobRead(content=body[start:None if limit is None else start + limit],
                               total_size=len(body), content_hash=entry.sha1_hex,
                               ranged=start > 0 or limit is not None)

    def list_tree(self, project_id, path="", max_depth=-1, *, include_size=False, max_entries=100_000):
        self._check_project(project_id)
        if max_entries is None or max_entries < 1:
            raise ValueError("native tree enumeration requires a positive entry budget")
        result = []
        stack = [(iter(self._tree(self._directory(path))), path, 0)]
        while stack:
            iterator, prefix, depth = stack[-1]
            entry = next(iterator, None)
            if entry is None:
                stack.pop()
                continue
            if len(result) >= max_entries:
                raise ValueError("native tree entry budget exceeded")
            projected = self._project(entry, prefix, include_size=include_size)
            result.append(projected)
            if entry.is_dir and (max_depth < 0 or depth < max_depth):
                stack.append((iter(self._tree(entry.sha1_hex)), projected.path, depth + 1))
        return result
