"""Business logic for real repository Scopes.

Responsibilities the repository deliberately does NOT have:
  - path canonicalization (mirror the version scope-state rules)
  - duplicate-scope rejection
  - bound-connector check on delete
  - auto-suggest from existing folder tree
"""

from __future__ import annotations

from typing import Optional

from postgrest.exceptions import APIError

from src.exceptions import AppException, ErrorCode, NotFoundException
from src.repo.models import RepositoryScope
from src.repo.scope_repository import RepositoryScopeRepository


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────

def _canonicalize_path(p: str) -> str:
    """Mirror the repository_scopes_path_canonical CHECK constraint:
      - no leading or trailing /
      - no // anywhere
    """
    if p is None:
        return ""
    s = p.strip()
    while s.startswith("/"):
        s = s[1:]
    while s.endswith("/"):
        s = s[:-1]
    while "//" in s:
        s = s.replace("//", "/")
    return s


# ──────────────────────────────────────────────────────────────────────────
# Service
# ──────────────────────────────────────────────────────────────────────────

class ScopeService:
    def __init__(self, repository: Optional[RepositoryScopeRepository] = None):
        self._repo = repository or RepositoryScopeRepository()

    # ── Reads ────────────────────────────────────────────────────────────

    def list_for_project(self, project_id: str) -> list[RepositoryScope]:
        return self._repo.list_by_project(project_id)

    def get(self, scope_id: str) -> Optional[RepositoryScope]:
        return self._repo.get(scope_id)

    def resolve_for_request(
        self, project_id: str, *, scope_id: Optional[str], request_path: Optional[str],
    ) -> Optional[RepositoryScope]:
        """Used by version_engine auth: resolve the scope a request operates on.

        Priority:
          1. Explicit scope_id query param.
          2. Path-prefix inference (longest matching path in project).
          3. None: the caller must represent Project root explicitly.
        """
        if scope_id:
            scope = self._repo.get(scope_id)
            if scope and scope.project_id == project_id:
                return scope
            return None
        if request_path:
            inferred = self._repo.find_by_path_prefix(project_id, request_path)
            if inferred is not None:
                return inferred
        return None

    # ── Writes ───────────────────────────────────────────────────────────

    def create(
        self,
        *,
        project_id: str,
        name: str,
        path: str,
        exclude: Optional[list[str]] = None,
        max_mode: str = "rw",
    ) -> RepositoryScope:
        canonical = _canonicalize_path(path)

        if canonical == "":
            raise AppException(
                code=ErrorCode.BAD_REQUEST,
                status_code=422,
                message="A Scope requires a non-empty repository path",
            )

        from src.platform.entitlements.service import EntitlementService
        from src.platform.project.repository import ProjectRepositorySupabase

        project = ProjectRepositorySupabase().get_by_id(project_id)
        if project is None:
            raise NotFoundException("Project not found")
        EntitlementService().require_capacity(
            project.org_id,
            "repo_scopes.max_per_project",
            current_count=len(self._repo.list_by_project(project_id)),
        )

        try:
            return self._repo.insert(
                project_id=project_id, name=name, path=canonical,
                exclude=list(exclude or []), max_mode=max_mode,
            )
        except APIError as exc:
            if exc.code == "0A000" and "native_scope_not_supported" in exc.message:
                raise AppException(code=ErrorCode.BAD_REQUEST, status_code=409,
                                   message="Scope is not available for native repositories yet",
                                   details={"code": "native_scope_not_supported"}) from exc
            raise

    def update(
        self,
        scope_id: str,
        *,
        name: Optional[str] = None,
        exclude: Optional[list[str]] = None,
        max_mode: Optional[str] = None,
    ) -> Optional[RepositoryScope]:
        # `path` is intentionally not in the update signature — renaming a
        # scope's path means deleting + recreating, by design.
        return self._repo.update(
            scope_id, name=name, exclude=exclude, max_mode=max_mode
        )

    def delete(
        self, scope_id: str, *, has_bound_connectors: Optional[bool] = None,
    ) -> None:
        """Delete a Scope.

        has_bound_connectors: if True, raises 409 with a "delete connectors first"
            hint. Caller passes the result of querying scope-bound entry points —
            we don't query it here to keep the service module decoupled from
            access surfaces and connections.
        """
        scope = self._repo.get(scope_id)
        if scope is None:
            raise NotFoundException("Scope not found")
        if has_bound_connectors:
            raise AppException(
                code=ErrorCode.BAD_REQUEST,
                status_code=409,
                message=(
                    "Scope has connectors bound to it. Delete those connectors "
                    "first, or use force-delete to remove them."
                ),
            )
        self._repo.delete(scope_id)

    # ── Auto-suggest ─────────────────────────────────────────────────────

    def auto_suggest_from_tree(
        self, project_id: str, top_level_folders: list[str],
    ) -> list[dict]:
        """Given the project's top-level folder names, propose new scopes
        for each folder NOT already covered by an existing scope.

        Returns a list of ScopeIn-shaped dicts. The router converts to
        Pydantic; we keep this layer plain for testability.
        """
        existing = {s.path for s in self._repo.list_by_project(project_id)}
        suggestions: list[dict] = []
        for folder in top_level_folders:
            canonical = _canonicalize_path(folder)
            if canonical in existing:
                continue
            suggestions.append({
                "name": _humanize(canonical) or "Folder",
                "path": canonical,
                "exclude": [],
                "max_mode": "rw",
            })
        return suggestions


def _humanize(path: str) -> str:
    """Turn 'src/handbook' → 'Src / Handbook'. Best-effort; user can rename."""
    if not path:
        return ""
    parts = [p.replace("_", " ").replace("-", " ").strip().title() for p in path.split("/")]
    return " / ".join(parts)
