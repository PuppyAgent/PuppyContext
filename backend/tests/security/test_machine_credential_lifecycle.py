"""ISSUE-003 machine credential issuance, hash auth, and revocation."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import MethodType, SimpleNamespace

import pytest

from src.config import settings
from src.platform.access.adapters.agent.config.repository import AgentRepository
from src.platform.access.adapters.sandbox_endpoint.repository import SandboxEndpointRepository
from src.repo.access_credentials import AccessCredentialRepository, access_token_hash
from src.repo.scope_repository import RepositoryScopeRepository


class _MemoryQuery:
    def __init__(self, client, table):
        self.client = client
        self.table_name = table
        self.filters = []
        self.limit_count = None
        self.ordering = None
        self.operation = "select"
        self.payload = None

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, column, value):
        self.filters.append(lambda row, c=column, v=value: row.get(c) == v)
        return self

    def filter(self, column, operator, value):
        assert operator == "eq"
        if column.startswith("config->>"):
            key = column.split("config->>", 1)[1]
            self.filters.append(lambda row, k=key, v=value: (row.get("config") or {}).get(k) == v)
        else:
            self.eq(column, value)
        return self

    def in_(self, column, values):
        accepted = set(values)
        self.filters.append(lambda row, c=column, a=accepted: row.get(c) in a)
        return self

    def is_(self, column, value):
        assert value == "null"
        self.filters.append(lambda row, c=column: row.get(c) is None)
        return self

    def order(self, column, desc=False):
        self.ordering = (column, desc)
        return self

    def limit(self, count):
        self.limit_count = count
        return self

    def insert(self, payload):
        self.operation = "insert"
        self.payload = deepcopy(payload)
        return self

    def update(self, payload):
        self.operation = "update"
        self.payload = deepcopy(payload)
        return self

    def delete(self):
        self.operation = "delete"
        return self

    def _matches(self, row):
        return all(predicate(row) for predicate in self.filters)

    def execute(self):
        table = self.client.tables.setdefault(self.table_name, [])
        if self.operation == "insert":
            row = deepcopy(self.payload)
            row.setdefault("id", f"row-{len(table) + 1}")
            row.setdefault("created_at", datetime.now(timezone.utc).isoformat())
            row.setdefault("updated_at", row["created_at"])
            table.append(row)
            return SimpleNamespace(data=[deepcopy(row)])

        matched = [row for row in table if self._matches(row)]
        if self.operation == "update":
            for row in matched:
                row.update(deepcopy(self.payload))
            return SimpleNamespace(data=deepcopy(matched))
        if self.operation == "delete":
            self.client.tables[self.table_name] = [row for row in table if not self._matches(row)]
            return SimpleNamespace(data=deepcopy(matched))

        if self.ordering:
            column, desc = self.ordering
            matched.sort(key=lambda row: row.get(column) or "", reverse=desc)
        if self.limit_count is not None:
            matched = matched[: self.limit_count]
        return SimpleNamespace(data=deepcopy(matched))


class _MemoryClient:
    def __init__(self, **tables):
        self.tables = {name: deepcopy(rows) for name, rows in tables.items()}

    def table(self, name):
        return _MemoryQuery(self, name)


@pytest.fixture(autouse=True)
def _credential_secret(monkeypatch):
    monkeypatch.setattr(settings, "ACCESS_CREDENTIAL_PREVIOUS_HASH_SECRET", "")
    monkeypatch.setattr(
        settings,
        "ACCESS_CREDENTIAL_HASH_SECRET",
        "test-credential-secret-at-least-32-characters",
    )


def _surface(
    *, surface_id="surface-1", kind="agent", config=None, scope_id="scope-1"
):
    now = datetime.now(timezone.utc).isoformat()
    return {
        "id": surface_id,
        "org_id": "org-1",
        "project_id": "project-1",
        "scope_id": scope_id,
        "kind": kind,
        "name": "Surface",
        "config": config or {},
        "status": "active",
        "created_by": None,
        "created_at": now,
        "updated_at": now,
    }


def test_hashing_never_falls_back_to_jwt_or_internal_secret(monkeypatch):
    monkeypatch.setattr(settings, "ACCESS_CREDENTIAL_HASH_SECRET", "")
    monkeypatch.setattr(settings, "INTERNAL_API_SECRET", "internal-secret")
    monkeypatch.setattr(settings, "JWT_SECRET", "jwt-secret")
    with pytest.raises(ValueError, match="ACCESS_CREDENTIAL_HASH_SECRET"):
        access_token_hash("mcp_example")


def test_credential_rotation_revokes_old_hash_and_never_stores_plaintext():
    client = _MemoryClient(access_surface_credentials=[])
    repo = AccessCredentialRepository(client)

    old = repo.issue_bearer_token(
        access_surface_id="surface-1",
        org_id="org-1",
        project_id="project-1",
        prefix="mcp",
    )
    assert repo.get_active_by_token(old) is not None
    assert old not in repr(client.tables["access_surface_credentials"])

    new = repo.issue_bearer_token(
        access_surface_id="surface-1",
        org_id="org-1",
        project_id="project-1",
        prefix="mcp",
    )
    assert repo.get_active_by_token(old) is None
    assert repo.get_active_by_token(new) is not None


def test_explicit_previous_key_preserves_access_without_issuing_legacy_hashes(monkeypatch):
    repo = AccessCredentialRepository(_MemoryClient(access_surface_credentials=[]))
    old_secret = settings.ACCESS_CREDENTIAL_HASH_SECRET
    old = repo.issue_bearer_token(
        access_surface_id="old", org_id="org-1", project_id="project-1", prefix="mcp",
    )
    old_hash = access_token_hash(old)
    monkeypatch.setattr(settings, "ACCESS_CREDENTIAL_HASH_SECRET", "new-independent-secret-for-rotation-123456789")
    assert repo.get_active_by_token(old) is None
    monkeypatch.setattr(settings, "ACCESS_CREDENTIAL_PREVIOUS_HASH_SECRET", old_secret)
    assert repo.get_active_by_token(old)["key_hash"] == old_hash
    new = repo.issue_bearer_token(
        access_surface_id="new", org_id="org-1", project_id="project-1", prefix="mcp",
    )
    assert repo.get_active_by_token(new)["key_hash"] == access_token_hash(new)
    rows = repo._client.tables["access_surface_credentials"]
    rows[0]["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    assert repo.get_active_by_token(old) is None
    rows[0]["expires_at"] = None
    rows[0]["status"] = "revoked"
    assert repo.get_active_by_token(old) is None


def test_git_previous_key_still_uses_authoritative_runtime_rpc(monkeypatch):
    old_secret = settings.ACCESS_CREDENTIAL_HASH_SECRET
    old_hash = access_token_hash("git_legacy")
    monkeypatch.setattr(settings, "ACCESS_CREDENTIAL_HASH_SECRET", "new-independent-secret-for-rotation-123456789")
    monkeypatch.setattr(settings, "ACCESS_CREDENTIAL_PREVIOUS_HASH_SECRET", old_secret)
    calls = []
    authorized = True

    def rpc(name, params):
        assert name == "resolve_git_runtime_credential"
        calls.append(params["p_key_hash"])
        row = [{"id": "old", "effective_mode": "r"}] if authorized and params["p_key_hash"] == old_hash else []
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=row))

    repo = AccessCredentialRepository(SimpleNamespace(rpc=rpc))
    assert repo.resolve_git_runtime_credential("git_legacy")["effective_mode"] == "r"
    assert calls == [access_token_hash("git_legacy"), old_hash]
    authorized = False
    assert repo.resolve_git_runtime_credential("git_legacy") is None


def test_expired_scope_session_credential_is_rejected():
    client = _MemoryClient(
        access_surfaces=[_surface(kind="cli")],
        access_surface_credentials=[],
        repository_scopes=[{
            "id": "scope-1",
            "project_id": "project-1",
            "name": "Docs",
            "path": "docs",
            "exclude": [],
            "max_mode": "rw",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }],
    )
    credentials = AccessCredentialRepository(client)
    token = credentials.issue_bearer_token(
        access_surface_id="surface-1",
        org_id="org-1",
        project_id="project-1",
        prefix="cli",
        revoke_existing=False,
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )

    assert credentials.get_active_by_token(token) is None
    assert RepositoryScopeRepository(client).get_by_access_key(token) is None


def test_scope_credential_resolves_via_cli_access_surface_only():
    now = datetime.now(timezone.utc).isoformat()
    client = _MemoryClient(
        access_surfaces=[_surface(kind="cli")],
        access_surface_credentials=[],
        repository_scopes=[{
            "id": "scope-1", "project_id": "project-1", "name": "Docs",
            "path": "docs", "exclude": [], "max_mode": "rw",
            "created_at": now, "updated_at": now,
        }],
    )
    token = AccessCredentialRepository(client).issue_bearer_token(
        access_surface_id="surface-1",
        org_id="org-1",
        project_id="project-1",
        prefix="cli",
    )

    scope = RepositoryScopeRepository(client).get_by_access_key(token)
    assert scope and scope.id == "scope-1"
    assert token not in repr(client.tables)


def test_git_http_token_is_separate_from_cli_bearer_and_resolves_runtime_facts():
    now = datetime.now(timezone.utc).isoformat()
    client = _MemoryClient(
        access_surfaces=[
            _surface(kind="git_remote", scope_id=None),
            _surface(surface_id="surface-cli", kind="cli"),
        ],
        access_surface_credentials=[],
        repository_scopes=[{
            "id": "scope-1", "project_id": "project-1", "name": "Docs",
            "path": "docs", "exclude": ["private"], "max_mode": "rw",
            "created_at": now, "updated_at": now,
        }],
    )
    credentials = AccessCredentialRepository(client)
    git_token = credentials.issue_git_http_token(
        access_surface_id="surface-1",
        org_id="org-1",
        project_id="project-1",
        grant_mode="r",
    )
    cli_token = credentials.issue_bearer_token(
        access_surface_id="surface-cli",
        org_id="org-1",
        project_id="project-1",
        prefix="cli",
        revoke_existing=False,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )

    assert credentials.get_active_by_token(git_token) is None
    assert credentials.resolve_git_runtime_credential(cli_token) is None
    resolved = credentials.resolve_git_runtime_credential(git_token)
    assert resolved is not None
    assert resolved["project_id"] == "project-1"
    assert resolved["target_kind"] == "project_root"
    assert resolved["scope_id"] is None
    assert resolved["path_prefix"] == ""
    assert resolved["excludes"] == []
    assert resolved["effective_mode"] == "r"


def test_shared_git_read_and_readwrite_credentials_rotate_independently():
    client = _MemoryClient(
        access_surfaces=[_surface(kind="git_remote", scope_id=None)],
        access_surface_credentials=[],
    )
    credentials = AccessCredentialRepository(client)

    read_v1 = credentials.issue_git_http_token(
        access_surface_id="surface-1",
        org_id="org-1",
        project_id="project-1",
        grant_mode="r",
    )
    readwrite = credentials.issue_git_http_token(
        access_surface_id="surface-1",
        org_id="org-1",
        project_id="project-1",
        grant_mode="rw",
    )
    read_v2 = credentials.issue_git_http_token(
        access_surface_id="surface-1",
        org_id="org-1",
        project_id="project-1",
        grant_mode="r",
    )

    assert credentials.resolve_git_runtime_credential(read_v1) is None
    assert credentials.resolve_git_runtime_credential(read_v2)["effective_mode"] == "r"
    assert credentials.resolve_git_runtime_credential(readwrite)["effective_mode"] == "rw"


def test_shared_git_rotation_preserves_same_mode_session_credential():
    now = datetime.now(timezone.utc)
    client = _MemoryClient(
        access_surfaces=[_surface(kind="git_remote", scope_id=None)],
        access_surface_credentials=[],
    )
    credentials = AccessCredentialRepository(client)

    shared_v1 = credentials.issue_git_http_token(
        access_surface_id="surface-1",
        org_id="org-1",
        project_id="project-1",
        grant_mode="r",
    )
    session = credentials.issue_git_http_token(
        access_surface_id="surface-1",
        org_id="org-1",
        project_id="project-1",
        grant_mode="r",
        revoke_existing=False,
        expires_at=now + timedelta(minutes=5),
    )
    shared_v2 = credentials.issue_git_http_token(
        access_surface_id="surface-1",
        org_id="org-1",
        project_id="project-1",
        grant_mode="r",
    )

    assert credentials.resolve_git_runtime_credential(shared_v1) is None
    assert credentials.resolve_git_runtime_credential(shared_v2) is not None
    assert credentials.resolve_git_runtime_credential(session) is not None
    active = [
        row for row in client.tables["access_surface_credentials"]
        if row["status"] == "active"
    ]
    assert {row["credential_lifecycle"] for row in active} == {"shared", "session"}


def test_user_git_credential_rechecks_current_role_and_clamps_write_mode():
    """A Viewer-owned Git credential may never inherit an rw target maximum."""
    now = datetime.now(timezone.utc).isoformat()
    raw_token = "pwg_user_git_token"
    client = _MemoryClient(
        projects=[{
            "id": "project-1", "org_id": "org-1", "visibility": "private",
            "lifecycle_status": "ready",
        }],
        org_members=[{
            "id": "org-member-1", "org_id": "org-1", "user_id": "user-1", "role": "member",
        }],
        project_members=[{
            "id": "project-member-1", "org_id": "org-1", "project_id": "project-1",
            "user_id": "user-1", "role": "viewer",
        }],
        access_surfaces=[_surface(kind="git_remote", scope_id=None)],
        access_surface_credentials=[{
            "id": "credential-1", "org_id": "org-1", "project_id": "project-1",
            "access_surface_id": "surface-1", "user_id": "user-1",
            "credential_type": "git_http_token", "credential_lifecycle": "user",
            "grant_mode": "rw", "key_prefix": "pwg", "key_last4": "oken",
            "key_hash": access_token_hash(raw_token), "hash_alg": "hmac_sha256_v1",
            "status": "active", "created_at": now,
        }],
    )

    credentials = AccessCredentialRepository(client)
    resolved = credentials.resolve_git_runtime_credential(raw_token)
    assert resolved is not None
    assert resolved["effective_mode"] == "r"
    assert resolved["user_id"] == "user-1"

    client.tables["project_members"] = []
    assert credentials.resolve_git_runtime_credential(raw_token) is None


def test_shared_surface_rotation_does_not_revoke_user_git_credentials():
    client = _MemoryClient(
        projects=[{
            "id": "project-1", "org_id": "org-1", "visibility": "private",
            "lifecycle_status": "ready",
        }],
        org_members=[{
            "id": "org-member-1", "org_id": "org-1", "user_id": "user-1", "role": "member",
        }],
        project_members=[{
            "id": "project-member-1", "org_id": "org-1", "project_id": "project-1",
            "user_id": "user-1", "role": "editor",
        }],
        access_surfaces=[_surface(kind="git_remote", scope_id=None)],
        access_surface_credentials=[],
    )
    credentials = AccessCredentialRepository(client)
    user_token = "pwg_user_specific_token"
    client.tables["access_surface_credentials"].append({
        "id": "user-credential", "org_id": "org-1", "project_id": "project-1",
        "access_surface_id": "surface-1", "user_id": "user-1",
        "credential_type": "git_http_token", "credential_lifecycle": "user",
        "grant_mode": "rw", "key_prefix": "pwg", "key_last4": "oken",
        "key_hash": access_token_hash(user_token), "hash_alg": "hmac_sha256_v1",
        "status": "active", "created_at": datetime.now(timezone.utc).isoformat(),
    })
    credentials.issue_git_http_token(
        access_surface_id="surface-1",
        org_id="org-1",
        project_id="project-1",
        grant_mode="rw",
    )
    credentials.issue_git_http_token(
        access_surface_id="surface-1",
        org_id="org-1",
        project_id="project-1",
        grant_mode="rw",
    )

    user_row = next(
        row for row in client.tables["access_surface_credentials"]
        if row.get("credential_lifecycle") == "user"
    )
    assert user_row["status"] == "active"
    assert credentials.resolve_git_runtime_credential(user_token) is not None


def test_agent_issues_once_authenticates_by_hash_and_revokes_old_token():
    surface = _surface(config={
        "name": "Agent",
        "repository_view": {
            "target": {"kind": "project_root", "project_id": "project-1"},
            "path_prefix": "",
            "excludes": [],
            "max_mode": "rw",
        },
    })
    client = _MemoryClient(access_surfaces=[surface], access_surface_credentials=[])
    repo = AgentRepository(client)
    repo._scope_for_path = MethodType(lambda _self, *_a, **_k: {
        "id": None,
        "path": "",
        "exclude": [],
        "mode": "rw",
        "target": {"kind": "project_root", "project_id": "project-1"},
    }, repo)
    repo._agent_surface_for_scope = MethodType(lambda _self, **_kwargs: deepcopy(surface), repo)

    created = repo.create(project_id="project-1", name="Agent")
    issued = created.mcp_api_key
    assert issued and issued.startswith("mcp_")
    stored_surface = client.tables["access_surfaces"][0]
    assert "mcp_api_key" not in stored_surface["config"]

    ordinary = repo.get_by_id("surface-1")
    assert ordinary and ordinary.mcp_api_key is None
    assert ordinary.mcp_enabled is True
    updated = repo.update("surface-1", description="updated")
    assert updated and updated.mcp_enabled is True
    assert updated.mcp_api_key is None
    credentials = AccessCredentialRepository(client)
    assert credentials.get_active_by_token(issued)["access_surface_id"] == "surface-1"

    replacement = repo.regenerate_mcp_api_key("surface-1")
    assert replacement and replacement != issued
    assert credentials.get_active_by_token(issued) is None
    assert credentials.get_active_by_token(replacement)["access_surface_id"] == "surface-1"


def test_sandbox_issues_once_and_exec_lookup_rejects_revoked_token(monkeypatch):
    client = _MemoryClient(
        access_surfaces=[],
        access_surface_credentials=[],
        repository_scopes=[{"id": "scope-1", "path": "docs"}],
    )
    repo = SandboxEndpointRepository(client)
    monkeypatch.setattr(repo, "_scope_for_path", lambda *_a, **_k: {"id": "scope-1", "path": ""})
    monkeypatch.setattr(repo, "_project_org_id", lambda *_a, **_k: "org-1")

    created = repo.create(project_id="project-1", name="Sandbox")
    issued = created["access_key"]
    assert issued and issued.startswith("sbx_")
    stored_surface = client.tables["access_surfaces"][0]
    assert "access_key" not in stored_surface["config"]

    ordinary = repo.get_by_id(created["id"])
    assert ordinary["access_key"] is None
    assert ordinary["has_key"] is True
    updated = repo.update(created["id"], description="updated")
    assert updated and updated["has_key"] is True
    assert updated["access_key"] is None
    assert repo.get_by_access_key(issued)["id"] == created["id"]

    replacement = repo.regenerate_access_key(created["id"])["access_key"]
    assert replacement != issued
    assert repo.get_by_access_key(issued) is None
    assert repo.get_by_access_key(replacement)["id"] == created["id"]
