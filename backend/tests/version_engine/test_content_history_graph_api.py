"""HTTP history is derived from pinned native Git refs, for both object formats."""

import base64

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.platform.auth.dependencies import get_current_user
from src.platform.auth.models import CurrentUser
from src.version_engine.bootstrap.dependencies import (
    get_history_graph_service,
    get_product_operation_adapter,
    get_version_admin_service,
)
from src.version_engine.entrypoints.http.content_history import history_router
from src.version_engine.read.admin import VersionAdminService
from src.version_engine.read.history_cursor import HistoryCursorCodec
from src.version_engine.read.history_graph import HistoryGraphService
from src.version_engine.write_engine.git_object_format import encode_object
from tests.authorization_fakes import authorization_for, install_authorization
from tests.repository_hosting.unit.test_native_product_reads import native_reads as native_fixture
from tests.repository_hosting.unit.test_repository_snapshot import row

native_reads = native_fixture


def client_for(native_reads, *, limit=100):
    ops, manager, wire, _calls, objects, root, base, *_ = native_reads

    def put(parents, timestamp, message):
        body = (
            f"tree {root}\n"
            + "".join(f"parent {p}\n" for p in parents)
            + f"author A <a@local> {timestamp} +0000\ncommitter A <a@local> {timestamp} +0000\n\n{message}\n"
        ).encode()
        oid, loose = encode_object("commit", body, object_format=wire["object_format"])
        objects[oid] = loose
        return oid

    left, right = put([base], 10, "Left"), put([base], 20, "Right")
    merge = put([left, right], 2, "Merge despite clock skew")
    wire["refs"] = [
        row(
            b"HEAD",
            {"kind": "symbolic", "target_b64": base64.b64encode(b"refs/heads/main").decode()},
        ),
        row(b"refs/heads/main", {"kind": "oid", "oid": merge}, "commit"),
        row(b"refs/heads/right", {"kind": "oid", "oid": right}, "commit"),
    ]
    graph = HistoryGraphService(
        manager, cursor_codec=HistoryCursorCodec("unit-secret"), max_traversal_nodes=limit
    )
    app = FastAPI()
    app.include_router(history_router, prefix="/api/v1/content")
    app.dependency_overrides[get_history_graph_service] = lambda: graph
    app.dependency_overrides[get_version_admin_service] = lambda: VersionAdminService(manager)
    app.dependency_overrides[get_product_operation_adapter] = lambda: ops
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id="u", role="authenticated"
    )
    install_authorization(app, authorization_for("p", role="viewer"))
    return TestClient(app), [merge, right, left, base], wire


def test_native_topological_api_preserves_merge_parents_and_pages(native_reads):
    client, ids, _wire = client_for(native_reads)
    first = client.get("/api/v1/content/p/commits", params={"order": "topo", "limit": 2})
    assert first.status_code == 200, first.text
    page = first.json()["data"]
    assert [c["commit_id"] for c in page["commits"]] == ids[:2]
    assert page["commits"][0]["parent_ids"] == [ids[2], ids[1]]
    assert page["head_commit_id"] == ids[0] and page["total"] == 4
    second = client.get(
        "/api/v1/content/p/commits", params={"cursor": page["next_cursor"], "limit": 2}
    )
    assert second.status_code == 200, second.text
    end = second.json()["data"]
    assert [c["commit_id"] for c in end["commits"]] == ids[2:]
    assert not end["refs_included"] and not end["has_more"]
    assert page["snapshot_id"] == end["snapshot_id"]


def test_native_linear_history_contains_real_git_ancestry(native_reads):
    client, ids, _wire = client_for(native_reads)
    response = client.get("/api/v1/content/p/commits")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert [c["commit_id"] for c in data["commits"]] == list(reversed(ids))
    assert data["root_hash"] == native_reads[5]


def test_native_cursor_rejects_tampering_and_changed_refs(native_reads):
    client, ids, wire = client_for(native_reads)
    page = client.get("/api/v1/content/p/commits", params={"order": "topo", "limit": 2}).json()[
        "data"
    ]
    assert client.get("/api/v1/content/p/commits", params={"cursor": "forged"}).status_code == 400
    wire["refs"][1] = row(b"refs/heads/main", {"kind": "oid", "oid": ids[2]}, "commit")
    assert (
        client.get("/api/v1/content/p/commits", params={"cursor": page["next_cursor"]}).status_code
        == 409
    )


def test_native_history_traversal_has_budget(native_reads):
    client, _, _ = client_for(native_reads, limit=1)
    response = client.get("/api/v1/content/p/commits", params={"order": "topo"})
    assert response.status_code == 422
