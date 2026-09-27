"""
GitHub connector — one-time repository import.

The connector is intentionally storage-agnostic: it downloads a repository
archive, normalizes it into a relative path -> bytes map, and returns that to
SynchronizeEngine. SynchronizeEngine owns the Version Engine write-port commit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import io
import json
import posixpath
from typing import Any, TYPE_CHECKING
from urllib.parse import quote, urlparse
import zipfile

import httpx

from src.provider._base import (
    AuthRequirement,
    BaseProvider,
    Capability,
    ConfigField,
    ProviderSpec,
    Credentials,
    FetchResult,
    TriggerMode,
)
from src.provider.oauth.github_service import GithubOAuthService
from src.infra.s3.service import S3Service

if TYPE_CHECKING:
    from src.provider._base import ProviderDeps, ProviderSetup


DEFAULT_MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_FILE_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_FILES = 1000
MAX_SKIPPED_DETAILS = 250

DEFAULT_EXCLUDED_DIRS = frozenset({
    ".git",
    ".hg",
    ".svn",
    ".cache",
    ".next",
    ".nuxt",
    ".parcel-cache",
    ".pytest_cache",
    ".mypy_cache",
    ".tox",
    ".turbo",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "env",
    "node_modules",
    "target",
    "venv",
})

DEFAULT_EXCLUDED_FILES = frozenset({
    ".DS_Store",
    "Thumbs.db",
})

SENSITIVE_FILE_NAMES = frozenset({
    ".npmrc",
    ".pypirc",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
})


@dataclass(frozen=True)
class GitHubRepoRef:
    owner: str
    repo: str
    ref: str | None = None
    subdir: str = ""


@dataclass
class ExtractedRepoFiles:
    files: dict[str, bytes]
    skipped: list[dict[str, Any]] = field(default_factory=list)
    skipped_count: int = 0
    total_bytes: int = 0


class GithubProvider(BaseProvider):
    """Provider for one-time GitHub repository imports."""

    def spec(self) -> ProviderSpec:
        return ProviderSpec(
            provider="github",
            display_name="GitHub",
            capabilities=Capability.PULL,
            supported_directions=["inbound"],
            default_trigger=TriggerMode.MANUAL,
            default_node_type="json",
            auth=AuthRequirement.OPTIONAL_OAUTH,
            oauth_type="github",
            oauth_ui_type="github",
            supported_sync_modes=(),
            default_sync_mode="manual",
            creation_mode="direct",
            description="One-time import of repository files",
            accept_types=("folder",),
            icon_url="https://github.githubassets.com/favicons/favicon-dark.svg",
            config_fields=(
                ConfigField(
                    key="source_url",
                    label="Repository URL",
                    type="url",
                    required=True,
                    placeholder="https://github.com/org/repo",
                    hint="Full URL of the GitHub repository",
                ),
            ),
        )

    def __init__(
        self,
        github_service: GithubOAuthService,
        s3_service: S3Service,
        node_service: Any = None,
    ):
        self.node_service = node_service
        self.github_service = github_service
        self.s3_service = s3_service

    async def fetch(self, config: dict, credentials: Credentials) -> FetchResult:
        """Download a GitHub repository archive and return real files."""
        source_url = config.get("source_url", "")
        if not source_url:
            raise ValueError("source_url is required for GitHub import")

        repo_ref = _parse_github_repo_url(source_url)
        headers = _github_headers(credentials.access_token)

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(90.0, connect=15.0),
            follow_redirects=True,
        ) as client:
            repo_data = await _fetch_repo_metadata(client, headers, repo_ref)
            selected_ref = config.get("ref") or repo_ref.ref or repo_data.get("default_branch") or "main"
            commit_sha = await _fetch_commit_sha(client, headers, repo_ref, selected_ref)
            archive_bytes = await _download_zipball(client, headers, repo_ref, selected_ref, config)

        extracted = _extract_zip_files(
            archive_bytes,
            subdir=repo_ref.subdir,
            max_files=_positive_int(config.get("max_files"), DEFAULT_MAX_FILES),
            max_total_bytes=_positive_int(config.get("max_total_bytes"), DEFAULT_MAX_TOTAL_BYTES),
            max_file_bytes=_positive_int(config.get("max_file_bytes"), DEFAULT_MAX_FILE_BYTES),
            include_binary=_coerce_bool(config.get("include_binary"), default=True),
            extra_excluded_dirs=set(config.get("exclude_dirs") or []),
        )

        manifest = {
            "source_type": "github_repo",
            "importer": "puppyone.github.import",
            "owner": repo_ref.owner,
            "repo": repo_ref.repo,
            "full_name": repo_data.get("full_name") or f"{repo_ref.owner}/{repo_ref.repo}",
            "description": repo_data.get("description"),
            "html_url": repo_data.get("html_url") or source_url,
            "default_branch": repo_data.get("default_branch"),
            "ref": selected_ref,
            "commit_sha": commit_sha,
            "subdir": repo_ref.subdir,
            "files_imported": len(extracted.files),
            "bytes_imported": extracted.total_bytes,
            "files_skipped": extracted.skipped_count,
            "skipped": extracted.skipped,
            "limits": {
                "max_files": _positive_int(config.get("max_files"), DEFAULT_MAX_FILES),
                "max_file_bytes": _positive_int(config.get("max_file_bytes"), DEFAULT_MAX_FILE_BYTES),
                "max_total_bytes": _positive_int(config.get("max_total_bytes"), DEFAULT_MAX_TOTAL_BYTES),
            },
        }

        files = {
            **extracted.files,
            ".puppyone/import.json": json.dumps(
                manifest,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8"),
        }
        content_hash = _hash_file_map(files, repo_ref, selected_ref, commit_sha)
        short_sha = commit_sha[:7] if commit_sha else selected_ref

        return FetchResult(
            content=manifest,
            content_hash=content_hash,
            node_type="folder",
            node_name=repo_ref.repo,
            summary=(
                f"Import from GitHub {repo_ref.owner}/{repo_ref.repo}@{short_sha}: "
                f"{len(extracted.files)} files"
            ),
            files=files,
        )


def _parse_github_repo_url(source_url: str) -> GitHubRepoRef:
    parsed = urlparse(source_url)
    host = parsed.netloc.lower()
    if host not in ("github.com", "www.github.com"):
        raise ValueError(f"Invalid GitHub URL: {source_url}")

    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) < 2:
        raise ValueError(f"Invalid GitHub URL: {source_url}")

    owner = parts[0]
    repo = parts[1][:-4] if parts[1].endswith(".git") else parts[1]
    if not owner or not repo:
        raise ValueError(f"Invalid GitHub URL: {source_url}")

    ref = None
    subdir = ""
    if len(parts) >= 4 and parts[2] in ("tree", "blob"):
        ref = parts[3]
        subdir = "/".join(parts[4:])

    return GitHubRepoRef(
        owner=owner,
        repo=repo,
        ref=ref,
        subdir=_normalize_repo_path(subdir) if subdir else "",
    )


def _github_headers(access_token: str = "") -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Puppyone-GitHub-Import",
    }
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    return headers


async def _fetch_repo_metadata(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    repo_ref: GitHubRepoRef,
) -> dict[str, Any]:
    response = await client.get(
        f"https://api.github.com/repos/{repo_ref.owner}/{repo_ref.repo}",
        headers=headers,
    )
    _raise_for_github_error(response, f"{repo_ref.owner}/{repo_ref.repo}")
    return response.json()


async def _fetch_commit_sha(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    repo_ref: GitHubRepoRef,
    ref: str,
) -> str:
    response = await client.get(
        f"https://api.github.com/repos/{repo_ref.owner}/{repo_ref.repo}/commits/{quote(ref, safe='')}",
        headers=headers,
    )
    if response.status_code == 200:
        return str(response.json().get("sha") or ref)
    return ref


async def _download_zipball(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    repo_ref: GitHubRepoRef,
    ref: str,
    config: dict,
) -> bytes:
    response = await client.get(
        f"https://api.github.com/repos/{repo_ref.owner}/{repo_ref.repo}/zipball/{quote(ref, safe='')}",
        headers=headers,
    )
    _raise_for_github_error(response, f"{repo_ref.owner}/{repo_ref.repo}@{ref}")

    archive_bytes = response.content
    max_archive_bytes = _positive_int(config.get("max_archive_bytes"), DEFAULT_MAX_ARCHIVE_BYTES)
    if len(archive_bytes) > max_archive_bytes:
        raise ValueError(
            f"GitHub repository archive is too large "
            f"({len(archive_bytes)} bytes > {max_archive_bytes} bytes)"
        )
    return archive_bytes


def _extract_zip_files(
    archive_bytes: bytes,
    *,
    subdir: str = "",
    max_files: int = DEFAULT_MAX_FILES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    include_binary: bool = True,
    extra_excluded_dirs: set[str] | None = None,
) -> ExtractedRepoFiles:
    excluded_dirs = set(DEFAULT_EXCLUDED_DIRS) | {d.strip("/") for d in (extra_excluded_dirs or set()) if d}
    wanted_subdir = _normalize_repo_path(subdir) if subdir else ""
    extracted = ExtractedRepoFiles(files={})

    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue

                rel_path = _strip_archive_root(info.filename)
                if not rel_path:
                    continue

                try:
                    rel_path = _normalize_repo_path(rel_path)
                except ValueError:
                    _record_skip(extracted, rel_path, "invalid_path")
                    continue

                if wanted_subdir:
                    if rel_path == wanted_subdir:
                        rel_path = posixpath.basename(rel_path)
                    elif rel_path.startswith(f"{wanted_subdir}/"):
                        rel_path = rel_path[len(wanted_subdir) + 1:]
                    else:
                        continue

                if _has_excluded_dir(rel_path, excluded_dirs):
                    _record_skip(extracted, rel_path, "excluded_dir")
                    continue
                if _is_excluded_file(rel_path):
                    _record_skip(extracted, rel_path, "excluded_file")
                    continue
                if _looks_sensitive_file(rel_path):
                    _record_skip(extracted, rel_path, "sensitive_file")
                    continue
                if info.file_size > max_file_bytes:
                    _record_skip(extracted, rel_path, "file_too_large", size=info.file_size)
                    continue
                if len(extracted.files) >= max_files:
                    _record_skip(extracted, rel_path, "file_count_limit", size=info.file_size)
                    continue
                if extracted.total_bytes + info.file_size > max_total_bytes:
                    _record_skip(extracted, rel_path, "total_size_limit", size=info.file_size)
                    continue

                with archive.open(info) as file_obj:
                    content = file_obj.read()

                if not include_binary and _is_binary_like(content):
                    _record_skip(extracted, rel_path, "binary_file", size=len(content))
                    continue

                extracted.files[rel_path] = content
                extracted.total_bytes += len(content)
    except zipfile.BadZipFile as exc:
        raise ValueError("GitHub repository archive could not be read") from exc

    return extracted


def _strip_archive_root(filename: str) -> str:
    path = filename.replace("\\", "/").lstrip("/")
    parts = [part for part in path.split("/") if part]
    if len(parts) <= 1:
        return ""
    return "/".join(parts[1:])


def _normalize_repo_path(path: str) -> str:
    normalized = posixpath.normpath(path.replace("\\", "/").strip("/"))
    if normalized in ("", ".") or normalized.startswith("../") or "/../" in normalized:
        raise ValueError(f"Invalid repository path: {path!r}")
    return normalized


def _has_excluded_dir(path: str, excluded_dirs: set[str]) -> bool:
    return any(part in excluded_dirs for part in path.split("/")[:-1])


def _is_excluded_file(path: str) -> bool:
    return posixpath.basename(path) in DEFAULT_EXCLUDED_FILES


def _looks_sensitive_file(path: str) -> bool:
    name = posixpath.basename(path)
    lower = name.lower()
    if name in SENSITIVE_FILE_NAMES:
        return True
    if lower == ".env":
        return True
    if lower.startswith(".env.") and not lower.endswith((".example", ".sample", ".template")):
        return True
    return lower.endswith((".pem", ".p12", ".pfx"))


def _is_binary_like(content: bytes) -> bool:
    sample = content[:4096]
    return b"\0" in sample


def _record_skip(
    extracted: ExtractedRepoFiles,
    path: str,
    reason: str,
    *,
    size: int | None = None,
) -> None:
    extracted.skipped_count += 1
    if len(extracted.skipped) >= MAX_SKIPPED_DETAILS:
        return
    entry: dict[str, Any] = {"path": path, "reason": reason}
    if size is not None:
        entry["size"] = size
    extracted.skipped.append(entry)


def _hash_file_map(
    files: dict[str, bytes],
    repo_ref: GitHubRepoRef,
    ref: str,
    commit_sha: str,
) -> str:
    digest = hashlib.sha256()
    digest.update(f"github:{repo_ref.owner}/{repo_ref.repo}:{ref}:{commit_sha}:{repo_ref.subdir}".encode("utf-8"))
    for path in sorted(files):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(files[path]).digest())
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def _positive_int(value: Any, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _raise_for_github_error(response: httpx.Response, resource: str) -> None:
    if response.status_code < 400:
        return

    message = ""
    try:
        payload = response.json()
        message = str(payload.get("message") or "")
    except Exception:
        message = response.text[:200]

    if response.status_code == 404:
        detail = (
            f"GitHub repository not found or private: {resource}. "
            "Check the URL, or connect GitHub for private repositories."
        )
    elif response.status_code == 403 and "rate limit" in message.lower():
        detail = "GitHub API rate limit reached. Connect GitHub and retry."
    elif message:
        detail = f"GitHub import failed for {resource}: {message}"
    else:
        detail = f"GitHub import failed for {resource} (HTTP {response.status_code})"
    raise ValueError(detail)


def setup(deps: "ProviderDeps") -> "ProviderSetup":
    from src.provider._base import ProviderSetup

    oauth_svc = GithubOAuthService()
    return ProviderSetup(
        adapter=GithubProvider(
            github_service=oauth_svc,
            s3_service=deps.s3_service,
            node_service=deps.node_service,
        ),
        oauth_bindings={"github": oauth_svc},
    )
