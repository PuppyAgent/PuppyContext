"""Operation status cannot turn a preparation, cache or old grant into an ACK."""

import threading
import uuid
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest

from src.platform.authorization.models import (
    GrantSource,
    ProjectCapability,
    ProjectGrant,
    ProjectRole,
)
from src.version_engine.infrastructure.supabase.ref_authority_repository import (
    AdmittedRefAuthorityRepository,
)
from src.version_engine.infrastructure.supabase.repo_manager import VersionRepoManager
from src.version_engine.read.operation_status import operation_status

pytestmark = pytest.mark.hosting_component
KEY = str(uuid.uuid4())
GRANT = ProjectGrant("project", "org", "user", ProjectRole.VIEWER, GrantSource.PROJECT_MEMBER,
                     frozenset({ProjectCapability.CONTENT_READ}))
RECORD = {"project_id": "project", "actor": "user:user", "request_key": KEY,
          "input_sha256": "a"*64, "ref_request_sha256": "b"*64, "status": "committed",
          "result": {"project_id": "project", "generation": 1, "status": "committed"}, "product": None}


def test_native_operation_status_uses_only_checked_rpc_no_lease_or_storage():
    client = Mock()
    client.rpc.return_value.execute.return_value.data = deepcopy(RECORD)
    lease = Mock(side_effect=AssertionError("lookup must not obtain a lease"))
    control = AdmittedRefAuthorityRepository(client, lease_provider=lease)
    expected = {k: v for k, v in RECORD.items() if k != "actor"}
    assert operation_status(control, "project", GRANT, KEY) == expected
    lease.assert_not_called()
    client.rpc.assert_called_once_with("get_admitted_version_operation_status", {
        "p_project_id": "project", "p_actor": "user:user", "p_request_key": KEY,
    })
    client.table.assert_not_called()
    s3 = Mock()
    manager = VersionRepoManager(s3, SimpleNamespace(client=client))
    manager.repository_metadata = Mock(side_effect=AssertionError("not current native service selection"))
    assert manager.get_native_operation_status("project", GRANT, KEY) == expected
    assert not s3.mock_calls


@pytest.mark.parametrize("change", [
    {"project_id": "other"}, {"actor": "user:other"}, {"request_key": str(uuid.uuid4())},
    {"status": "published"}, {"status": []}, {"input_sha256": "A"*64},
    {"ref_request_sha256": None}, {"result": None},
    {"result": {"project_id": "other", "generation": 1, "status": "committed"}},
    {"result": {"project_id": "project", "generation": True, "status": "committed"}},
    {"status": "pending"}, {"status": "rejected", "product": {"commit_oid": "a"*40}},
    {"input_sha256": None, "product": {}}, {"product": []},
])
def test_native_operation_status_rejects_invalid_bindings(change):
    control = SimpleNamespace(operation_status=lambda *_: {**deepcopy(RECORD), **change})
    with pytest.raises(RuntimeError, match="invalid"):
        operation_status(control, "project", GRANT, KEY)


def test_native_operation_status_missing_capability_and_denial_do_not_fallback():
    with pytest.raises(RuntimeError, match="lookup unavailable"):
        operation_status(SimpleNamespace(), "project", GRANT, KEY)
    control = Mock()
    with pytest.raises(PermissionError):
        operation_status(control, "other", GRANT, KEY)
    control.operation_status.assert_not_called()
    control.operation_status.side_effect = RuntimeError("lookup down")
    with pytest.raises(RuntimeError, match="lookup down"):
        operation_status(control, "project", GRANT, KEY)
    assert control.mock_calls == [call.operation_status('project', 'user:user', KEY)]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["committed", "missing", "denied", "unavailable", "network"])
async def test_native_operation_status_http_is_current_read_and_sanitizes_failures(outcome):
    from fastapi import HTTPException, Response
    from httpx import ConnectError

    from src.platform.authorization.models import ProjectAction
    from src.version_engine.entrypoints.http.content_operations import read_operation_status

    main_thread = threading.get_ident()
    def authorize(*_):
        assert threading.get_ident() != main_thread, "blocking authorization must run off the event loop"
        return GRANT
    authorization = SimpleNamespace(authorize=Mock(side_effect=authorize))
    data = {k: v for k, v in RECORD.items() if k != "actor"}
    lookup = AsyncMock(return_value=data if outcome == "committed" else None)
    if outcome in {"denied", "unavailable", "network"}:
        kind = {"denied": PermissionError, "unavailable": RuntimeError, "network": ConnectError}[outcome]
        lookup.side_effect = kind("private backend detail")
    response = Response()
    kwargs = dict(project_id="project", request_key=uuid.UUID(KEY), response=response,
                  ops=SimpleNamespace(native_operation_status=lookup),
                  current_user=SimpleNamespace(user_id="user"), authorization=authorization)
    if outcome == "committed":
        assert (await read_operation_status(**kwargs)).model_dump(mode="json")["data"] == data
        assert response.headers["cache-control"] == "no-store"
    else:
        with pytest.raises(HTTPException) as error:
            await read_operation_status(**kwargs)
        assert error.value.status_code == {"missing": 404, "denied": 403, "unavailable": 503, "network": 503}[outcome]
        assert "private backend detail" not in error.value.detail
    authorization.authorize.assert_called_once_with("project", "user", ProjectAction.CONTENT_READ)
    lookup.assert_awaited_once_with("project", GRANT, KEY)


def runtime_grant(*, scoped=False):
    from src.platform.authorization.models import RuntimeGrant, RuntimeMode, RuntimePrincipal
    from src.platform.repository_target.models import (
        ProjectRootTarget,
        ResolvedRepositoryView,
        ScopeTarget,
    )

    target = ScopeTarget("project", "scope") if scoped else ProjectRootTarget("project")
    return RuntimeGrant(RuntimePrincipal("credential", "git_http_token"), target,
                        ResolvedRepositoryView(target, "docs" if scoped else "", (), "rw"), RuntimeMode.READ)


def test_native_operation_status_scope_cannot_borrow_full_repository_result_authority():
    control = Mock()
    with pytest.raises(PermissionError, match="Scope"):
        operation_status(control, "project", runtime_grant(scoped=True), KEY)
    assert not control.mock_calls


@pytest.mark.asyncio
async def test_native_operation_status_runtime_route_and_named_wire_schema(monkeypatch):
    from fastapi import Request, Response

    from src.version_engine.entrypoints.git import operations
    from src.version_engine.entrypoints.http.content_operations import operations_router
    from src.version_engine.entrypoints.http.schemas import NativeOperationStatusEnvelope

    assert operations.operations_router.routes[0].response_model is NativeOperationStatusEnvelope
    assert operations_router.routes[0].response_model is NativeOperationStatusEnvelope
    grant, request = runtime_grant(), Request({"type": "http", "headers": []})
    auth = AsyncMock(return_value={"_runtime_grant": grant})
    monkeypatch.setattr(operations, "resolve_git_project_auth", auth)
    thread = threading.get_ident()
    data = {k: v for k, v in RECORD.items() if k != "actor"}
    def lookup(*args):
        assert threading.get_ident() != thread
        assert args == ("project", grant, KEY)
        return data
    response = Response()
    result = await operations.read_git_operation_status("project", uuid.UUID(KEY), request, response,
                                                        SimpleNamespace(get_native_operation_status=lookup))
    assert result.model_dump(mode="json")["data"] == data
    assert response.headers["cache-control"] == "no-store"
    auth.assert_awaited_once_with("project", request)


@pytest.mark.asyncio
async def test_native_operation_status_routes_cannot_substitute_authorization_planes(monkeypatch):
    from fastapi import HTTPException, Request, Response

    from src.version_engine.entrypoints.git import operations
    from src.version_engine.entrypoints.http.content_operations import read_operation_status

    lookup = Mock()
    monkeypatch.setattr(operations, "resolve_git_project_auth", AsyncMock(return_value={"_runtime_grant": GRANT}))
    with pytest.raises(HTTPException) as denied:
        await operations.read_git_operation_status("project", uuid.UUID(KEY), Request({"type": "http", "headers": []}),
                                                   Response(), lookup)
    assert denied.value.status_code == 403 and not lookup.mock_calls
    with pytest.raises(HTTPException) as denied:
        await read_operation_status("project", uuid.UUID(KEY), Response(), lookup,
                                    SimpleNamespace(user_id="user"), SimpleNamespace(authorize=lambda *_: runtime_grant()))
    assert denied.value.status_code == 403 and not lookup.mock_calls


def test_native_operation_status_pending_is_explicit_and_contains_no_candidate():
    pending = {**RECORD, "status": "pending", "result": None, "ref_request_sha256": None}
    control = SimpleNamespace(operation_status=lambda *_: pending)
    assert operation_status(control, "project", GRANT, KEY)["status"] == "pending"
    pending["product"] = {"tree_oid": "a"*40}
    with pytest.raises(RuntimeError, match="pending"):
        operation_status(control, "project", GRANT, KEY)
