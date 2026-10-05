"""Metadata discovery and explicit ref reads are current reads, not authority."""
import base64
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.version_engine.infrastructure.supabase.repo_manager import VersionRepoManager
from src.version_engine.read.ref_metadata import decode_ref_name, repository_ref_metadata
from tests.repository_hosting.integration.test_ref_transaction_service import grant
from tests.repository_hosting.unit.test_repository_snapshot import fixture, row

pytestmark = pytest.mark.hosting_component


def b64(name):
    return base64.b64encode(name).decode()


@pytest.mark.parametrize("fmt", ["sha1", "sha256"])
def test_metadata_is_current_reader_only_without_storage_or_lease(fmt):
    wire, _, _, _, _ = fixture()
    wire["object_format"] = fmt
    oid = "a" * (40 if fmt == "sha1" else 64)
    wire["refs"].extend([row(b"refs/tags/raw-\xff", {"kind": "oid", "oid": oid}, "blob")])
    wire["gc_token"] = "internal fence"
    client, storage = Mock(), Mock()
    client.rpc.return_value.execute.return_value.data = wire
    manager = VersionRepoManager(storage, SimpleNamespace(client=client))
    manager.get_native_service = Mock(side_effect=AssertionError("metadata must not build storage service"))
    result = manager.get_native_ref_metadata("p", grant("p"))
    assert result["object_format"] == fmt
    assert result["refs"][-1] == {"name": None, "name_b64": b64(b"refs/tags/raw-\xff"),
                                 "state": {"kind": "oid", "oid": oid}, "object_kind": "blob", "peeled_oid": None}
    assert "gc_token" not in result and not storage.mock_calls
    client.rpc.assert_called_once_with("get_admitted_version_repository_snapshot", {"p_project_id": "p", "p_actor": "user:" + grant("p").user_id})
    client.table.assert_not_called()


@pytest.mark.parametrize("defect", ["project", "format", "generation", "sequence", "missing_head", "duplicate", "oid_type", "unknown_type", "head_type", "encoding", "peel", "inconsistent_type"])
def test_metadata_never_fabricates_absence_from_invalid_control_data(defect):
    wire, *_ = fixture()
    wire["refs"].append(row(b"refs/heads/main", {"kind": "oid", "oid": "a" * 40}, "commit"))
    if defect == "project":
        wire["project_id"] = "other"
    elif defect == "format":
        wire["object_format"] = "other"
    elif defect == "generation":
        wire["generation"] = True
    elif defect == "sequence":
        wire["ref_sequence"] = -1
    elif defect == "missing_head":
        wire["refs"] = wire["refs"][1:]
    elif defect == "duplicate":
        wire["refs"] *= 2
    elif defect == "oid_type":
        wire["refs"][1]["state"]["oid"] = 42
    elif defect == "unknown_type":
        wire["refs"][1]["kind"] = None
    elif defect == "head_type":
        wire["refs"][1]["kind"] = "tree"
    elif defect == "encoding":
        wire["refs"][1]["name_b64"] += "=="
    elif defect == "inconsistent_type":
        wire["refs"].append(row(b"refs/tags/conflict", {"kind": "oid", "oid": "a" * 40}, "blob"))
    else:
        wire["refs"][1]["peeled_oid"] = "b" * 40
    with pytest.raises(RuntimeError, match="invalid admitted repository metadata"):
        repository_ref_metadata(SimpleNamespace(read_snapshot=lambda *_: deepcopy(wire)), "p", grant("p"))


@pytest.mark.parametrize("value", ["", "!!!!", "SEVBRA===", b64(b"main"), b64(b"refs/heads/../x")])
def test_ref_selectors_are_canonical_lossless_names(value):
    with pytest.raises(ValueError):
        decode_ref_name(value)


def test_explicit_ref_selection_reaches_one_snapshot_and_rejects_legacy():
    from contextlib import contextmanager

    from fastapi import HTTPException

    from src.version_engine.entrypoints.http.content_read import _product_read

    calls = []
    @contextmanager
    def open_read(project, authorized, *, selector):
        calls.append((project, authorized, selector))
        yield SimpleNamespace(get_read_revision=lambda _: {"target_ref_b64": b64(selector)})
    ops = SimpleNamespace(open_read=open_read)
    selected = b64(b"refs/heads/raw-\xff")
    auth = grant("p")
    with _product_read(ops, "p", auth, ref_b64=selected) as (_, revision):
        assert revision["target_ref_b64"] == selected
    assert calls == [("p", auth, b"refs/heads/raw-\xff")]
    @contextmanager
    def legacy(*args, **kwargs):
        yield SimpleNamespace(get_read_revision=lambda _: None)
    with pytest.raises(HTTPException) as rejected, _product_read(SimpleNamespace(open_read=legacy), "p", auth, ref_b64=b64(b"HEAD")):
        pytest.fail("explicit selector fell back to legacy")
    assert rejected.value.status_code == 400


@pytest.mark.parametrize("suffix", ["refs", "operations/00000000-0000-0000-0000-000000000001"])
def test_full_application_metadata_routes_never_enter_transport_lease(monkeypatch, tmp_path, suffix):
    from unittest.mock import AsyncMock

    from fastapi.testclient import TestClient

    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    from src.main import create_app
    from src.platform.project import write_lease
    from src.version_engine.bootstrap.dependencies import get_repo_manager
    from src.version_engine.entrypoints.git import auth, operations
    from tests.repository_hosting.unit.test_native_operation_status import runtime_grant

    def forbidden(*_args, **_kwargs):
        pytest.fail("metadata read acquired a transport/write lease")
    monkeypatch.setattr(write_lease, "ProjectWriteLease", forbidden)
    resolver = AsyncMock(return_value={"_runtime_grant": runtime_grant()})
    monkeypatch.setattr(auth, "resolve_git_project_auth", resolver)
    monkeypatch.setattr(operations, "resolve_git_project_auth", resolver)
    wire, *_ = fixture()
    wire["project_id"] = "project"
    metadata = repository_ref_metadata(SimpleNamespace(read_snapshot=lambda *_: wire), "project", grant("project"))
    manager = SimpleNamespace(get_native_ref_metadata=lambda *_: metadata, get_native_operation_status=lambda *_: None)
    app = create_app()
    app.dependency_overrides[get_repo_manager] = lambda: manager
    client = TestClient(app)
    try:
        response = client.get("/git/project.git/" + suffix, headers={"Origin": "http://localhost:3000"})
    finally:
        client.close()
    assert response.status_code == (200 if suffix == "refs" else 404)
    assert response.headers["cache-control"] == "no-store"
    assert "X-PuppyOne-Repository-Revision" in response.headers["access-control-expose-headers"]
    from src.platform.project.write_lease import git_project_write_lease
    transport = [r for r in app.routes if r.path.startswith("/git/") and r.path.endswith(("/git-upload-pack", "/git-receive-pack", "/rebuild-cache"))]
    assert transport
    assert all(any(d.call is git_project_write_lease for d in r.dependant.dependencies) for r in transport)


def test_scoped_or_foreign_grants_cannot_discover_full_repository_refs():
    from tests.repository_hosting.unit.test_native_operation_status import runtime_grant

    for project, authorized in (("foreign", grant("p")), ("project", runtime_grant(scoped=True))):
        control = Mock()
        with pytest.raises(PermissionError):
            repository_ref_metadata(control, project, authorized)
        control.read_snapshot.assert_not_called()
