"""Canonical HTTP lifecycle and explicit legacy-route rejection."""
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from src.platform.synchronize.run_repository import SyncRun
from tests.authorization_fakes import authorization_for, install_authorization
from tests.platform.test_synchronize_identity_http import environment as environment

BASE = "/api/v1/synchronize"
BODY = {
    "project_id": "project-1", "provider": "url", "target_path": "without-scope",
    "config": {"source": {"resource_url": "https://example.test", "resource_name": "Source",
                            "metadata": {"connection_id": "user-metadata-not-an-identity"}}, "options": {}},
    "sync_mode": "manual", "trigger": {"type": "manual"},
}


@pytest.fixture
def canonical(environment):
    _app, bindings, runs, _queue = environment
    # Minimal persistence facts; lifecycle and authorization remain real.
    def update(binding_id, **fields):
        binding = bindings.items[binding_id]
        if "trigger" in fields:
            binding.trigger = fields["trigger"]
        if "target_path" in fields:
            binding.path = fields["target_path"]
    bindings.update = update
    runs.list_failed_for_connections = lambda ids, **_: [r for r in runs.items.values()
        if r.synchronize_binding_id in ids and r.status == "failed"]
    return environment


def test_create_reload_edit_retry_history_pause_resume_delete(canonical):
    app, bindings, runs, queue = canonical
    with TestClient(app) as client:
        response = client.post(f"{BASE}/bindings", json=BODY)
        assert response.status_code == 200, response.text
        created = response.json()["data"]
        assert "sync" not in created
        binding = created["binding"]
        binding_id = binding["id"]
        assert binding["last_synchronize_commit_id"] == ""
        assert "last_sync_commit_id" not in binding
        assert binding["config"]["source"]["metadata"]["connection_id"] == "user-metadata-not-an-identity"
        execution = created["execution_result"]
        assert execution["synchronize_binding_id"] == binding_id
        assert execution["synchronize_run_id"] == "run-1"
        assert not {"connection_id", "access_point_id", "run_id"} & execution.keys()
        assert client.get("/api/v1/integrations/connections?project_id=project-1").status_code == 404
    with TestClient(app) as client:
        path = f"{BASE}/bindings/{binding_id}"
        assert client.get(f"{BASE}/bindings?project_id=project-1").json()["data"][0]["id"] == binding_id
        edited = client.patch(path, json={"target_path": "new-destination"})
        assert edited.json()["data"]["path"] == "new-destination"
        assert client.patch(f"{path}/trigger", json={"sync_mode": "manual"}).status_code == 200
        runs.items["run-1"].status = "failed"
        retry = client.post(f"{path}/refresh")
        assert retry.json()["data"]["results"][0]["synchronize_binding_id"] == binding_id
        history = client.get(f"{path}/runs").json()["data"]
        assert {r["id"] for r in history} == {"run-1", "run-2"}
        assert all(r["synchronize_binding_id"] == binding_id and "access_point_id" not in r for r in history)
        detail = client.get(f"{BASE}/runs/run-1").json()["data"]
        assert detail["synchronize_binding_id"] == binding_id
        failed = client.get(f"{BASE}/failed-runs?project_id=project-1").json()["data"][0]
        assert failed["synchronize_binding_id"] == binding_id
        assert failed["synchronize_binding_name"] == "Source"
        assert failed["target_path"] == "new-destination"
        status = client.get(f"{BASE}/status?project_id=project-1").json()["data"]
        assert status["bindings"][0]["id"] == binding_id
        assert "syncs" not in status and "uploads" not in status
        assert "access_key" not in status["bindings"][0]
        runs.items["run-2"].status = "failed"
        assert client.post(f"{path}/pause").status_code == 200
        assert client.post(f"{path}/refresh").status_code == 409
        assert client.post(f"{path}/resume").status_code == 200
        assert client.delete(path).status_code == 200
        assert not bindings.items
    assert queue.enqueue_sync_run.call_count == 3


@pytest.mark.parametrize("field", ["connection_id", "access_point_id", "sync_id"])
def test_old_identity_query_cannot_silently_trigger_all_bindings(canonical, field):
    app, _, _, queue = canonical
    with TestClient(app) as client:
        response = client.post(f"{BASE}/pull?project_id=project-1&{field}=not-a-binding")
        assert response.status_code == 422
    queue.enqueue_sync_run.assert_not_called()


@pytest.mark.parametrize("extra", [{"sync_mode": "import_once"}, {"access_point_id": "access-1"}, {"target_folder_path": "legacy"}])
def test_create_rejects_noncanonical_or_import_only_payload_before_persistence(canonical, extra):
    app, bindings, _, queue = canonical
    with TestClient(app) as client:
        response = client.post(f"{BASE}/bindings", json={**BODY, **extra})
        assert response.status_code == 422
    assert not bindings.items
    queue.enqueue_sync_run.assert_not_called()


@pytest.mark.parametrize("operation,method", [("", "delete"), ("", "patch"), ("pause", "post"),
    ("resume", "post"), ("refresh", "post"), ("runs", "get"), ("trigger", "patch")])
@pytest.mark.parametrize("resource_id", ["access-only", "missing", "foreign-binding"])
def test_wrong_or_foreign_id_never_targets_another_resource(canonical, resource_id, operation, method):
    app, bindings, _, queue = canonical
    foreign = bindings.create(project_id="other-project", provider="url")
    bindings.items["foreign-binding"] = foreign
    with TestClient(app) as client:
        path = f"{BASE}/bindings/{resource_id}" + (f"/{operation}" if operation else "")
        kwargs = {"json": {"sync_mode": "manual"} if operation == "trigger" else {}} if method == "patch" else {}
        response = getattr(client, method)(path, **kwargs)
        assert response.status_code in {403, 404}, response.text
    assert foreign.status == "active"
    queue.enqueue_sync_run.assert_not_called()


def test_viewer_can_read_but_cannot_create_mutate_or_trigger(canonical):
    app, bindings, _, queue = canonical
    binding = bindings.create(project_id="project-1", provider="url", path="")
    install_authorization(app, authorization_for("project-1", role="viewer"))
    with TestClient(app) as client:
        assert client.get(f"{BASE}/bindings?project_id=project-1").status_code == 200
        assert client.post(f"{BASE}/bindings", json=BODY).status_code == 403
        assert client.post(f"{BASE}/bindings/{binding.id}/refresh").status_code == 403
        assert client.post(f"{BASE}/pull?project_id=project-1").status_code == 403
    queue.enqueue_sync_run.assert_not_called()


def test_absent_binding_path_is_not_serialized_as_a_project_root(canonical):
    app, bindings, _, queue = canonical
    bindings.create(project_id="project-1", provider="url", path=None)
    with TestClient(app) as client:
        response = client.get(f"{BASE}/bindings?project_id=project-1")
        assert response.status_code == 409
        assert "requires repair" in response.text
    queue.enqueue_sync_run.assert_not_called()


def test_foreign_run_requires_its_own_project_grant(canonical):
    app, bindings, runs, _ = canonical
    foreign = bindings.create(project_id="other-project", provider="url")
    runs.items["foreign-run"] = SyncRun(id="foreign-run", synchronize_binding_id=foreign.id, stdout="private")
    with TestClient(app) as client:
        response = client.get(f"{BASE}/runs/foreign-run")
        assert response.status_code in {403, 404}
        assert "private" not in response.text


def test_initial_enqueue_failure_keeps_cleanup_and_503_semantics(canonical):
    app, bindings, runs, queue = canonical
    queue.enqueue_sync_run = AsyncMock(side_effect=RuntimeError("queue unavailable"))
    with TestClient(app) as client:
        response = client.post(f"{BASE}/bindings", json=BODY)
        assert response.status_code == 503
    assert not bindings.items
    assert runs.items["run-1"].status == "failed"


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("fields", [
    {"provider": "database", "config": {"db_config": {"ciphertext": "never-return-this"}}},
    {"provider": "url", "config": {"db_config": {"ciphertext": "never-return-this"}}},
    {"provider": "url", "trigger": {"type": "import_once"}},
])
def test_unclassified_import_sources_are_not_reinterpreted_as_bindings(canonical, legacy, fields):
    app, bindings, _, queue = canonical
    bindings.create(project_id="project-1", provider="url")
    ambiguous = bindings.create(project_id="project-1", **fields)
    base = "/api/v1/integrations" if legacy else BASE
    resource = "connections" if legacy else "bindings"
    expected = 404 if legacy else 409
    with TestClient(app) as client:
        for path in (f"/{resource}?project_id=project-1", "/status?project_id=project-1", "/failed-runs?project_id=project-1"):
            response = client.get(base + path)
            assert response.status_code == expected
            if not legacy:
                assert response.json()["detail"]["code"] == "SOURCE_CLASSIFICATION_REQUIRED"
            assert "never-return-this" not in response.text
        assert client.delete(f"{base}/{resource}/{ambiguous.id}").status_code == expected
        assert client.post(f"{base}/{resource}/{ambiguous.id}/pause").status_code == expected
        # A valid binding appears first: classification must gate the whole
        # batch, rather than enqueue it and fail halfway through the request.
        assert client.post(f"{base}/pull?project_id=project-1").status_code == expected
    assert ambiguous.id in bindings.items and ambiguous.status == "active"
    queue.enqueue_sync_run.assert_not_called()


@pytest.mark.parametrize("query", [
    "synchronize_binding_id=&project_id=project-1",
    "synchronize_binding_id=%20&project_id=project-1",
    "binding_id=unknown&project_id=project-1",
    "misspelled_id=unknown&project_id=project-1",
    "project_id=project-1&project_id=project-2",
    "provider=&project_id=project-1",
])
def test_invalid_pull_selectors_never_expand_to_a_project_wide_write(canonical, query):
    app, bindings, _, queue = canonical
    bindings.create(project_id="project-1", provider="url")
    with TestClient(app) as client:
        assert client.post(f"{BASE}/pull?{query}").status_code == 422
    queue.enqueue_sync_run.assert_not_called()


def test_explicit_single_binding_and_project_pull_remain_distinct(canonical):
    app, bindings, _, queue = canonical
    first = bindings.create(project_id="project-1", provider="url")
    second = bindings.create(project_id="project-1", provider="url")
    with TestClient(app) as client:
        single = client.post(f"{BASE}/pull?synchronize_binding_id={first.id}").json()["data"]
        assert [row["synchronize_binding_id"] for row in single["results"]] == [first.id]
        project = client.post(f"{BASE}/pull?project_id=project-1").json()["data"]
        assert {row["synchronize_binding_id"] for row in project["results"]} == {first.id, second.id}
    assert queue.enqueue_sync_run.call_count == 2


def test_reviewed_historical_binding_is_visible_private_and_non_executable(canonical):
    app, bindings, runs, queue = canonical
    binding = bindings.create(project_id="project-1", provider="database", path="historical",
        status="disabled", legacy_read_only_reason="Reviewed binding-only history; no Import source",
        config={"db_config": {"ciphertext": "opaque-private-config"}, "other": "opaque-private-config",
                "source": {"resource_name": "Historical binding"}},
        trigger={"type": "manual", "old_secret": "opaque-private-config"})
    runs.items["historical-run"] = SyncRun(id="historical-run", synchronize_binding_id=binding.id, status="failed")
    with TestClient(app) as client:
        listed = client.get(f"{BASE}/bindings?project_id=project-1")
        assert listed.status_code == 200 and "opaque-private-config" not in listed.text
        assert listed.json()["data"][0]["id"] == binding.id
        status = client.get(f"{BASE}/status?project_id=project-1")
        assert status.status_code == 200 and "opaque-private-config" not in status.text
        assert status.json()["data"]["bindings"][0]["id"] == binding.id
        history = client.get(f"{BASE}/bindings/{binding.id}/runs")
        assert history.status_code == 200 and history.json()["data"][0]["id"] == "historical-run"
        for action in ("pause", "resume", "refresh"):
            response = client.post(f"{BASE}/bindings/{binding.id}/{action}")
            assert response.status_code == 409 and response.json()["detail"]["code"] == "LEGACY_BINDING_READ_ONLY"
        assert client.patch(f"{BASE}/bindings/{binding.id}", json={"target_path": "new"}).status_code == 409
        assert client.delete(f"{BASE}/bindings/{binding.id}").status_code == 409
    assert binding.status == "disabled" and set(runs.items) == {"historical-run"}
    queue.enqueue_sync_run.assert_not_called()


def test_canonical_openapi_has_no_legacy_identity_schema_fields(canonical):
    app, *_ = canonical
    openapi = app.openapi()
    schemas = openapi["components"]["schemas"]
    for name, schema in schemas.items():
        if not name.startswith("Synchronize"):
            continue
        assert not {"connection_id", "access_point_id", "last_sync_commit_id", "sync", "syncs"} & schema.get("properties", {}).keys()
    assert "synchronize_binding_id" in schemas["SynchronizeRun"]["required"]
    parameters = openapi["paths"][f"{BASE}/bindings/{{synchronize_binding_id}}/refresh"]["post"]["parameters"]
    assert {p["name"] for p in parameters} == {"synchronize_binding_id"}
    assert "post" in openapi["paths"][f"{BASE}/bootstrap"]
    assert "post" in openapi["paths"][f"{BASE}/push/{{path}}"]
