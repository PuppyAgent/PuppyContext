"""GitHub Synchronize service — connect/disconnect/import/export orchestration.

Sits between the router (HTTP shape) and the repository / importer /
exporter (work units). Keeps DB writes + side-effects in one place
so the router stays thin.
"""
from __future__ import annotations

from src.platform.synchronize.github.exporter import export_to_branch
from src.platform.synchronize.github.importer import _load_oauth_token, import_branch
from src.platform.synchronize.github.repository import (
    GithubSyncLogRepository,
    GithubSyncRepository,
)
from src.platform.synchronize.github.schemas import (
    GithubBranchList,
    GithubBranchSummary,
    GithubExportRequest,
    GithubImportRequest,
    GithubIntegrationCreate,
    GithubIntegrationStatus,
    GithubIntegrationUpdate,
    GithubRepoList,
    GithubRepoSummary,
    GithubSyncLogEntry,
    GithubSyncLogList,
    GithubSyncRunResult,
)
from src.provider.github.client import GithubApi
from src.utils.logger import log_info


class GithubSyncNotFound(Exception):
    pass


class GithubSyncService:
    """Stateless façade. One instance per request is fine; all heavy
    state lives in the repositories."""

    def __init__(self):
        self._bindings = GithubSyncRepository()
        self._sync_log = GithubSyncLogRepository()

    # ── connect / disconnect ──────────────────────

    async def connect(self, project_id: str,
                      payload: GithubIntegrationCreate) -> GithubIntegrationStatus:
        # Cross-check the schema-level invariant ahead of the DB so we
        # surface a clean 400 instead of a Postgres CHECK violation.
        if payload.auto_pull and not payload.webhook_secret:
            raise ValueError(
                "auto_pull requires a webhook_secret to verify deliveries"
            )

        body = {
            "oauth_connection_id": payload.oauth_connection_id,
            "github_repo_owner": payload.github_repo_owner,
            "github_repo_name": payload.github_repo_name,
            "default_branch": payload.default_branch,
            "auto_pull": payload.auto_pull,
            "webhook_secret": payload.webhook_secret,
        }
        row = await self._bindings.upsert(project_id, body)
        log_info(
            f"[GithubIntegration] connect project={project_id} "
            f"repo={payload.github_repo_owner}/{payload.github_repo_name} "
            f"branch={payload.default_branch}"
        )
        return _row_to_status(row)

    async def update(self, project_id: str,
                     payload: GithubIntegrationUpdate) -> GithubIntegrationStatus:
        existing = await self._bindings.get_by_project(project_id)
        if not existing:
            raise GithubSyncNotFound(project_id)
        merged = {**existing}
        for field, value in payload.model_dump(exclude_unset=True).items():
            merged[field] = value
        if merged.get("auto_pull") and not merged.get("webhook_secret"):
            raise ValueError(
                "auto_pull requires a webhook_secret to verify deliveries"
            )
        row = await self._bindings.upsert(project_id, {
            k: merged[k] for k in (
                "oauth_connection_id", "github_repo_owner", "github_repo_name",
                "default_branch", "auto_pull", "webhook_secret",
            ) if k in merged
        })
        return _row_to_status(row)

    async def disconnect(self, project_id: str) -> bool:
        existed = await self._bindings.delete_by_project(project_id)
        if existed:
            log_info(f"[GithubIntegration] disconnect project={project_id}")
        return existed

    async def status(self, project_id: str) -> GithubIntegrationStatus | None:
        row = await self._bindings.get_by_project(project_id)
        return _row_to_status(row) if row else None

    # ── repo discovery (for the UI picker) ────────

    async def list_user_repos(self, oauth_connection_id: int) -> GithubRepoList:
        oauth = await _load_oauth_token(oauth_connection_id)
        if not oauth:
            raise GithubSyncNotFound(
                f"oauth_connection {oauth_connection_id}"
            )
        api = GithubApi(oauth["access_token"])
        try:
            repos = await api.list_user_repos()
        finally:
            await api.aclose()
        return GithubRepoList(repos=[
            GithubRepoSummary(
                owner=(r.get("owner") or {}).get("login", ""),
                name=r.get("name", ""),
                full_name=r.get("full_name", ""),
                default_branch=r.get("default_branch", "main"),
                private=bool(r.get("private", False)),
            )
            for r in repos
        ])

    async def list_repo_branches(
        self, oauth_connection_id: int, owner: str, name: str,
    ) -> GithubBranchList:
        """List branches for a repo so the UI picker can populate its
        dropdown. The default branch is flagged inline so the picker
        can pre-select it without a second round-trip."""
        oauth = await _load_oauth_token(oauth_connection_id)
        if not oauth:
            raise GithubSyncNotFound(
                f"oauth_connection {oauth_connection_id}"
            )
        api = GithubApi(oauth["access_token"])
        try:
            # We need both the branch list and the repo metadata to flag
            # which branch is default. ``get_branch_head`` per branch
            # would be N+1; instead, hit ``/repos/{owner}/{name}`` once.
            raw_branches = await api.list_branches(owner, name)
            repo_meta = await api.get_repo_meta(owner, name)
            default_branch = repo_meta.get("default_branch") or ""
        finally:
            await api.aclose()
        return GithubBranchList(
            repo_owner=owner,
            repo_name=name,
            branches=[
                GithubBranchSummary(
                    name=b.get("name", ""),
                    sha=(b.get("commit") or {}).get("sha", ""),
                    protected=bool(b.get("protected", False)),
                    is_default=b.get("name", "") == default_branch,
                )
                for b in raw_branches
            ],
        )

    # ── sync triggers ─────────────────────────────

    async def pull(self, project_id: str, payload: GithubImportRequest) -> tuple[str, GithubSyncRunResult]:
        binding = await self._bindings.get_by_project(project_id)
        if not binding:
            raise GithubSyncNotFound(project_id)
        # Capture the actual execution identity, not a second Project lookup.
        binding_id = binding["id"]
        result = await import_branch(
            binding, branch=payload.branch, force=payload.force, triggered_by="manual",
        )
        return binding_id, result

    async def push(self, project_id: str, payload: GithubExportRequest) -> tuple[str, GithubSyncRunResult]:
        binding = await self._bindings.get_by_project(project_id)
        if not binding:
            raise GithubSyncNotFound(project_id)
        binding_id = binding["id"]
        result = await export_to_branch(
            binding, branch=payload.branch, message=payload.message, triggered_by="manual",
        )
        return binding_id, result

    async def import_now(self, project_id: str, payload: GithubImportRequest) -> GithubSyncRunResult:
        return (await self.pull(project_id, payload))[1]

    async def export_now(self, project_id: str, payload: GithubExportRequest) -> GithubSyncRunResult:
        return (await self.push(project_id, payload))[1]

    # ── sync log read ─────────────────────────────

    async def list_sync_log(self, project_id: str, *,
                            limit: int = 50, offset: int = 0) -> GithubSyncLogList:
        binding = await self._bindings.get_by_project(project_id)
        if not binding:
            raise GithubSyncNotFound(project_id)
        rows, total = await self._sync_log.list_recent(
            binding["id"], limit=limit, offset=offset,
        )
        return GithubSyncLogList(
            synchronize_github_binding_id=binding["id"],
            entries=[GithubSyncLogEntry(**r) for r in rows],
            total=total,
        )


def _row_to_status(row: dict) -> GithubIntegrationStatus:
    return GithubIntegrationStatus(
        id=row["id"],
        project_id=row["project_id"],
        oauth_connection_id=row.get("oauth_connection_id"),
        github_repo_owner=row.get("github_repo_owner", ""),
        github_repo_name=row.get("github_repo_name", ""),
        default_branch=row.get("default_branch", "main"),
        auto_pull=bool(row["auto_pull"]),
        has_webhook_secret=bool(row.get("webhook_secret")),
        last_pulled_sha=row.get("last_pulled_sha"),
        last_pulled_at=row.get("last_pulled_at"),
        last_pushed_sha=row.get("last_pushed_sha"),
        last_pushed_at=row.get("last_pushed_at"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
