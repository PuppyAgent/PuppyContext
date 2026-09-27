from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.version_engine.admission import connector_policy
from src.platform.authorization.models import RuntimeGrant, RuntimeMode, RuntimePrincipal
from src.platform.repository_target.models import ResolvedRepositoryView, ScopeTarget


def _auth(scope_id: str = "scope-1") -> dict:
    target = ScopeTarget(project_id="project-1", scope_id=scope_id)
    return {
        "agent": f"scope:{scope_id}",
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


def test_missing_cli_fs_policy_uses_default_without_delete():
    allowed = connector_policy.effective_cli_fs_allowed_commands({})

    assert "ls" in allowed
    assert "write" in allowed
    assert "rm" not in allowed
    assert "rmdir" not in allowed


def test_explicit_cli_fs_policy_is_exact_allow_list():
    allowed = connector_policy.effective_cli_fs_allowed_commands({
        "fs": {"allowed_commands": ["ls", "cat", "unknown", "rm"]},
    })

    assert allowed == frozenset({"ls", "cat", "rm"})


def test_cli_fs_command_policy_allows_cached_command(monkeypatch):
    calls = []

    class _Repo:
        def get_by_target_kind(self, project_id, scope_id, provider):
            calls.append((project_id, scope_id, provider))
            return SimpleNamespace(
                id="connector-1",
                kind="cli",
                status="active",
                policy={"fs": {"allowed_commands": ["ls"]}},
            )

    connector_policy.clear_connector_policy_cache()
    monkeypatch.setattr(connector_policy, "AccessModelRepository", _Repo)

    connector_policy.admit_cli_fs_command(_auth(), "ls", "cli")
    connector_policy.admit_cli_fs_command(_auth(), "ls", "cli")

    assert calls == [("project-1", "scope-1", "cli")]


def test_cli_fs_command_policy_denies_unlisted_command(monkeypatch):
    class _Repo:
        def get_by_target_kind(self, _project_id, _scope_id, _provider):
            return SimpleNamespace(
                id="connector-1",
                kind="cli",
                status="active",
                policy={"fs": {"allowed_commands": ["ls"]}},
            )

    connector_policy.clear_connector_policy_cache()
    monkeypatch.setattr(connector_policy, "AccessModelRepository", _Repo)

    with pytest.raises(HTTPException) as exc:
        connector_policy.admit_cli_fs_command(_auth(), "rm", "cli")

    assert exc.value.status_code == 403
    assert exc.value.detail["error_code"] == "CLI_FS_COMMAND_DENIED"
    assert exc.value.detail["command"] == "rm"


def test_cli_fs_command_policy_fails_closed_on_lookup_error(monkeypatch):
    class _Repo:
        def get_by_target_kind(self, _project_id, _scope_id, _provider):
            raise RuntimeError("db down")

    connector_policy.clear_connector_policy_cache()
    monkeypatch.setattr(connector_policy, "AccessModelRepository", _Repo)

    with pytest.raises(HTTPException) as exc:
        connector_policy.admit_cli_fs_command(_auth(), "ls", "cli")

    assert exc.value.status_code == 403
    assert exc.value.detail == "Connector policy could not be verified"
