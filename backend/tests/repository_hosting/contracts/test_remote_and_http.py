from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.version_engine.entrypoints.git.locator import canonical_git_url, parse_canonical_git_url
from src.version_engine.entrypoints.git.router import router as git_router

pytestmark = pytest.mark.hosting_component


@pytest.mark.parametrize("project", ["p", "project_1", "a-b", "A1", "123"])
@pytest.mark.parametrize("scope", [None, "scope_1", "s-2"])
def test_locator_roundtrip_has_no_credentials(project, scope):
    url = canonical_git_url("HTTPS://Cloud.Example/", project, scope)
    parsed = parse_canonical_git_url(url)
    assert (parsed.project_id, parsed.scope_id) == (project, scope)
    assert url.startswith("https://cloud.example/git/")
    assert "@" not in url and "?" not in url


@pytest.mark.parametrize(
    "url",
    [
        "https://u:secret@example.test/git/p.git",
        "https://example.test/git/p.git?token=secret",
        "https://example.test/git/p.git#secret",
        "https://example.test/git/p%2Fq.git",
        "https://example.test/git/p%252Fq.git",
        "https://example.test/git/../p.git",
        "https://example.test/git/p/scopes/s%2Fchild.git",
        "https://example.test/git/p.git/extra",
        "https://example.test/git/p/scopes/.git",
        "https://example.test/git//p.git",
    ],
)
def test_ambiguous_locator_is_not_a_repository_identity(url):
    assert parse_canonical_git_url(url) is None


@pytest.mark.parametrize("path", ["/git/p.git", "/git/p/scopes/s.git"])
@pytest.mark.parametrize("service", ["git-upload-pack", "git-receive-pack"])
@pytest.mark.parametrize("authorization", [None, "Basic !!!", "Bearer invalid"])
def test_real_route_rejects_unauthenticated_discovery(monkeypatch, path, service, authorization):
    from src.version_engine.entrypoints.git import auth

    monkeypatch.setattr(auth.settings, "SKIP_AUTH", False)

    class Credentials:
        def __init__(self, *args):
            pass

        def resolve_git_runtime_credential(self, token):
            return None

    monkeypatch.setattr(auth, "SupabaseClient", lambda: SimpleNamespace(client=None))
    monkeypatch.setattr(auth, "AccessCredentialRepository", Credentials)
    app = FastAPI()
    app.include_router(git_router)
    headers = {"Authorization": authorization} if authorization else {}
    with TestClient(app) as client:
        response = client.get(path + "/info/refs", params={"service": service}, headers=headers)
    assert response.status_code == 401
    assert "Basic" in response.headers["www-authenticate"]
    assert "PACK" not in response.text


@pytest.mark.parametrize(
    "method,suffix", [("post", "git-receive-pack"), ("post", "git-upload-pack")]
)
def test_anonymous_write_cannot_reach_repository_resolver(monkeypatch, method, suffix):
    from src.version_engine.bootstrap.dependencies import get_repo_manager

    app = FastAPI()
    app.include_router(git_router)

    class Manager:
        def get_server_repo(self, *a, **k):
            raise AssertionError("anonymous request reached repository")

    app.dependency_overrides[get_repo_manager] = lambda: Manager()
    with TestClient(app) as client:
        response = getattr(client, method)(f"/git/p.git/{suffix}", content=b"0000")
    assert response.status_code == 401
