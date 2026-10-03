"""Snapshot decoding/access failures before consumer integration."""
from __future__ import annotations

import base64
from copy import deepcopy
from types import SimpleNamespace

import pytest

from src.version_engine.read.repository_snapshot import repository_snapshot
from src.version_engine.write_engine.git_object_format import encode_object
from tests.repository_hosting.integration.test_ref_transaction_service import grant

pytestmark = pytest.mark.hosting_component


def row(name, state, kind=None):
    return {"name_b64": base64.b64encode(name).decode(), "state": state, "kind": kind}


def fixture():
    wire = {"project_id": "p", "authority": "native", "object_format": "sha1", "generation": 1, "ref_sequence": 0,
            "refs": [row(b"HEAD", {"kind": "symbolic", "target_b64": base64.b64encode(b"refs/heads/main").decode()})]}
    calls, objects = [], {}
    def begin(project, actor, pin):
        calls.append("begin")
        return deepcopy(wire) | {"pin_id": pin}
    def get(oid):
        calls.append("get")
        return objects[oid]
    control = SimpleNamespace(begin_read=begin, release=lambda *_: calls.append("release"),
                              renew=lambda *_: calls.append("renew"))
    backend = SimpleNamespace(publication_project_id="p", get_durable=get)
    return wire, calls, objects, control, backend


@pytest.mark.parametrize("defect", ["project", "authority", "format", "missing_head", "duplicate", "wrong_oid", "symref"])
def test_invalid_snapshot_releases_pin_without_reading_objects(defect):
    wire, calls, _objects, control, backend = fixture()
    if defect == "project":
        wire["project_id"] = "other"
    elif defect == "authority":
        wire["authority"] = "shadow"
    elif defect == "format":
        wire["object_format"] = "md5"
    elif defect == "missing_head":
        wire["refs"] = []
    elif defect == "duplicate":
        wire["refs"] *= 2
    elif defect == "wrong_oid":
        wire["refs"].append(row(b"refs/heads/main", {"kind": "oid", "oid": "f" * 64}, "commit"))
    else:
        wire["refs"].append(row(b"refs/heads/symbolic", wire["refs"][0]["state"]))
    with pytest.raises((RuntimeError, ValueError)), repository_snapshot(control, backend, grant("p"), project_id="p"):
        pytest.fail("invalid snapshot admitted")
    assert calls == ["begin", "release"]


def test_foreign_backend_is_rejected_before_pin_admission():
    _wire, calls, _objects, control, backend = fixture()
    backend.publication_project_id = "other"
    with pytest.raises(ValueError, match="binding mismatch"), repository_snapshot(control, backend, grant("p"), project_id="p"):
        pytest.fail("foreign storage admitted")
    assert not calls


def test_failed_renewal_does_not_read_storage():
    _wire, calls, _objects, control, backend = fixture()
    def expired(*_args):
        raise RuntimeError("expired pin")
    control.renew = expired
    with repository_snapshot(control, backend, grant("p"), project_id="p") as snapshot:
        snapshot._next_renewal = 0
        with pytest.raises(RuntimeError, match="expired pin"):
            snapshot.object("a" * 40)
    assert calls == ["begin", "release"]


def test_snapshot_verifies_raw_identity_and_ref_type():
    wire, calls, objects, control, backend = fixture()
    oid, raw = encode_object("blob", b"actual bytes")
    wire["refs"].append(row(b"refs/tags/blob", {"kind": "oid", "oid": oid}, "blob"))
    objects[oid] = encode_object("blob", b"different bytes")[1]
    with repository_snapshot(control, backend, grant("p"), project_id="p") as snapshot:
        with pytest.raises(ValueError, match="hash mismatch"):
            snapshot.object(oid)
        objects[oid] = raw
        assert snapshot.object(oid) == ("blob", b"actual bytes")
        with pytest.raises(ValueError, match="has no tree"):
            snapshot.revision(b"refs/tags/blob")
    assert calls[-1] == "release"


def test_snapshot_decompression_budget_is_not_unbounded():
    wire, _calls, objects, control, backend = fixture()
    oid, objects_raw = encode_object("blob", b"x" * 1000)
    objects[oid] = objects_raw
    wire["refs"].append(row(b"refs/tags/blob", {"kind": "oid", "oid": oid}, "blob"))
    with repository_snapshot(control, backend, grant("p"), project_id="p", max_bytes=10) as snapshot:
        with pytest.raises(ValueError, match="byte budget exceeded"):
            snapshot.object(oid)
        assert snapshot._remaining_bytes == 10
