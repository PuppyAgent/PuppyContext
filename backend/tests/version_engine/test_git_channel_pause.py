import base64
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.platform.authorization.models import RuntimeGrant, RuntimeMode, RuntimePrincipal
from src.platform.repository_target.models import ResolvedRepositoryView, ScopeTarget
from src.version_engine.entrypoints.git import auth as git_auth
from src.version_engine.entrypoints.git import router as git_router
from src.version_engine.entrypoints.http import access_point


def _request(headers: dict[str, str] | None = None):
    return SimpleNamespace(headers=headers or {})


def _scope_auth() -> dict:
    target = ScopeTarget(project_id="project-1", scope_id="scope-1")
    return {
        "agent": "git",
        "_runtime_grant": RuntimeGrant(
            principal=RuntimePrincipal(
                principal_id="credential-1",
                credential_kind="legacy_access_key",
            ),
            target=target,
            repository_view=ResolvedRepositoryView(
                target=target,
                path_prefix="docs",
                excludes=(),
                max_mode="rw",
            ),
            mode=RuntimeMode.READ_WRITE,
        ),
    }


def test_access_point_root_credential_is_current_and_scope_is_not_widened(monkeypatch):
    from src.infra.supabase.client import SupabaseClient
    from src.repo.access_credentials import AccessCredentialRepository

    row = dict(
        credential_id="credential-1",
        project_id="project-1",
        access_surface_id="surface-1",
        target_kind="project_root",
        scope_id=None,
        path_prefix="",
        excludes=[],
        target_max_mode="rw",
        effective_mode="rw",
    )
    monkeypatch.setattr(SupabaseClient, "__new__", lambda cls: SimpleNamespace(client=object()))
    monkeypatch.setattr(
        AccessCredentialRepository, "resolve_git_runtime_credential", lambda _self, _token: row
    )
    project, auth = access_point.resolve_access_point("token")
    assert project == "project-1"
    assert auth["_runtime_grant"].principal.principal_id == "credential-1"
    row.update(target_kind="scope", scope_id="scope-1")
    with pytest.raises(HTTPException) as exc:
        access_point.resolve_access_point("token")
    assert exc.value.status_code == 501


def test_access_point_storage_failure_is_retryable_not_invalid_credential(monkeypatch):
    from src.infra.supabase.client import SupabaseClient
    from src.repo.access_credentials import AccessCredentialRepository

    monkeypatch.setattr(SupabaseClient, "__new__", lambda cls: SimpleNamespace(client=object()))

    def unavailable(*args):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(AccessCredentialRepository, "resolve_git_runtime_credential", unavailable)
    with pytest.raises(HTTPException) as exc:
        access_point.resolve_access_point("token")
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_git_ap_infers_git_remote_channel_without_custom_header(monkeypatch):
    calls = []

    monkeypatch.setattr(
        git_router,
        "resolve_access_point",
        lambda _key: ("project-1", _scope_auth()),
    )
    monkeypatch.setattr(
        git_auth,
        "enforce_channel_pause",
        lambda _auth, channel, **_kwargs: calls.append(channel),
    )

    project_id, _auth = await git_router.resolve_git_access_point("access-key", _request())

    assert project_id == "project-1"
    assert calls == ["git_remote"]


@pytest.mark.asyncio
async def test_git_project_auth_infers_git_remote_channel_without_custom_header(monkeypatch):
    calls = []

    class _Credentials:
        def __init__(self, _client):
            pass

        def resolve_git_runtime_credential(self, _token):
            return {
                "credential_id": "credential-1",
                "project_id": "project-1",
                "access_surface_id": "surface-1",
                "target_kind": "project_root",
                "scope_id": None,
                "path_prefix": "",
                "excludes": [],
                "target_max_mode": "rw",
                "user_id": "user-1",
                "effective_mode": "rw",
            }

    monkeypatch.setattr(
        git_auth, "SupabaseClient", lambda: SimpleNamespace(client=SimpleNamespace())
    )
    monkeypatch.setattr(git_auth, "AccessCredentialRepository", _Credentials)
    monkeypatch.setattr(git_auth.settings, "SKIP_AUTH", False)
    monkeypatch.setattr(
        git_auth,
        "enforce_channel_pause",
        lambda _auth, channel, **_kwargs: calls.append(channel),
    )
    token = base64.b64encode(b"alice:secret").decode("ascii")

    auth = await git_auth.resolve_git_project_auth(
        "project-1",
        _request({"authorization": f"Basic {token}"}),
    )

    assert auth["_runtime_grant"].repository_view.path_prefix == ""
    assert "_scope" not in auth
    assert auth["_runtime_grant"].principal.credential_kind == "git_http_token"
    assert calls == ["git_remote"]


@pytest.mark.asyncio
async def test_canonical_git_route_requires_exact_root_or_scope_target(monkeypatch):
    class _Credentials:
        def __init__(self, _client):
            pass

        def resolve_git_runtime_credential(self, _token):
            return {
                "credential_id": "credential-docs",
                "project_id": "project-1",
                "access_surface_id": "surface-docs",
                "target_kind": "scope",
                "scope_id": "scope-docs",
                "path_prefix": "docs",
                "excludes": ["drafts"],
                "target_max_mode": "r",
                "user_id": None,
                "effective_mode": "r",
            }

    monkeypatch.setattr(
        git_auth, "SupabaseClient", lambda: SimpleNamespace(client=SimpleNamespace())
    )
    monkeypatch.setattr(git_auth, "AccessCredentialRepository", _Credentials)
    monkeypatch.setattr(git_auth.settings, "SKIP_AUTH", False)
    monkeypatch.setattr(git_auth, "enforce_channel_pause", lambda *_a, **_k: None)
    token = base64.b64encode(b"x-puppyone-token:git_secret").decode("ascii")
    request = _request({"authorization": f"Basic {token}"})

    with pytest.raises(Exception) as root_error:
        await git_auth.resolve_git_project_auth("project-1", request)
    assert root_error.value.status_code == 401
    assert "Basic realm" in root_error.value.headers["WWW-Authenticate"]

    auth = await git_auth.resolve_git_scope_auth("project-1", "scope-docs", request)
    grant = auth["_runtime_grant"]
    assert grant.target.scope_id == "scope-docs"
    assert grant.repository_view.path_prefix == "docs"
    assert grant.repository_view.excludes == ("drafts",)
    assert "_scope" not in auth

    with pytest.raises(Exception) as wrong_scope_error:
        await git_auth.resolve_git_scope_auth("project-1", "scope-other", request)
    assert wrong_scope_error.value.status_code == 401
