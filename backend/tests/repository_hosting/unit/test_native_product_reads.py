"""Product reads use one admitted ref snapshot, never a transport/legacy view."""

import base64
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.version_engine.adapters.product.operation_adapter import ProductOperationAdapter
from src.version_engine.domain.errors import ObjectNotFoundError
from src.version_engine.write_engine.git_object_format import TreeEntry, encode_object, encode_tree
from tests.repository_hosting.integration.test_ref_transaction_service import grant
from tests.repository_hosting.unit.test_repository_snapshot import fixture, row

pytestmark = pytest.mark.hosting_component


@pytest.fixture(params=["sha1", "sha256"])
def native_reads(request):
    fmt = request.param
    wire, calls, objects, control, backend = fixture()
    wire["object_format"] = fmt

    def put(kind, body):
        oid, raw = encode_object(kind, body, object_format=fmt)
        objects[oid] = raw
        return oid

    blob = put("blob", b"raw\x00bytes")
    link = put("blob", b"../outside")
    child = put("tree", encode_tree([TreeEntry("inside", b"100755", blob)], object_format=fmt))
    entries = [
        TreeEntry("file", b"100755", blob),
        TreeEntry("link", b"120000", link),
        TreeEntry("module", b"160000", "a" * (40 if fmt == "sha1" else 64)),
        TreeEntry("dir", b"40000", child),
        TreeEntry(".keep", b"100644", blob),
        TreeEntry(b"raw-\xff".decode("utf-8", "surrogateescape"), b"100644", blob),
    ]
    root = put("tree", encode_tree(entries, object_format=fmt))
    commit = put(
        "commit",
        f"tree {root}\nauthor A <a@local> 1 +0000\ncommitter A <a@local> 1 +0000\n\nmessage\n".encode(),
    )
    wire["refs"] = [
        row(
            b"HEAD",
            {"kind": "symbolic", "target_b64": base64.b64encode(b"refs/heads/trunk").decode()},
        ),
        row(b"refs/heads/trunk", {"kind": "oid", "oid": commit}, "commit"),
    ]
    manager = Mock()
    manager.get_native_service.return_value = SimpleNamespace(
        control=control, backend=backend, object_format=fmt
    )
    manager.get_repo.side_effect = AssertionError("legacy repository used")
    manager.get_server_repo.side_effect = AssertionError("transport repository used")
    return (
        ProductOperationAdapter(manager),
        manager,
        wire,
        calls,
        objects,
        root,
        commit,
        blob,
        child,
    )


def test_product_read_uses_captured_ref_bytes_modes_and_head(native_reads):
    ops, manager, wire, calls, _objects, root, commit, _blob, _child = native_reads
    with ops.open_read("p", grant("p")) as read:
        assert read.get_root_hash("p") == root
        assert read.get_head_commit_id("p") == commit
        assert read.revision.ref_name == b"refs/heads/trunk"
        wire["refs"][0] = row(
            b"HEAD",
            {"kind": "symbolic", "target_b64": base64.b64encode(b"refs/heads/other").decode()},
        )
        shown = {e.name: e for e in read.list_dir("p", include_size=True)}
        assert shown["file"].git_mode == "100755"
        assert shown["link"].git_mode == "120000"
        assert shown["module"].type == "gitlink"
        assert shown["module"].size_bytes is None
        assert ".keep" in shown
        raw = b"raw-\xff".decode("utf-8", "surrogateescape")
        assert read.read_file("p", raw) == b"raw\x00bytes"
        assert read.read_file("p", "link") == b"../outside"
        assert read.read_file("p", "dir/inside") == b"raw\x00bytes"
        assert read.get_head_commit_id("p") == commit
        assert read.revision.head_guard.target == b"refs/heads/trunk"
        assert read.read_file_range("p", "file", start=1, limit=3).content == b"aw\x00"
        with pytest.raises(FileNotFoundError):
            read.read_file("p", "module")
        with pytest.raises(FileNotFoundError):
            read.read_file("p", "link/inside")
        assert read.stat("p", "missing") is None
        assert len(read.list_tree("p")) == 7
        with pytest.raises(ValueError, match="entry budget exceeded"):
            read.list_tree("p", max_entries=2)
    assert calls[0] == "begin" and calls[-1] == "release"
    with pytest.raises(RuntimeError, match="snapshot is closed"):
        read.read_file("p", "file")
    manager.get_repo.assert_not_called()
    manager.get_server_repo.assert_not_called()


def test_product_read_missing_tree_fails_without_legacy_fallback(native_reads):
    ops, manager, _wire, calls, objects, _root, _commit, _blob, child = native_reads
    del objects[child]

    def unavailable(oid):
        if oid not in objects:
            raise ObjectNotFoundError("canonical tree unavailable")
        return objects[oid]

    manager.get_native_service.return_value.backend.get_durable = unavailable
    with pytest.raises(ObjectNotFoundError), ops.open_read("p", grant("p")) as read:
        read.list_dir("p")
    assert calls[-1] == "release"
    manager.get_repo.assert_not_called()
    manager.get_server_repo.assert_not_called()


def test_product_read_denies_foreign_grants_and_projects(native_reads):
    ops, manager, _wire, calls, _objects, _root, _commit, _blob, _child = native_reads
    with pytest.raises(PermissionError), ops.open_read("p", grant("other")):
        pytest.fail("foreign grant admitted")
    assert not calls
    manager.get_native_service.assert_not_called()
    with (
        ops.open_read("p", grant("p")) as read,
        pytest.raises(PermissionError, match="Project binding mismatch"),
    ):
        read.list_dir("other")


def test_product_read_unborn_is_intrinsic_and_metadata_failures_do_not_downgrade(native_reads):
    ops, manager, wire, calls, _objects, _root, _commit, _blob, _child = native_reads
    wire["refs"] = wire["refs"][:1]
    with ops.open_read("p", grant("p")) as read:
        assert read.get_head_commit_id("p") == ""
        assert read.list_dir("p") == []
    assert calls == ["begin", "release"]
    manager.get_native_service.side_effect = RuntimeError("metadata unavailable")
    with pytest.raises(RuntimeError, match="metadata unavailable"), ops.open_read("p", grant("p")):
        pytest.fail("metadata outage downgraded")
    manager.get_repo.assert_not_called()


@pytest.mark.parametrize("method", ["list_dir", "read_file", "raw_file", "stat", "full_tree"])
def test_native_content_routes_use_admitted_product_view(native_reads, method):
    from starlette.requests import Request

    from src.version_engine.entrypoints.http import content_read

    ops, manager, _wire, calls, _objects, _root, commit, _blob, _child = native_reads
    kwargs = dict(
        project_id="p",
        path="file",
        ops=ops,
        authorization=SimpleNamespace(authorize=lambda *_: grant("p")),
        current_user=SimpleNamespace(user_id="u"),
    )
    if method in {"list_dir", "full_tree"}:
        kwargs["path"] = ""
    if method == "full_tree":
        kwargs["max_depth"] = -1
    if method == "raw_file":
        kwargs["request"] = Request({"type": "http", "headers": []})
    response = getattr(content_read, method)(**kwargs)
    if method == "raw_file":
        assert response.body == b"raw\x00bytes"
    else:
        assert response.data.head_commit_id == commit
        assert (
            response.data.repository_revision["target_ref_b64"]
            == base64.b64encode(b"refs/heads/trunk").decode()
        )
        if method == "read_file":
            assert response.data.content_text == "raw\x00bytes"
        elif method == "stat":
            assert response.data.exists and response.data.git_mode == "100755"
        else:
            raw = next(e for e in response.data.entries if e.name == "raw-\\xff")
            assert raw.path_bytes_b64 == base64.b64encode(b"raw-\xff").decode()
        # Including non-UTF8 Git names must not break the HTTP JSON encoder.
        response.model_dump_json()
    assert calls[0] == "begin" and calls[-1] == "release"
    assert calls.count("begin") == 1
    manager.get_native_service.assert_called_once_with("p")
    manager.get_repo.assert_not_called()
    manager.get_server_repo.assert_not_called()


@pytest.mark.parametrize("method", ["read_file", "raw_file", "stat"])
def test_native_content_byte_paths_are_lossless_and_read_only_grants_work(native_reads, method):
    from starlette.requests import Request

    from src.version_engine.entrypoints.http import content_read

    ops, manager, _wire, _calls, _objects, _root, commit, _blob, _child = native_reads
    encoded = base64.b64encode(b"raw-\xff").decode()
    kwargs = dict(
        project_id="p",
        path="",
        path_bytes_b64=encoded,
        ops=ops,
        authorization=SimpleNamespace(authorize=lambda *_: grant("p", writable=False)),
        current_user=SimpleNamespace(user_id="u"),
    )
    if method == "raw_file":
        kwargs["request"] = Request({"type": "http", "headers": []})
    response = getattr(content_read, method)(**kwargs)
    if method == "raw_file":
        assert response.body == b"raw\x00bytes"
        assert "\r" not in response.headers["content-disposition"]
        assert "\n" not in response.headers["content-disposition"]
    else:
        assert response.data.path_bytes_b64 == encoded
        assert response.data.head_commit_id == commit
        assert response.data.path == "raw-\\xff"
        response.model_dump_json()
    manager.get_repo.assert_not_called()


def test_native_missing_objects_do_not_consult_legacy_incident_classification(
    native_reads, monkeypatch
):
    from fastapi import HTTPException

    from src.version_engine.entrypoints.http import content_read

    ops, manager, _wire, calls, _objects, _root, _commit, _blob, _child = native_reads
    manager.get_native_service.return_value.backend.get_durable = Mock(
        side_effect=ObjectNotFoundError("missing")
    )
    incident = Mock(side_effect=AssertionError("legacy incident consulted"))
    monkeypatch.setattr(content_read, "_is_marked_irrecoverable", incident, raising=False)
    monkeypatch.setattr(content_read, "_has_unsupported_legacy_root", incident, raising=False)
    with pytest.raises(HTTPException) as error:
        content_read.list_dir(
            "p",
            path="",
            ops=ops,
            authorization=SimpleNamespace(authorize=lambda *_: grant("p")),
            current_user=SimpleNamespace(user_id="u"),
        )
    assert error.value.status_code == 500
    assert error.value.detail["code"] == "VERSION_STORAGE_INTEGRITY_ERROR"
    assert calls[-1] == "release"
    incident.assert_not_called()


@pytest.mark.parametrize(
    "raw", [b"../escape", b"/absolute", b"dir//file", b"dir/./file", b"a\x00b", b"a" * 501]
)
def test_native_byte_paths_reject_traversal_and_oversize(raw):
    from fastapi import HTTPException

    from src.version_engine.entrypoints.http.content_read import _read_request_path

    with pytest.raises(HTTPException) as error:
        _read_request_path("", base64.b64encode(raw).decode())
    assert error.value.status_code == 400


def test_native_byte_path_limit_preserves_existing_unicode_path_contract():
    from src.version_engine.entrypoints.http.content_read import _read_request_path

    path = "文" * 500
    assert _read_request_path(path, None) == path
    assert _read_request_path("", base64.b64encode(path.encode()).decode()) == path


def test_git_filename_headers_cannot_inject_headers():
    from src.version_engine.entrypoints.http.content_read import (
        _content_disposition_attachment,
        _content_disposition_inline,
    )

    for disposition in (_content_disposition_inline, _content_disposition_attachment):
        header = disposition('name\r\n"\\injected')
        assert "\r" not in header and "\n" not in header
        assert "name___" in header
