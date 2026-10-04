"""
VersionTreeReader — direct read interface for Git-native project trees.

Reads legacy root-first trees directly from canonical Git tree objects in the
project ObjectStore. Admitted native reads use NativeTreeReader instead; they
must not enter this class's historical Scope compatibility paths.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.version_engine.domain.errors import (
    ObjectNotFoundError,
    PathNotFoundError,
    VersionReadError,
)
from src.version_engine.storage.object_store import ObjectStore
from src.version_engine.write_engine.git_object_format import EMPTY_TREE_SHA1, decode_tree

from src.infra.file_formats import detect_mime, detect_node_type
from src.version_engine.write_engine.path_utils import normalize_path
from src.utils.logger import log_error, log_warning

if TYPE_CHECKING:
    from src.version_engine.infrastructure.supabase.repo_manager import VersionRepoManager

# `detect_type` is re-exported (alias of `detect_node_type`) so the
# many existing imports of `tree_reader.detect_type` keep working.
# All format knowledge lives in `src.infra.file_formats`.
detect_type = detect_node_type
__all__ = [
    "detect_type",
    "detect_mime",
    "VersionBlobRead",
    "VersionEntry",
    "VersionTreeReader",
]


_ENTRYPOINT_FILE_NAMES = {"readme.md", "start here.md"}


@dataclass
class VersionEntry:
    """A single entry (file or directory) in a version tree."""
    name: str
    path: str
    type: str              # "folder" | "json" | "markdown" | "file"
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


class VersionTreeReader:
    """Read legacy trees from the acknowledged PG root and object store.

    Native authority or metadata failure must not turn into empty content.
    """

    def __init__(self, repo_manager: VersionRepoManager):
        self._repos = repo_manager

    def list_dir_in_scope(
        self,
        project_id: str,
        scope_path: str,
        path: str = "",
        *,
        include_size: bool = False,
    ) -> list[VersionEntry]:
        """List a scope-relative directory from the canonical project root."""

        try:
            repo, root_hash = self._scope_root_for_read(project_id, scope_path)
        except Exception as e:
            log_error(f"[VersionTreeReader] Failed to get scope root for {project_id}: {e}")
            raise VersionReadError(
                f"failed to read scope head for project {project_id}"
            ) from e
        if not root_hash:
            return []

        rel_path = normalize_path(path)
        tree_hash = root_hash
        if rel_path:
            tree_hash = self._navigate_to_subtree(repo.store, root_hash, rel_path)
            if not tree_hash:
                raise PathNotFoundError(f"directory not found: {rel_path}")

        try:
            entries = _read_tree(repo.store, tree_hash)
        except ObjectNotFoundError as exc:
            raise
        except Exception as e:
            log_error(f"[VersionTreeReader] Failed to read scope tree at {path}: {e}")
            raise VersionReadError(
                f"failed to read scope tree at {path or scope_path or 'root'}"
            ) from e

        integrity_by_hash = self._blob_integrity_statuses(repo.store, entries)
        display_path = _join_scope_path(scope_path, rel_path)
        result = [
            self._build_entry(
                repo.store, name, typ, hash_val, display_path,
                include_size=include_size,
                integrity_by_hash=integrity_by_hash,
            )
            for name, (typ, hash_val) in entries.items()
            if name != ".keep"
        ]
        result.sort(key=_entry_sort_key)
        return result

    def read_file_in_scope(self, project_id: str, scope_path: str, path: str) -> bytes:
        """Read a scope-relative file from the canonical project root."""

        try:
            repo, root_hash = self._scope_root_for_read(project_id, scope_path)
        except Exception as e:
            raise VersionReadError(f"failed to read scope head for project {project_id}") from e
        if not root_hash:
            raise FileNotFoundError(f"Scope {scope_path!r} has no content")

        blob_hash = self._resolve_blob(repo.store, root_hash, normalize_path(path))
        if not blob_hash:
            raise FileNotFoundError(f"File not found: {path}")
        return _read_blob(repo.store, blob_hash)

    def read_file_range_in_scope(
        self,
        project_id: str,
        scope_path: str,
        path: str,
        *,
        start: int = 0,
        limit: int | None = None,
    ) -> VersionBlobRead:
        """Read a byte range from a scope-relative file."""

        try:
            repo, root_hash = self._scope_root_for_read(project_id, scope_path)
        except Exception as e:
            raise VersionReadError(f"failed to read scope head for project {project_id}") from e
        if not root_hash:
            raise FileNotFoundError(f"Scope {scope_path!r} has no content")

        blob_hash = self._resolve_blob(repo.store, root_hash, normalize_path(path))
        if not blob_hash:
            raise FileNotFoundError(f"File not found: {path}")

        content = _read_blob(repo.store, blob_hash)
        if start <= 0 and limit is None:
            return VersionBlobRead(
                content=content,
                total_size=len(content),
                content_hash=blob_hash,
                ranged=False,
            )
        safe_start = max(0, start)
        end = len(content) if limit is None else min(len(content), safe_start + limit)
        return VersionBlobRead(
            content=content[safe_start:end],
            total_size=len(content),
            content_hash=blob_hash,
            ranged=safe_start > 0 or limit is not None,
        )

    def stat_in_scope(
        self,
        project_id: str,
        scope_path: str,
        path: str,
        *,
        include_size: bool = False,
    ) -> VersionEntry | None:
        """Stat a scope-relative path from the canonical project root."""

        scope_norm = normalize_path(scope_path)
        rel_path = normalize_path(path)
        try:
            repo, root_hash = self._scope_root_for_read(project_id, scope_norm)
        except Exception as e:
            log_error(f"[VersionTreeReader] Failed to get scope hash for stat: {e}")
            raise VersionReadError(f"failed to read scope head for project {project_id}") from e

        if not rel_path:
            return VersionEntry(
                name=os.path.basename(scope_norm),
                path=scope_norm,
                type="folder",
                size_bytes=0 if include_size else None,
            )
        if not root_hash:
            return None

        parent_path = os.path.dirname(rel_path)
        name = os.path.basename(rel_path)
        parent_hash = root_hash
        if parent_path:
            parent_hash = self._navigate_to_subtree(repo.store, root_hash, parent_path)
            if not parent_hash:
                return None

        try:
            entries = _read_tree(repo.store, parent_hash)
        except ObjectNotFoundError as exc:
            if _is_missing_object_error(exc):
                raise
            return None
        except Exception:
            return None
        if name not in entries:
            return None

        typ, hash_val = entries[name]
        display_path = _join_scope_path(scope_norm, rel_path)
        if typ == "T":
            child_count, integrity_status = self._count_children_with_integrity(
                repo.store, hash_val,
            )
            return VersionEntry(
                name=name,
                path=display_path,
                type="folder",
                size_bytes=0 if include_size else None,
                children_count=child_count,
                integrity_status=integrity_status,
            )
        return VersionEntry(
            name=name,
            path=display_path,
            type=detect_type(name),
            content_hash=hash_val,
            size_bytes=self._blob_size(repo.store, hash_val) if include_size else None,
            mime_type=detect_mime(name),
            integrity_status=self._object_integrity_status(repo.store, hash_val),
        )

    def list_tree_in_scope(
        self,
        project_id: str,
        scope_path: str,
        path: str = "",
        max_depth: int = -1,
        *,
        include_size: bool = False,
        max_entries: int | None = None,
    ) -> list[VersionEntry]:
        """Recursively list a scope from the canonical project root."""

        try:
            repo, root_hash = self._scope_root_for_read(project_id, scope_path)
        except Exception as e:
            log_error(f"[VersionTreeReader] Failed to get scope hash for list_tree: {e}")
            raise VersionReadError(
                f"failed to read scope head for project {project_id}"
            ) from e
        if not root_hash:
            return []

        rel_path = normalize_path(path)
        tree_hash = root_hash
        if rel_path:
            tree_hash = self._navigate_to_subtree(repo.store, root_hash, rel_path)
            if not tree_hash:
                raise PathNotFoundError(f"directory not found: {rel_path}")

        result: list[VersionEntry] = []
        self._walk_tree(
            repo.store,
            tree_hash,
            _join_scope_path(scope_path, rel_path),
            result,
            0,
            max_depth,
            include_size=include_size,
            max_entries=max_entries,
        )
        return result

    def list_dir(
        self, project_id: str, path: str = "", *, include_size: bool = False
    ) -> list[VersionEntry]:
        """List directory contents (similar to ls).

        Reads the Git tree object directly and returns a list of child entries.
        Empty path = project root directory.
        """
        try:
            repo, root_hash = self._project_root_for_read(project_id)
        except Exception as e:
            log_error(f"[VersionTreeReader] Failed to get root hash for {project_id}: {e}")
            raise VersionReadError(
                f"failed to read project root for project {project_id}"
            ) from e
        if not root_hash:
            return []

        tree_hash = root_hash
        if path:
            tree_hash = self._navigate_to_subtree(repo.store, root_hash, path)
            if not tree_hash:
                raise PathNotFoundError(f"directory not found: {path}")

        try:
            entries = _read_tree(repo.store, tree_hash)
        except ObjectNotFoundError as exc:
            raise
        except Exception as e:
            log_error(f"[VersionTreeReader] Failed to read tree at {path}: {e}")
            raise VersionReadError(
                f"failed to read tree at {path or 'project root'}"
            ) from e

        integrity_by_hash = self._blob_integrity_statuses(repo.store, entries)
        result = [
            self._build_entry(
                repo.store, name, typ, hash_val, path,
                include_size=include_size,
                integrity_by_hash=integrity_by_hash,
            )
            for name, (typ, hash_val) in entries.items()
            if name != ".keep"
        ]
        result.sort(key=_entry_sort_key)

        # A missing object is an integrity incident, not permission to rewrite
        # acknowledged metadata. Scope rows are not a complete inventory of a
        # root-first Project; rebuilding here can erase healthy siblings too.
        return result

    def _build_entry(
        self,
        store,
        name: str,
        typ: str,
        hash_val: str,
        parent_path: str,
        *,
        include_size: bool = False,
        integrity_by_hash: dict[str, str] | None = None,
    ) -> VersionEntry:
        entry_path = f"{parent_path}/{name}" if parent_path else name
        if typ == "T":
            child_count, integrity_status = self._count_children_with_integrity(
                store, hash_val,
            )
            return VersionEntry(
                name=name, path=entry_path, type="folder",
                size_bytes=0 if include_size else None,
                children_count=child_count,
                integrity_status=integrity_status,
            )
        return VersionEntry(
            name=name, path=entry_path, type=detect_type(name),
            content_hash=hash_val,
            size_bytes=self._blob_size(store, hash_val) if include_size else None,
            mime_type=detect_mime(name),
            integrity_status=(
                (integrity_by_hash or {}).get(hash_val)
                or self._object_integrity_status(store, hash_val)
            ),
        )

    def read_file(self, project_id: str, path: str) -> bytes:
        """Read file content."""
        try:
            repo, root_hash = self._project_root_for_read(project_id)
        except Exception as e:
            raise VersionReadError(f"failed to read project root for project {project_id}") from e
        if not root_hash:
            raise FileNotFoundError(f"Project {project_id} has no content")

        blob_hash = self._resolve_blob(repo.store, root_hash, path)
        if not blob_hash:
            raise FileNotFoundError(f"File not found: {path}")

        return _read_blob(repo.store, blob_hash)

    def read_file_range(
        self,
        project_id: str,
        path: str,
        *,
        start: int = 0,
        limit: int | None = None,
    ) -> VersionBlobRead:
        """Read a byte range from decoded file content.

        The storage backend keeps git loose-object bytes, not raw file bytes.
        Ranges must be applied after ObjectStore decodes the blob, otherwise
        callers would receive slices of zlib-framed object data.
        """
        try:
            repo, root_hash = self._project_root_for_read(project_id)
        except Exception as e:
            raise VersionReadError(f"failed to read project root for project {project_id}") from e
        if not root_hash:
            raise FileNotFoundError(f"Project {project_id} has no content")

        blob_hash = self._resolve_blob(repo.store, root_hash, path)
        if not blob_hash:
            raise FileNotFoundError(f"File not found: {path}")

        if start <= 0 and limit is None:
            content = _read_blob(repo.store, blob_hash)
            return VersionBlobRead(
                content=content,
                total_size=len(content),
                content_hash=blob_hash,
                ranged=False,
            )

        content = _read_blob(repo.store, blob_hash)
        safe_start = max(0, start)
        end = len(content) if limit is None else min(len(content), safe_start + limit)
        return VersionBlobRead(
            content=content[safe_start:end],
            total_size=len(content),
            content_hash=blob_hash,
            ranged=safe_start > 0 or limit is not None,
        )

    def stat(
        self, project_id: str, path: str, *, include_size: bool = False
    ) -> VersionEntry | None:
        """Get information for a single entry (similar to stat)."""
        try:
            repo, root_hash = self._project_root_for_read(project_id)
        except Exception as e:
            log_error(f"[VersionTreeReader] Failed to get root hash for stat: {e}")
            raise VersionReadError(f"failed to read project root for project {project_id}") from e
        if not root_hash:
            return None

        if not path:
            return VersionEntry(
                name="", path="", type="folder",
                size_bytes=0 if include_size else None,
            )

        parent_path = os.path.dirname(path)
        name = os.path.basename(path)

        parent_hash = root_hash
        if parent_path:
            parent_hash = self._navigate_to_subtree(repo.store, root_hash, parent_path)
            if not parent_hash:
                return None

        try:
            entries = _read_tree(repo.store, parent_hash)
        except ObjectNotFoundError as exc:
            if _is_missing_object_error(exc):
                raise
            return None
        except Exception:
            return None

        if name not in entries:
            return None

        typ, hash_val = entries[name]

        if typ == "T":
            child_count, integrity_status = self._count_children_with_integrity(
                repo.store, hash_val,
            )
            return VersionEntry(
                name=name,
                path=path,
                type="folder",
                size_bytes=0 if include_size else None,
                children_count=child_count,
                integrity_status=integrity_status,
            )

        return VersionEntry(
            name=name,
            path=path,
            type=detect_type(name),
            content_hash=hash_val,
            size_bytes=self._blob_size(repo.store, hash_val) if include_size else None,
            mime_type=detect_mime(name),
            integrity_status=self._object_integrity_status(repo.store, hash_val),
        )

    def list_tree(
        self,
        project_id: str,
        path: str = "",
        max_depth: int = -1,
        *,
        include_size: bool = False,
        max_entries: int | None = None,
    ) -> list[VersionEntry]:
        """Recursively list the directory tree (for a full tree view).

        max_depth = -1 means unlimited recursion.
        max_entries limits returned entries to protect object-store backed
        scopes from unbounded recursive walks.
        """
        try:
            repo, root_hash = self._project_root_for_read(project_id)
        except Exception as e:
            log_error(f"[VersionTreeReader] Failed to get root hash for list_tree: {e}")
            raise VersionReadError(
                f"failed to read project root for project {project_id}"
            ) from e
        if not root_hash:
            return []

        tree_hash = root_hash
        if path:
            tree_hash = self._navigate_to_subtree(repo.store, root_hash, path)
            if not tree_hash:
                raise PathNotFoundError(f"directory not found: {path}")

        result: list[VersionEntry] = []
        self._walk_tree(
            repo.store, tree_hash, path, result, 0, max_depth,
            include_size=include_size,
            max_entries=max_entries,
        )
        return result

    def exists(self, project_id: str, path: str) -> bool:
        """Check whether a path exists."""
        return self.stat(project_id, path) is not None

    def get_root_hash(self, project_id: str) -> str:
        """Get the current root hash of the project."""
        try:
            _repo, root_hash = self._project_root_for_read(project_id)
            return root_hash or ""
        except Exception as e:
            log_error(f"[VersionTreeReader] Failed to get root hash: {e}")
            raise VersionReadError(f"failed to read project root for project {project_id}") from e

    def get_head_commit_id(self, project_id: str) -> str:
        """Get the project's current global head commit_id (may be empty).

        Returns the commit_id of the most recent commit across all scopes.
        """
        try:
            repo = self._repos.get_repo(project_id)
            return repo.history.get_head_commit_id() or ""
        except Exception as e:
            log_error(f"[VersionTreeReader] Failed to get head commit_id: {e}")
            raise VersionReadError(f"failed to read project head for project {project_id}") from e

    # ── Internal helpers ──

    def _project_root_for_read(self, project_id: str):
        """Return the canonical project root hash for reads.

        Root is the source of truth. The repair branch is legacy-only: older
        scoped pushes could land scope_state before root projection completed.
        Reads repair that historical shape once rather than treating
        scope_state as a second authority.
        """

        get_repo = getattr(self._repos, "get_repo", None)
        if callable(get_repo):
            repo = get_repo(project_id)
        else:
            repo = self._repos.get_server_repo(project_id)
        history = getattr(repo, "history", None)
        root_hash = history.get_root_hash() if history is not None else ""
        root_hash = root_hash or ""
        if root_hash and root_hash != EMPTY_TREE_SHA1:
            return repo, root_hash

        repaired = self._repair_project_root_from_scope_state(
            project_id,
            root_hash,
        )
        return repo, repaired or root_hash

    def _scope_root_for_read(self, project_id: str, scope_path: str):
        """Return the tree hash for ``scope_path`` derived from project root."""

        repo, project_root = self._project_root_for_read(project_id)
        scope_norm = normalize_path(scope_path)
        if not scope_norm:
            return repo, project_root

        if project_root:
            scope_tree = self._navigate_to_subtree(repo.store, project_root, scope_norm)
            if scope_tree:
                return repo, scope_tree

        # Legacy fallback only. This covers pre-root-first projects whose root
        # has not been repaired yet; new writes must keep this cache in sync
        # with the accepted root.
        get_server_repo = getattr(self._repos, "get_server_repo", None)
        if callable(get_server_repo):
            try:
                server_repo = get_server_repo(project_id)
                legacy_hash = server_repo.get_scope_hash(scope_norm) or ""
                if legacy_hash:
                    return server_repo, legacy_hash
            except Exception:
                pass
        return repo, ""

    def _repair_project_root_from_scope_state(
        self,
        project_id: str,
        current_root_hash: str,
    ) -> str:
        get_server_repo = getattr(self._repos, "get_server_repo", None)
        if not callable(get_server_repo):
            return ""

        try:
            server_repo = get_server_repo(project_id)
            scope_hashes = server_repo.get_all_scope_hashes()
        except Exception as exc:
            log_warning(
                f"[VersionTreeReader] could not load scope state for "
                f"root repair project={project_id}: {exc}",
            )
            return ""

        if not any(scope_hashes.values()):
            return ""

        try:
            from src.version_engine.derived.projection import build_root_from_scope_state

            derived_root = build_root_from_scope_state(server_repo, "", "")
        except Exception as exc:
            log_warning(
                f"[VersionTreeReader] root repair build failed "
                f"project={project_id}: {exc}",
            )
            return ""

        if not derived_root or derived_root == current_root_hash:
            return ""

        try:
            cas_update = getattr(server_repo, "cas_update_root_hash", None)
            if callable(cas_update):
                cas_update(current_root_hash or "", derived_root)
        except Exception as exc:
            # A CAS loser can still serve the derived root for this read; the
            # next request will either observe the winner or try repair again.
            log_warning(
                f"[VersionTreeReader] root repair CAS failed "
                f"project={project_id}: {exc}",
            )

        return derived_root

    def _navigate_to_subtree(
        self, store: ObjectStore, root_hash: str, path: str
    ) -> str | None:
        parts = [p for p in path.split("/") if p]
        current = root_hash
        for part in parts:
            try:
                entries = _read_tree(store, current)
            except ObjectNotFoundError as exc:
                raise
            except Exception as exc:
                raise VersionReadError(
                    f"failed to navigate tree path {path}"
                ) from exc
            if part not in entries:
                return None
            typ, h = entries[part]
            if typ != "T":
                return None
            current = h
        return current

    def _resolve_blob(
        self, store: ObjectStore, root_hash: str, path: str
    ) -> str | None:
        """Navigate directly to a blob by path — O(depth) not O(total files)."""
        if not root_hash:
            return None
        parts = [p for p in path.split("/") if p]
        if not parts:
            return None
        try:
            current = root_hash
            for part in parts[:-1]:
                entries = _read_tree(store, current)
                if part not in entries:
                    return None
                typ, h = entries[part]
                if typ != "T":
                    return None
                current = h
            entries = _read_tree(store, current)
            leaf = parts[-1]
            if leaf not in entries:
                return None
            typ, h = entries[leaf]
            if typ == "T":
                return None
            return h
        except ObjectNotFoundError as exc:
            if _is_missing_object_error(exc):
                raise
            return None
        except Exception:
            return None

    def _count_children(self, store: ObjectStore, tree_hash: str) -> int | None:
        count, _integrity_status = self._count_children_with_integrity(
            store, tree_hash,
        )
        return count

    def _count_children_with_integrity(
        self,
        store: ObjectStore,
        tree_hash: str,
    ) -> tuple[int | None, str]:
        try:
            entries = _read_tree(store, tree_hash)
            return sum(1 for name in entries if name != ".keep"), "ok"
        except ObjectNotFoundError as exc:
            # `ls parent/` should only require the parent tree itself. Git can
            # list an entry whose child tree is missing/corrupt; the integrity
            # error belongs to `ls parent/child/`, not to the parent listing.
            # Return an unknown count here so the UI can still show sibling
            # files and fail loudly only when the broken child is expanded.
            if _is_missing_object_error(exc):
                return None, "damaged"
            return None, "unknown"
        except Exception:
            return None, "unknown"

    def _object_integrity_status(self, store: ObjectStore, object_hash: str) -> str:
        try:
            return "ok" if store.exists(object_hash) else "damaged"
        except Exception:
            return "unknown"

    def _blob_integrity_statuses(
        self,
        store: ObjectStore,
        entries: dict,
    ) -> dict[str, str]:
        blob_hashes = [
            hash_val
            for typ, hash_val in entries.values()
            if typ == "B"
        ]
        if not blob_hashes:
            return {}
        try:
            existing = store.exists_many(blob_hashes)
        except Exception:
            return {hash_val: "unknown" for hash_val in blob_hashes}
        return {
            hash_val: "ok" if hash_val in existing else "damaged"
            for hash_val in blob_hashes
        }

    def _blob_size(self, store: ObjectStore, blob_hash: str) -> int:
        try:
            return len(_read_blob(store, blob_hash))
        except Exception:
            return 0

    def _walk_tree(
        self,
        store: ObjectStore,
        tree_hash: str,
        prefix: str,
        result: list[VersionEntry],
        depth: int,
        max_depth: int,
        *,
        include_size: bool = False,
        max_entries: int | None = None,
    ) -> None:
        if max_entries is not None and len(result) >= max_entries:
            return
        if max_depth >= 0 and depth > max_depth:
            return

        try:
            entries = _read_tree(store, tree_hash)
        except ObjectNotFoundError as exc:
            if _is_missing_object_error(exc):
                raise
            return
        except Exception as exc:
            if depth == 0:
                raise VersionReadError(
                    f"failed to read tree at {prefix or 'project root'}"
                ) from exc
            return

        for name, (typ, hash_val) in sorted(entries.items()):
            if name == ".keep":
                continue
            if max_entries is not None and len(result) >= max_entries:
                return

            entry_path = f"{prefix}/{name}" if prefix else name

            if typ == "T":
                child_count, integrity_status = self._count_children_with_integrity(
                    store, hash_val,
                )
                result.append(VersionEntry(
                    name=name,
                    path=entry_path,
                    type="folder",
                    size_bytes=0 if include_size else None,
                    children_count=child_count,
                    integrity_status=integrity_status,
                ))
                if integrity_status == "ok":
                    self._walk_tree(
                        store, hash_val, entry_path, result, depth + 1, max_depth,
                        include_size=include_size,
                        max_entries=max_entries,
                    )
            else:
                result.append(VersionEntry(
                    name=name,
                    path=entry_path,
                    type=detect_type(name),
                    content_hash=hash_val,
                    size_bytes=self._blob_size(store, hash_val) if include_size else None,
                    mime_type=detect_mime(name),
                    integrity_status=self._object_integrity_status(store, hash_val),
                ))


def _join_scope_path(scope_path: str, rel_path: str) -> str:
    scope = normalize_path(scope_path)
    rel = normalize_path(rel_path)
    if not scope:
        return rel
    if not rel:
        return scope
    return f"{scope}/{rel}"


def _read_tree(store: ObjectStore, tree_hash: str) -> dict:
    """Read a canonical Git tree object."""
    if not tree_hash:
        return {}
    obj_type, content = store.get_object(tree_hash)
    if obj_type != "tree":
        raise ValueError(f"object {tree_hash} is a {obj_type}, expected tree")
    return {
        entry.name: ["T" if entry.is_dir else "B", entry.sha1_hex]
        for entry in decode_tree(content)
    }


def _read_blob(store: ObjectStore, blob_hash: str) -> bytes:
    """Read decoded bytes from a canonical Git blob object."""
    return store.get(blob_hash)


def _is_missing_object_error(exc: ObjectNotFoundError) -> bool:
    return "not found" in str(exc).lower()
