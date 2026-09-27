"""Thin async wrapper around the GitHub REST API.

Just enough surface to drive the import + export flows:

* :meth:`list_user_repos`  — for the project-settings UI's repo picker.
* :meth:`get_branch_head`  — current commit SHA on a branch.
* :meth:`get_tree_recursive` — flat list of (path, blob_sha) at a commit's
  tree.
* :meth:`get_blob_content`  — raw bytes of a blob (decodes base64).
* :meth:`create_blob` / :meth:`create_tree` / :meth:`create_commit` /
  :meth:`update_ref`  — for export.

Token comes from ``oauth_connections.access_token`` via
``GithubOAuthService``. We don't try to be cute about token refresh
here — the OAuth service has the canonical implementation and we just
pull a fresh token whenever the API returns 401.

Errors
------
GitHub returns structured error JSON on 4xx/5xx. We raise
:class:`GithubApiError` carrying the HTTP status, the documentation
URL (when present), and the user-facing message.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Optional

import httpx

from src.utils.logger import log_warning


_GITHUB_BASE = "https://api.github.com"
_DEFAULT_TIMEOUT = 30


class GithubApiError(Exception):
    """Surfaced when the GitHub API returns a non-2xx response."""

    def __init__(self, status: int, message: str, doc_url: Optional[str] = None):
        self.status = status
        self.doc_url = doc_url
        super().__init__(f"GitHub API {status}: {message}")


@dataclass
class TreeEntry:
    path: str
    sha: str
    mode: str  # GitHub's git-mode string, e.g. "100644"
    type: str  # 'blob' or 'tree'
    size: Optional[int] = None


class GithubApi:
    """Per-token API client. Cheap to construct; reuse across calls
    within one request handler so the underlying httpx client stays
    open."""

    def __init__(self, access_token: str, *, timeout: float = _DEFAULT_TIMEOUT):
        self._token = access_token
        self._client = httpx.AsyncClient(
            base_url=_GITHUB_BASE,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self._client.aclose()

    async def aclose(self):
        await self._client.aclose()

    # ── reads ─────────────────────────────────────

    async def list_user_repos(self, *, per_page: int = 100,
                              max_pages: int = 5) -> list[dict]:
        """Return the authenticated user's repos (excluding orgs they're
        a member of but don't own, by default — GitHub's behaviour for
        ``/user/repos?affiliation=owner``). Supports rich filtering on
        the GitHub side; we keep it simple for the picker.
        """
        repos: list[dict] = []
        for page in range(1, max_pages + 1):
            r = await self._client.get(
                "/user/repos",
                params={"per_page": per_page, "page": page,
                        "sort": "updated", "affiliation": "owner,collaborator,organization_member"},
            )
            self._raise_for_status(r)
            batch = r.json() or []
            repos.extend(batch)
            if len(batch) < per_page:
                break
        return repos

    async def get_repo_meta(self, owner: str, repo: str) -> dict:
        """Return the repo's metadata blob (default_branch, private,
        permissions, ...). One round-trip; the branch picker uses this
        to flag the GitHub-side default branch in the dropdown without
        a per-branch ``get_branch_head`` N+1.
        """
        r = await self._client.get(f"/repos/{owner}/{repo}")
        self._raise_for_status(r)
        return r.json()

    async def get_branch_head(self, owner: str, repo: str, branch: str) -> dict:
        r = await self._client.get(
            f"/repos/{owner}/{repo}/branches/{branch}",
        )
        self._raise_for_status(r)
        return r.json()

    async def list_branches(
        self, owner: str, repo: str, *,
        per_page: int = 100, max_pages: int = 5,
    ) -> list[dict]:
        """Return the repo's branches.

        GitHub's ``/repos/{owner}/{repo}/branches`` is paginated and the
        large monorepos easily exceed 100 entries, so we walk pages until
        we either drain the list or hit ``max_pages`` (which caps the
        worst-case latency at ``max_pages × per_page_round_trip`` and
        keeps the picker UI snappy).
        """
        branches: list[dict] = []
        for page in range(1, max_pages + 1):
            r = await self._client.get(
                f"/repos/{owner}/{repo}/branches",
                params={"per_page": per_page, "page": page},
            )
            self._raise_for_status(r)
            batch = r.json() or []
            branches.extend(batch)
            if len(batch) < per_page:
                break
        return branches

    async def get_tree_recursive(
        self, owner: str, repo: str, tree_sha: str,
    ) -> tuple[list[TreeEntry], bool]:
        """Return ``(entries, truncated)``. GitHub caps the recursive
        tree response at ~100k entries; the truncated flag tells us we
        need to fall back to per-directory paging (not implemented in
        the MVP — the importer surfaces a clear error)."""
        r = await self._client.get(
            f"/repos/{owner}/{repo}/git/trees/{tree_sha}",
            params={"recursive": "1"},
        )
        self._raise_for_status(r)
        data = r.json()
        entries = [
            TreeEntry(
                path=e["path"], sha=e["sha"], mode=e["mode"],
                type=e["type"], size=e.get("size"),
            )
            for e in data.get("tree", [])
        ]
        return entries, bool(data.get("truncated", False))

    async def get_blob_content(self, owner: str, repo: str, sha: str) -> bytes:
        """Return raw blob bytes. Skips LFS pointer detection — the
        importer is responsible for that decision since LFS handling
        depends on user policy.
        """
        r = await self._client.get(f"/repos/{owner}/{repo}/git/blobs/{sha}")
        self._raise_for_status(r)
        data = r.json()
        encoding = data.get("encoding", "base64")
        if encoding == "base64":
            return base64.b64decode(data["content"])
        if encoding == "utf-8":
            return data["content"].encode("utf-8")
        raise GithubApiError(500, f"unsupported blob encoding: {encoding}")

    # ── writes ────────────────────────────────────

    async def create_blob(self, owner: str, repo: str, content: bytes) -> str:
        r = await self._client.post(
            f"/repos/{owner}/{repo}/git/blobs",
            json={
                "content": base64.b64encode(content).decode("ascii"),
                "encoding": "base64",
            },
        )
        self._raise_for_status(r)
        return r.json()["sha"]

    async def create_tree(
        self, owner: str, repo: str,
        tree_entries: list[dict],
        base_tree: Optional[str] = None,
    ) -> str:
        """``tree_entries`` shape per GitHub docs:
        ``[{"path": ..., "mode": "100644", "type": "blob", "sha": ...}]``.
        If *base_tree* is set, GitHub layers your entries on top of it
        (any path you specify replaces the base; everything else is
        inherited). Pass ``None`` for "this is the entire tree".
        """
        body: dict = {"tree": tree_entries}
        if base_tree:
            body["base_tree"] = base_tree
        r = await self._client.post(
            f"/repos/{owner}/{repo}/git/trees", json=body,
        )
        self._raise_for_status(r)
        return r.json()["sha"]

    async def create_commit(
        self, owner: str, repo: str, *,
        message: str, tree_sha: str, parent_shas: list[str],
        author_name: str = "Puppyone", author_email: str = "noreply@puppyone",
    ) -> str:
        r = await self._client.post(
            f"/repos/{owner}/{repo}/git/commits",
            json={
                "message": message,
                "tree": tree_sha,
                "parents": parent_shas,
                "author": {"name": author_name, "email": author_email},
            },
        )
        self._raise_for_status(r)
        return r.json()["sha"]

    async def update_ref(
        self, owner: str, repo: str, ref: str, sha: str, *,
        force: bool = False,
    ) -> None:
        """Move a ref (e.g. ``heads/main``) to point at *sha*. ``force``
        is git's ``+`` push — only set it if you know what you're
        doing (lost commits are not recoverable through the API)."""
        r = await self._client.patch(
            f"/repos/{owner}/{repo}/git/refs/{ref}",
            json={"sha": sha, "force": force},
        )
        self._raise_for_status(r)

    # ── helpers ───────────────────────────────────

    def _raise_for_status(self, r: httpx.Response) -> None:
        if r.is_success:
            return
        try:
            payload = r.json()
        except Exception:
            payload = {}
        message = (payload.get("message") if isinstance(payload, dict)
                   else None) or r.text or "GitHub API error"
        doc = (payload.get("documentation_url")
               if isinstance(payload, dict) else None)
        if r.status_code == 401:
            # 401 from GitHub almost always means the stored OAuth token
            # was revoked / expired. Log loudly with hint so ops can act
            # (today: tell user to re-authorize; future: trigger refresh
            # flow against ``oauth_connections.refresh_token``).
            log_warning(
                f"[GithubApi] 401 from GitHub — access token likely "
                f"revoked or expired: {message}"
            )
        raise GithubApiError(r.status_code, message, doc)
