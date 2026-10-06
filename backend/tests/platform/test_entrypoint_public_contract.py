"""Immutable historical contracts plus explicit reviewed publication/retirement deltas."""
import hashlib
import json
from datetime import UTC
from pathlib import Path


def normalize(value):
    if isinstance(value, dict):
        return {key: normalize(item) for key, item in value.items() if key not in {"description", "summary", "tags"}}
    if isinstance(value, list):
        return [normalize(item) for item in value]
    return value


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def contract(openapi):
    paths = normalize(openapi["paths"])
    # Existing FastAPI multi-method proxy routes choose their ID from a set.
    # Exclude ONLY those unstable IDs; their URLs, verbs and full schemas remain checked.
    for name in ("/api/v1/mcp/proxy", "/api/v1/mcp/proxy/{path}"):
        for operation in paths[name].values():
            operation.pop("operationId", None)
    return {
        "paths": {path: fingerprint(value) for path, value in paths.items()},
        "schemas": {name: fingerprint(value) for name, value in normalize(openapi["components"]["schemas"]).items()},
        "securitySchemes": normalize(openapi["components"].get("securitySchemes", {})),
    }


def test_exact_resource_retirement_preserves_every_unrelated_contract():
    from src.main import app

    expected = json.loads((Path(__file__).with_name("entrypoint_contract_c28e38a3.json")).read_text())
    # Keep the original and the four published S2 fixtures immutable. Removal
    # is exact and reviewed, never a wildcard exemption for unrelated APIs.
    for filename in ("synchronize_contract_delta.json", "access_contract_delta.json", "github_database_contract_delta.json", "aggregate_contract_delta.json"):
        delta = json.loads(Path(__file__).with_name(filename).read_text())
        for category in ("paths", "schemas"):
            assert not expected["contract"][category].keys() & delta[category].keys()
            expected["contract"][category].update(delta[category])
    retirement = json.loads(Path(__file__).with_name("retired_entrypoint_contract_delta.json").read_text())
    for category in ("paths", "schemas"):
        delta = retirement[category]
        assert delta["changed"] == {}  # canonical and unrelated wires remain identical
        for name, digest in delta["removed"].items():
            assert expected["contract"][category].pop(name) == digest
        for name, digest in delta["added"].items():
            assert name not in expected["contract"][category]
            expected["contract"][category][name] = digest
    # ISSUE-062 adds ref/base metadata and byte-path alternatives only to these
    # content reads. Match exact before/after digests; do not rewrite any
    # historical publication/retirement fixture or exempt other APIs.
    native = json.loads(Path(__file__).with_name("native_content_read_contract_delta.json").read_text())
    assert set(native["paths"]) == {
        f"/api/v1/content/{{project_id}}/{action}" for action in ("ls", "cat", "raw", "stat", "tree")
    }
    assert set(native["schemas"]) == {"ListDirResponse", "ReadFileResponse", "StatResponse", "TreeResponse", "VersionEntryResponse"}
    for category in ("paths", "schemas"):
        for name, change in native[category].items():
            assert expected["contract"][category][name] == change["before"]
            expected["contract"][category][name] = change["after"]
    # The optional bulk precondition follows the existing single-write contract.
    # Only this request schema changes; paths and historical fixtures do not.
    bulk = json.loads(Path(__file__).with_name("product_bulk_base_contract_delta.json").read_text())
    assert bulk["paths"] == {} and set(bulk["schemas"]) == {"BulkWriteRequest"}
    change = bulk["schemas"]["BulkWriteRequest"]
    assert expected["contract"]["schemas"]["BulkWriteRequest"] == change["before"]
    expected["contract"]["schemas"]["BulkWriteRequest"] = change["after"]
    product = json.loads(Path(__file__).with_name("native_product_write_contract_delta.json").read_text())
    assert product["paths"] == {}
    assert set(product["schemas"]) == {"WriteFileRequest", "BulkWriteRequest", "MkdirRequest", "MoveRequest", "RemoveRequest"}
    assert set(product["added_schemas"]) == {"NativeProductWrite"}
    for name, change in product["schemas"].items():
        assert expected["contract"]["schemas"][name] == change["before"]
        expected["contract"]["schemas"][name] = change["after"]
    for name, digest in product["added_schemas"].items():
        assert name not in expected["contract"]["schemas"]
        expected["contract"]["schemas"][name] = digest
    status = json.loads(Path(__file__).with_name("native_operation_status_contract_delta.json").read_text())
    assert set(status["paths"]) == {"/api/v1/content/{project_id}/operations/{request_key}",
                                    "/git/{project_id}.git/operations/{request_key}"}
    assert set(status["schemas"]) == {"NativeOperationStatusResponse", "NativeOperationStatusEnvelope"}
    for category in ("paths", "schemas"):
        assert not expected["contract"][category].keys() & status[category].keys()
        expected["contract"][category].update(status[category])
    refs = json.loads(Path(__file__).with_name("native_ref_read_contract_delta.json").read_text())
    assert set(refs["changed_paths"]) == {f"/api/v1/content/{{project_id}}/{action}" for action in ("ls", "cat", "raw", "stat", "tree")}
    assert set(refs["paths"]) == {"/api/v1/content/{project_id}/refs", "/git/{project_id}.git/refs"}
    assert set(refs["schemas"]) == {"NativeRepositoryRefResponse", "NativeRepositoryMetadataResponse", "NativeRepositoryMetadataEnvelope"}
    for name, change in refs["changed_paths"].items():
        assert expected["contract"]["paths"][name] == change["before"]
        expected["contract"]["paths"][name] = change["after"]
    for category in ("paths", "schemas"):
        assert not expected["contract"][category].keys() & refs[category].keys()
        expected["contract"][category].update(refs[category])
    bare = json.loads(Path(__file__).with_name("native_bare_management_contract_delta.json").read_text())
    assert bare["paths"] == {}
    assert set(bare["changed_paths"]) == {"/api/v1/content/{project_id}/head"}
    assert set(bare["schemas"]) == {"NativeRepositoryCreate", "RepositoryHeadState", "RepositoryHeadUpdate"}
    assert set(bare["changed_schemas"]) == {"ProjectCreate"}
    for name, change in bare["changed_paths"].items():
        assert expected["contract"]["paths"][name] == change["before"]
        expected["contract"]["paths"][name] = change["after"]
    for name, change in bare["changed_schemas"].items():
        assert expected["contract"]["schemas"][name] == change["before"]
        expected["contract"]["schemas"][name] = change["after"]
    for category in ("paths", "schemas"):
        assert not expected["contract"][category].keys() & bare[category].keys()
        expected["contract"][category].update(bare[category])
    assert contract(app.openapi()) == expected["contract"]


def test_access_domain_kind_and_target_are_serialized_without_legacy_provider():
    from datetime import datetime
    from types import SimpleNamespace
    from unittest.mock import Mock

    from src.platform.access.models import AccessSurface
    from src.platform.access.public_router import list_project_access_surfaces
    from src.platform.repository_target.models import ProjectRootTarget

    surface = AccessSurface(
        id="surface-1", target=ProjectRootTarget(project_id="project-1"), kind="cli",
        name="CLI", direction="bidirectional", config={}, policy={}, oauth_connection_id=None,
        trigger={"type": "manual"}, status="active", last_run_at=None, last_run_id=None,
        error_message=None, created_by="user-1", created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    service = Mock()
    service.list.return_value = [surface]
    response = list_project_access_surfaces(kind="cli", direction=None,
                               authorized=SimpleNamespace(project=SimpleNamespace(id="project-1")), service=service)
    payload = response.model_dump(mode="json")
    assert payload["data"][0]["kind"] == "cli"
    assert payload["data"][0]["target"] == {"kind": "project_root", "project_id": "project-1"}
    assert "provider" not in payload["data"][0]
    # Read the domain inventory, then reject unexpected historical source rows;
    # silently filtering them would turn required repair into empty success.
    service.list.assert_called_once_with("project-1", kind="cli", direction=None, access_surface_only=False)
    assert payload["message"] == "Access surfaces listed"


def test_run_binding_id_is_authorized_and_serialized_at_http_boundary(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from src.platform.auth.models import CurrentUser
    from src.platform.synchronize import router
    from src.platform.synchronize.public_router import get_synchronize_run
    from src.platform.synchronize.run_repository import SyncRun
    from tests.authorization_fakes import authorization_for

    run = SyncRun(id="run-1", synchronize_binding_id="source-1", stdout="hello")
    repository = Mock()
    repository.get_by_id.return_value = run
    monkeypatch.setattr(router, "_get_run_repo", lambda: repository)
    service = Mock()
    service.repository.get_by_id.return_value = SimpleNamespace(id="source-1", project_id="project-1")
    response = get_synchronize_run(
        "run-1", service=service, authorization=authorization_for("project-1", role="viewer"),
        current_user=CurrentUser(user_id="user-1", role="authenticated"),
    )
    payload = response.model_dump(mode="json")["data"]
    assert payload["synchronize_binding_id"] == "source-1"
    assert "connection_id" not in payload and "access_point_id" not in payload
    assert payload["stdout"] == "hello"
    service.repository.get_by_id.assert_called_once_with("source-1")
