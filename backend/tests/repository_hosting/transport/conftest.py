"""Real HTTP/production transport; explicitly substituted control plane and disk objects."""

import pytest
from fastapi import FastAPI

from src.config import settings
from src.version_engine.bootstrap.dependencies import get_repo_manager
from src.version_engine.entrypoints.git.router import router as git_router
from tests.repository_hosting.harness.http import AUTH
from tests.version_engine.test_write_engine import _patch_canonical_git_credentials, _serve_git_app


@pytest.fixture(params=["", "docs"], ids=["root", "scope"])
def hosted(request, component_repo, git_repo, tmp_path, monkeypatch):
    state = component_repo
    scope = request.param
    state.repo.add_scope("scope-test", scope)
    state.git_cache_root = tmp_path / "transport-cache"
    monkeypatch.setattr(settings, "GIT_VIEW_CACHE_DIR", state.git_cache_root)
    _patch_canonical_git_credentials(
        monkeypatch, scope_id="scope-test", scope_path=scope, is_root=not scope,
    )
    monkeypatch.setattr(
        "src.version_engine.entrypoints.git.router._git_receive_max_body_bytes",
        lambda _: 32 * 1024 * 1024,
    )
    monkeypatch.setattr(
        "src.platform.access.model_repository.AccessModelRepository.get_by_target_kind",
        lambda *a, **k: None,
    )
    monkeypatch.setattr("src.infra.search.text_indexer.index_commit_delta", lambda *a, **k: None)
    monkeypatch.setattr(
        "src.version_engine.derived.outbox.complete_version_outbox_for_commit", lambda *a, **k: True,
    )
    refs = {}

    class MemoryRefs:
        def list_refs(self, project_id, scope_path="", **kwargs):
            return [row for (p, s, _), row in refs.items() if (p, s) == (project_id, scope_path)]

        def set_ref(self, *, project_id, scope_path, ref_name, commit_id, created_by):
            refs[project_id, scope_path, ref_name] = dict(ref_name=ref_name, commit_id=commit_id)
            return True

    monkeypatch.setattr(
        "src.version_engine.infrastructure.supabase.version_ref_repository.VersionRefStore",
        MemoryRefs,
    )
    app = FastAPI()
    app.include_router(git_router)
    app.dependency_overrides[get_repo_manager] = lambda: state.manager
    with _serve_git_app(app) as base:
        path = "/git/test-proj/scopes/scope-test.git" if scope else "/git/test-proj.git"
        remote = base + path
        git_repo.run("config", "http.extraHeader", AUTH.split("=", 1)[1])
        git_repo.run("remote", "add", "origin", remote)
        initial = git_repo.commit({"original.txt": b"original\n"})
        git_repo.run("push", "origin", "main")
        yield git_repo, remote, state, scope, initial
