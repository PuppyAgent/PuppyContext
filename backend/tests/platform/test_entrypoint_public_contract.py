"""Contract captured from c28e38a3 BEFORE moving modules; unchanged clients use it."""
import hashlib
import json
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


def test_all_pre_migration_public_routes_and_schema_contracts_are_unchanged():
    from src.main import app

    expected = json.loads((Path(__file__).with_name("entrypoint_contract_c28e38a3.json")).read_text())
    # ISSUE-059 adds optional binding metadata needed by the resource client.
    # Keep the pre-migration fixture immutable and allow only this reviewed
    # additive delta; every original property, required field and route still
    # has to match the captured contract.
    import copy
    openapi = copy.deepcopy(app.openapi())
    binding = openapi["components"]["schemas"]["SyncResponse"]
    for field in ("trigger", "last_synced_at", "created_at", "updated_at"):
        assert field in binding["properties"]
        assert field not in binding.get("required", [])
        binding["properties"].pop(field)
    # Canonical S2 routes are an explicit additive contract, not a wildcard
    # exemption. Existing route/schema fingerprints must remain unchanged.
    delta = json.loads(Path(__file__).with_name("synchronize_contract_delta.json").read_text())
    for category in ("paths", "schemas"):
        assert not expected["contract"][category].keys() & delta[category].keys()
        expected["contract"][category].update(delta[category])
    assert contract(openapi) == expected["contract"]


def test_access_domain_kind_is_serialized_as_legacy_provider():
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from unittest.mock import Mock
    from src.platform.access.models import AccessSurface
    from src.platform.access.project_router import list_connectors
    from src.platform.repository_target.models import ProjectRootTarget

    surface = AccessSurface(
        id="surface-1", target=ProjectRootTarget(project_id="project-1"), kind="cli",
        name="CLI", direction="bidirectional", config={}, policy={}, oauth_connection_id=None,
        trigger={"type": "manual"}, status="active", last_run_at=None, last_run_id=None,
        error_message=None, created_by="user-1", created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    service = Mock()
    service.list.return_value = [surface]
    response = list_connectors(provider="cli", direction=None, include_non_access=False,
                               authorized=SimpleNamespace(project=SimpleNamespace(id="project-1")), service=service)
    payload = response.model_dump(mode="json")
    assert payload["data"][0]["provider"] == "cli"
    assert "kind" not in payload["data"][0]
    service.list.assert_called_once_with("project-1", kind="cli", direction=None, access_surface_only=True)
    assert payload["message"] == "Connectors listed"


def test_sync_run_connection_id_is_authorized_and_serialized_at_http_boundary(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from src.platform.auth.models import CurrentUser
    from src.platform.synchronize import router
    from src.platform.synchronize.run_repository import SyncRun
    from tests.authorization_fakes import authorization_for

    run = SyncRun(id="run-1", connection_id="source-1", stdout="hello")
    repository = Mock()
    repository.get_by_id.return_value = run
    monkeypatch.setattr(router, "_get_run_repo", lambda: repository)
    service = Mock()
    service.repository.get_by_id.return_value = SimpleNamespace(id="source-1", project_id="project-1")
    response = router.get_connection_run(
        "run-1", service=service, authorization=authorization_for("project-1", role="viewer"),
        current_user=CurrentUser(user_id="user-1", role="authenticated"),
    )
    payload = response.model_dump(mode="json")["data"]
    assert payload["access_point_id"] == "source-1"
    assert "connection_id" not in payload
    assert payload["stdout"] == "hello"
    service.repository.get_by_id.assert_called_once_with("source-1")
