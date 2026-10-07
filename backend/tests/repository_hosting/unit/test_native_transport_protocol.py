import base64
from types import SimpleNamespace

import pytest

from src.version_engine.adapters.git.native_repository import (
    NativeGitRepository,
)
from src.version_engine.adapters.git.protocol import pkt_line, read_pkt_lines
from src.version_engine.write_engine.ref_transaction import RefTransactionService
from tests.repository_hosting.integration.test_ref_transaction_service import grant

pytestmark = pytest.mark.hosting_component


def test_report_status_sanitizes_rejection_and_does_not_invent_success():
    name = b"refs/heads/main"
    response = NativeGitRepository.report(
        [SimpleNamespace(name=name)], {"side-band-64k"}, {name: "rejected\nok refs/heads/main\0"}
    )
    payloads, _ = read_pkt_lines(response.body)
    report = b"".join(packet[1:] for packet in payloads)
    lines, _ = read_pkt_lines(report)
    assert lines[0] == b"unpack ok\n"
    assert len(lines) == 2 and lines[1].startswith(b"ng ")
    assert lines[1].count(b"\n") == 1 and b"\0" not in lines[1]


def test_report_status_preserves_byte_refs_and_only_explicit_commits_succeed():
    name = b"refs/heads/byte-\xff"
    edits = [SimpleNamespace(name=name), SimpleNamespace(name=b"refs/heads/uncommitted")]
    response = NativeGitRepository.report(edits, set(), {name: None})
    lines, _ = read_pkt_lines(response.body)
    assert lines == [
        b"unpack ok\n",
        b"ok " + name + b"\n",
        b"ng refs/heads/uncommitted not published\n",
    ]


@pytest.mark.parametrize("protocol", ["version=0", "version=1", "version=2"])
@pytest.mark.parametrize("detached", [False, True])
def test_advertisement_is_refs_only_and_uses_verified_peel_metadata(protocol, detached, tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("advertisement touched object storage")

    refs = [
        {
            "name_b64": base64.b64encode(b"HEAD").decode(),
            "state": {
                "kind": "symbolic",
                "target_b64": base64.b64encode(b"refs/heads/main").decode(),
            },
        },
        {
            "name_b64": base64.b64encode(b"refs/heads/main").decode(),
            "state": {"kind": "oid", "oid": "a" * 40},
            "kind": "commit",
        },
        {
            "name_b64": base64.b64encode(b"refs/tags/tag").decode(),
            "state": {"kind": "oid", "oid": "b" * 40},
            "kind": "tag",
            "peeled_oid": "a" * 40,
        },
    ]
    if detached:
        refs[0]["state"] = {"kind": "oid", "oid": "a" * 40}
        refs[0]["kind"] = "commit"
    control = SimpleNamespace(
        read_snapshot=lambda project, actor: {
            "authority": "native",
            "object_format": "sha1",
            "refs": refs,
        }
    )
    backend = SimpleNamespace(get_durable=forbidden)
    transport = NativeGitRepository(RefTransactionService(control, backend, project_id="test"))
    response = transport.info_refs(grant("test"), "git-upload-pack", protocol=protocol)
    assert response.status_code == 200
    if protocol == "version=2":
        assert b"version 2" in response.body and b"ls-refs" in response.body
        path = tmp_path / "ls-refs"
        path.write_bytes(
            pkt_line(b"command=ls-refs\n")
            + b"0001"
            + pkt_line(b"peel\n")
            + pkt_line(b"symrefs\n")
            + b"0000"
        )
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

    control = SimpleNamespace(
        read_snapshot=lambda project, actor: {
            "authority": "native",
            "object_format": declared_format,
            "refs": [],
        }
    )
    service = RefTransactionService(
        control, SimpleNamespace(get_durable=forbidden), project_id="test"
    )
    transport = NativeGitRepository(service)
    monkeypatch.setattr("subprocess.Popen", forbidden)
    with pytest.raises(ValueError, match="object format mismatch"):
        transport.info_refs(grant("test"), "git-upload-pack")


def test_native_transport_does_not_execute_environment_git_config(monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "/tmp/untrusted-hooks")
    from src.version_engine.adapters.git.native_wire import version

    def forbidden(*args, **kwargs):
        pytest.fail("native discovery executed a subprocess")

    monkeypatch.setattr("subprocess.Popen", forbidden)
    test_advertisement_is_refs_only_and_uses_verified_peel_metadata("version=0", False, None)
    assert version("version=2") == 2
    with pytest.raises(ValueError):
        version("version=2:version=1")
