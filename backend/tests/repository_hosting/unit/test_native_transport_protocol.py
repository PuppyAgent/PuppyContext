import base64
from types import SimpleNamespace

import pytest

from src.version_engine.adapters.git.native_repository import (
    NativeGitRepository,
    accepted_receive_refs,
)
from src.version_engine.adapters.git.protocol import pkt_line
from src.version_engine.write_engine.ref_transaction import RefTransactionService
from tests.repository_hosting.integration.test_ref_transaction_service import grant

pytestmark = pytest.mark.hosting_component


def test_report_status_does_not_accept_status_text_in_stderr_or_ng_reason():
    name = b"refs/heads/main"
    rejection = pkt_line(b"unpack ok\n") + pkt_line(b"ng " + name + b" injected: ok " + name + b"\n") + b"0000"
    stream = pkt_line(b"\x02ok " + name + b"\n") + pkt_line(b"\x01" + rejection) + b"0000"
    assert accepted_receive_refs(stream, {"side-band-64k"}) == set()
    assert accepted_receive_refs(rejection, set()) == set()


def test_report_status_accepts_only_explicit_ok_after_unpack_ok():
    name = b"refs/heads/byte-\xff"
    report = pkt_line(b"unpack ok\n") + pkt_line(b"ok " + name + b"\n") + b"0000"
    framed = pkt_line(b"\x01" + report[:10]) + pkt_line(b"\x02progress\n") + pkt_line(b"\x01" + report[10:]) + b"0000"
    assert accepted_receive_refs(framed, {"side-band-64k"}) == {name}
    corrupt = pkt_line(b"unpack error\n") + pkt_line(b"ok " + name + b"\n") + b"0000"
    assert accepted_receive_refs(corrupt, set()) == set()


@pytest.mark.parametrize("protocol", ["version=0", "version=1", "version=2"])
@pytest.mark.parametrize("detached", [False, True])
def test_advertisement_is_refs_only_and_uses_verified_peel_metadata(protocol, detached, tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("advertisement touched object storage")
    refs = [
        {"name_b64": base64.b64encode(b"HEAD").decode(), "state": {"kind": "symbolic", "target_b64": base64.b64encode(b"refs/heads/main").decode()}},
        {"name_b64": base64.b64encode(b"refs/heads/main").decode(), "state": {"kind": "oid", "oid": "a" * 40}, "kind": "commit"},
        {"name_b64": base64.b64encode(b"refs/tags/tag").decode(), "state": {"kind": "oid", "oid": "b" * 40}, "kind": "tag", "peeled_oid": "a" * 40},
    ]
    if detached:
        refs[0]["state"] = {"kind": "oid", "oid": "a" * 40}
        refs[0]["kind"] = "commit"
    control = SimpleNamespace(snapshot=lambda _: {"authority": "native", "object_format": "sha1", "refs": refs})
    backend = SimpleNamespace(get_durable=forbidden)
    transport = NativeGitRepository(RefTransactionService(control, backend, project_id="test"))
    response = transport.info_refs(grant("test"), "git-upload-pack", protocol=protocol)
    assert response.status_code == 200
    if protocol == "version=2":
        assert b"version 2" in response.body and b"ls-refs" in response.body
        path = tmp_path / "ls-refs"
        path.write_bytes(pkt_line(b"command=ls-refs\n") + b"0001" + pkt_line(b"peel\n") + pkt_line(b"symrefs\n") + b"0000")
        listed = transport.upload(grant("test"), path, protocol=protocol)
        assert b"refs/tags/tag peeled:" + b"a" * 40 in listed.body
        assert b"a" * 40 + b" HEAD" in listed.body
    else:
        assert b"refs/tags/tag^{}" in response.body
        assert b"a" * 40 in response.body


@pytest.mark.parametrize("declared_format", [None, "sha256"])
def test_empty_advertisement_cannot_invent_a_repository_format(declared_format, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("format mismatch reached Git or object I/O")
    control = SimpleNamespace(snapshot=lambda _: {"authority": "native", "object_format": declared_format, "refs": []})
    service = RefTransactionService(control, SimpleNamespace(get_durable=forbidden), project_id="test")
    transport = NativeGitRepository(service)
    monkeypatch.setattr(transport, "git", forbidden)
    with pytest.raises(ValueError, match="object format mismatch"):
        transport.info_refs(grant("test"), "git-upload-pack")


def test_native_git_environment_cannot_inherit_replace_config_or_protocol_injection(monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "/tmp/untrusted-hooks")
    service = SimpleNamespace(control=None, project_id="test", object_format="sha1")
    transport = NativeGitRepository(service)
    env = transport.environment("version=2")
    assert env["GIT_PROTOCOL"] == "version=2"
    assert env["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert not any(key.startswith("GIT_CONFIG_KEY_") for key in env)
    with pytest.raises(ValueError):
        transport.environment("version=2:version=1")
