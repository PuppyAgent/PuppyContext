"""Match the real ScopeService signature and never create a root Scope."""
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from src.platform.access.adapters.mcp_endpoint import repository as mcp
from src.platform.access.adapters.sandbox_endpoint import repository as sandbox
from src.repo.scope_service import ScopeService


@pytest.fixture(params=[(mcp, mcp.McpEndpointRepository), (sandbox, sandbox.SandboxEndpointRepository)])
def endpoint(request, monkeypatch):
    module, cls = request.param
    service = create_autospec(ScopeService, instance=True)
    service.list_for_project.return_value = []
    service.create.return_value = SimpleNamespace(id="scope", path="docs")
    monkeypatch.setattr(module, "ScopeService", lambda: service)
    return object.__new__(cls), service


@pytest.mark.parametrize("path", [None, "", "/"])
def test_explicit_project_root_has_no_scope(endpoint, path):
    repo, scopes = endpoint
    assert repo._scope_for_path("project", path) == {"id": None, "path": ""}
    scopes.create.assert_not_called()
    scopes.list_for_project.assert_not_called()


def test_new_nonroot_scope_uses_max_mode_contract(endpoint):
    repo, scopes = endpoint
    assert repo._scope_for_path("project", "/docs/") == {"id": "scope", "path": "docs"}
    scopes.create.assert_called_once_with(
        project_id="project", name="docs", path="docs", exclude=[], max_mode="rw")


def test_existing_scope_is_reused_without_mutating_its_mode(endpoint):
    repo, scopes = endpoint
    scopes.list_for_project.return_value = [SimpleNamespace(id="existing", path="docs", max_mode="r")]
    assert repo._scope_for_path("project", "docs") == {"id": "existing", "path": "docs"}
    scopes.create.assert_not_called()
