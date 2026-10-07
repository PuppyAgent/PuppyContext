"""Stock Git pack interoperability and proof that fetch cannot create a repo."""

import asyncio
import base64
import hashlib
import io
import struct
import threading
import uuid
import zlib
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request

from src.version_engine.adapters.git.native_repository import NativeGitRepository
from src.version_engine.adapters.git.object_pack import IncomingPack, bounded_delta
from src.version_engine.adapters.git.object_reader import PublishedObjectReader
from src.version_engine.adapters.git.protocol import pkt_line, read_pkt_lines
from src.version_engine.read.repository_snapshot import repository_snapshot
from src.version_engine.write_engine.git_object_format import decode_object, encode_object
from tests.repository_hosting.harness.git import Git
from tests.repository_hosting.harness.http_server import _serve_git_app
from tests.repository_hosting.harness.network_workflows import (
    partial_clone,
    protocol_version,
    shallow_workflow,
)
from tests.repository_hosting.integration.test_ref_transaction_service import grant

pytestmark = pytest.mark.hosting_component


def storage(git):
    objects = git.objects()
    reads, pins = [], set()
    rows = [
        {
            "name_b64": base64.b64encode(b"HEAD").decode(),
            "state": {
                "kind": "symbolic",
                "target_b64": base64.b64encode(b"refs/heads/main").decode(),
            },
        }
    ]
    for name, oid in git.refs().items():
        rows.append(
            {
                "name_b64": base64.b64encode(name.encode()).decode(),
                "kind": objects[oid][0],
                "state": {"kind": "oid", "oid": oid},
            }
        )
    format = git.text("rev-parse", "--show-object-format")
    wire = {
        "project_id": "p",
        "authority": "native",
        "object_format": format,
        "generation": 1,
        "ref_sequence": 1,
        "refs": rows,
    }

    def begin(_project, _actor, pin):
        pins.add(pin)
        return deepcopy(wire) | {"pin_id": pin}

    def get(oid):
        reads.append(oid)
        return encode_object(*objects[oid], object_format=format)[1]

    control = SimpleNamespace(
        begin_read=begin,
        release=lambda _p, _a, pin: pins.remove(pin),
        renew=lambda *_: None,
        read_snapshot=lambda *_: deepcopy(wire),
        call=lambda *_, **kw: {},
    )
    backend = SimpleNamespace(publication_project_id="p", get_durable=get)
    return (
        SimpleNamespace(control=control, backend=backend, object_format=format, project_id="p"),
        reads,
        pins,
        objects,
    )


async def consume(response):
    try:
        return b"".join([part async for part in response.body_iterator])
    finally:
        response.close()


def fetch_body(git, wants, *, haves=(), extra=()):
    result = pkt_line(b"command=fetch\n") + b"0001"
    for oid in wants:
        result += pkt_line(b"want " + oid.encode() + b"\n")
    for oid in haves:
        result += pkt_line(b"have " + oid.encode() + b"\n")
    return (
        result + b"".join(pkt_line(line + b"\n") for line in extra) + pkt_line(b"done\n") + b"0000"
    )


@pytest.mark.parametrize("format", ["sha1", "sha256"])
def test_concurrent_cold_fetch_needs_no_files_or_git_and_retains_pins(
    tmp_path, monkeypatch, format
):
    source = Git.init(tmp_path / "source", format=format)
    head = source.commit({"large": b"large immutable blob\n" * 10000, "nested/file": b"hello"})
    service, _reads, pins, objects = storage(source)
    path = tmp_path / "request"
    path.write_bytes(fetch_body(source, [head]))
    transport = NativeGitRepository(service)

    def forbidden(*_, **kw):
        pytest.fail("fetch requires a local file or server Git process")

    async def parallel():
        responses = [transport.upload(grant("p"), path, protocol="version=2") for _ in range(8)]
        assert len(pins) == 8
        results = await asyncio.gather(*(consume(response) for response in responses))
        assert not pins
        return results

    with monkeypatch.context() as patch:
        patch.setattr(PublishedObjectReader, "CACHE_BYTES", 0)
        patch.setattr("tempfile.TemporaryDirectory", forbidden)
        patch.setattr("tempfile.TemporaryFile", forbidden)
        patch.setattr("subprocess.Popen", forbidden)
        results = asyncio.run(parallel())
    assert len(set(results)) == 1
    payloads, _ = read_pkt_lines(results[0])
    assert payloads[0] == b"packfile\n"
    pack = b"".join(payload[1:] for payload in payloads[1:])
    clone = Git.init(tmp_path / "clone", bare=True, format=format)
    clone.run("index-pack", "--stdin", "--strict", input=pack)
    clone.run("update-ref", "refs/heads/main", head)
    clone.run("fsck", "--full", "--strict")
    assert clone.objects() == objects


@pytest.mark.parametrize("format", ["sha1", "sha256"])
def test_incremental_fetch_and_blob_none_do_not_read_existing_blobs(tmp_path, format):
    source = Git.init(tmp_path / "source", format=format)
    old = source.commit({"old": b"already at client" * 100000})
    old_blob = source.text("rev-parse", "HEAD:old")
    head = source.commit({"new": b"new content"})
    new_blob = source.text("rev-parse", "HEAD:new")
    service, reads, pins, _ = storage(source)
    transport = NativeGitRepository(service)
    path = tmp_path / "request"
    path.write_bytes(fetch_body(source, [head], haves=[old]))
    asyncio.run(consume(transport.upload(grant("p"), path, protocol="version=2")))
    assert old_blob not in reads and new_blob in reads and not pins
    reads.clear()
    path.write_bytes(fetch_body(source, [head], extra=[b"filter blob:none"]))
    asyncio.run(consume(transport.upload(grant("p"), path, protocol="version=2")))
    assert old_blob not in reads and new_blob not in reads and not pins
    reads.clear()
    path.write_bytes(fetch_body(source, [old_blob], haves=[head], extra=[b"filter blob:none"]))
    asyncio.run(consume(transport.upload(grant("p"), path, protocol="version=2")))
    assert old_blob in reads and not pins  # explicit lazy want overrides common/filter omission


@pytest.mark.parametrize("format", ["sha1", "sha256"])
@pytest.mark.parametrize("thin", [False, True])
def test_incoming_stock_delta_pack_preserves_bytes_without_copying_old_repo(
    tmp_path, monkeypatch, format, thin
):
    source = Git.init(tmp_path / "source", format=format)
    old = source.commit({"file": b"0123456789abcdef\n" * 10000})
    service, _reads, pins, _ = storage(source)
    source.commit({"file": b"0123456789abcdef\n" * 9999 + b"changed\n"})
    args = ["pack-objects", "--stdout", "--revs", "--delta-base-offset"]
    if thin:
        args.append("--thin")
    pack = source.run(*args, input=("HEAD\n^" + old + "\n").encode()).stdout
    expected = source.objects()
    new_head = source.text("rev-parse", "HEAD")

    def forbidden(*args, **kwargs):
        pytest.fail("incoming pack created a bare repo or executed Git")

    monkeypatch.setattr("tempfile.TemporaryDirectory", forbidden)
    monkeypatch.setattr("subprocess.Popen", forbidden)
    with (
        repository_snapshot(
            service.control, service.backend, grant("p"), project_id="p"
        ) as snapshot,
        IncomingPack(PublishedObjectReader(snapshot, service.control)) as incoming,
    ):
        incoming.read(io.BytesIO(pack))
        for oid in incoming.objects:
            assert incoming.get(oid) == expected[oid]
        assert new_head in incoming.objects
        assert old not in incoming.objects
    assert not pins


@pytest.mark.parametrize("format", ["sha1", "sha256"])
def test_forward_ref_delta_chain_resolves_against_a_published_thin_base(tmp_path, format):
    from dulwich.pack import create_delta

    source = Git.init(tmp_path / "source", format=format)
    base = b"a thin base already published\n" * 100
    source.commit({"file": base})
    service, _, pins, _ = storage(source)
    middle, final = base + b"middle", base + b"middle-final"
    base_oid, _ = encode_object("blob", base, object_format=format)
    middle_oid, _ = encode_object("blob", middle, object_format=format)
    final_oid, _ = encode_object("blob", final, object_format=format)
    pack = struct.pack("!4sII", b"PACK", 2, 2)
    for base_id, original, result in ((middle_oid, middle, final), (base_oid, base, middle)):
        delta = b"".join(create_delta(original, result))
        size = len(delta)
        byte, size = 0x70 | (size & 15), size >> 4
        entry = bytearray()
        while size:
            entry.append(byte | 128)
            byte, size = size & 127, size >> 7
        entry.append(byte)
        pack += bytes(entry) + bytes.fromhex(base_id) + zlib.compress(delta)
    pack += hashlib.new(format, pack).digest()
    with (
        repository_snapshot(
            service.control, service.backend, grant("p"), project_id="p"
        ) as snapshot,
        IncomingPack(PublishedObjectReader(snapshot, service.control)) as incoming,
    ):
        incoming.read(io.BytesIO(pack))
        assert incoming.get(final_oid) == ("blob", final)
        assert incoming.get(middle_oid) == ("blob", middle)
        assert base_oid not in incoming.objects
    assert not pins


def test_pack_checksum_and_expansion_are_checked_before_publication(tmp_path, monkeypatch):
    source = Git.init(tmp_path / "source")
    source.commit({"file": b"hello"})
    service, _, _, _ = storage(source)
    pack = source.run("pack-objects", "--stdout", "--all").stdout
    with repository_snapshot(
        service.control, service.backend, grant("p"), project_id="p"
    ) as snapshot:
        reader = PublishedObjectReader(snapshot, service.control)
        with IncomingPack(reader) as incoming, pytest.raises(ValueError, match="checksum"):
            incoming.read(io.BytesIO(pack[:-1] + bytes([pack[-1] ^ 1])))
        header = struct.pack("!4sII", b"PACK", 2, 1)
        malicious = header + b"\x31" + zlib.compress(b"x" * 100000)
        malicious += hashlib.sha1(malicious).digest()
        with IncomingPack(reader) as incoming, pytest.raises(ValueError, match="expansion"):
            incoming.read(io.BytesIO(malicious))
    # Declares one output byte, then repeatedly copies the full base.
    with pytest.raises(ValueError, match="expansion"):
        bounded_delta(b"x" * 65536, b"\x80\x80\x04\x01\x80\x80")


@pytest.mark.parametrize(
    "name", [b".GiT", b".git.", b"git~1", b".git:stream", ".g\u200cit".encode()]
)
def test_incoming_tree_cannot_smuggle_git_metadata_paths(tmp_path, name):
    source = Git.init(tmp_path / "source")
    source.commit({"file": b"hello"})
    service, _, _, _ = storage(source)
    with (
        repository_snapshot(
            service.control, service.backend, grant("p"), project_id="p"
        ) as snapshot,
        IncomingPack(PublishedObjectReader(snapshot, service.control)) as incoming,
        pytest.raises(ValueError, match="protected Git path"),
    ):
        incoming.add("tree", b"100644 " + name + b"\0" + b"\xaa" * 20)


def test_uniterated_response_releases_pin_and_unpublished_oid_is_not_read(tmp_path):
    source = Git.init(tmp_path / "source")
    head = source.commit({"file": b"hello"})
    service, reads, pins, objects = storage(source)
    secret, _ = encode_object("blob", b"unpublished bytes")
    objects[secret] = ("blob", b"unpublished bytes")
    transport = NativeGitRepository(service)
    path = tmp_path / "request"
    path.write_bytes(fetch_body(source, [secret]))
    with pytest.raises(PermissionError):
        transport.upload(grant("p"), path, protocol="version=2")
    assert secret not in reads and not pins
    path.write_bytes(fetch_body(source, [head]))
    response = transport.upload(grant("p"), path, protocol="version=2")
    assert pins
    response.close()
    response.close()
    assert not pins


def test_cancelled_stream_joins_storage_read_before_releasing_pin(tmp_path):
    source = Git.init(tmp_path / "source")
    head = source.commit({"file": b"hello"})
    service, _, pins, _ = storage(source)
    entered, finish = threading.Event(), threading.Event()
    get = service.backend.get_durable

    def blocked(oid):
        entered.set()
        assert finish.wait(5)
        assert pins, "snapshot was released while storage was still running"
        return get(oid)

    service.backend.get_durable = blocked
    path = tmp_path / "request"
    path.write_bytes(fetch_body(source, [head]))
    response = NativeGitRepository(service).upload(grant("p"), path, protocol="version=2")

    async def run():
        async def send(_):
            pass

        async def receive():
            await asyncio.Future()

        task = asyncio.create_task(
            response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        )
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        await asyncio.sleep(0.01)
        assert pins and not task.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not pins

    asyncio.run(run())


@pytest.mark.parametrize("format", ["sha1", "sha256"])
@pytest.mark.parametrize("protocol", [0, 1, 2])
def test_stock_http_shallow_partial_and_incremental_roundtrips(
    tmp_path, monkeypatch, format, protocol
):
    """Fast protocol oracle; the substituted authority is NOT PG acceptance."""
    stock_run = Git.run
    monkeypatch.setattr(
        Git,
        "run",
        lambda self, *args, **kw: stock_run(
            self, "-c", f"protocol.version={protocol}", *args, **(kw | {"trace_packets": True})
        ),
    )
    source = Git.init(tmp_path / "source", format=format)
    source.commit({"file": b"initial"})
    service, _, pins, objects = storage(source)
    wire = service.control.read_snapshot()
    histories = {}
    lock = threading.Lock()
    service.control.read_snapshot = lambda *_: deepcopy(wire)

    def begin(_p, _a, pin):
        pins.add(pin)
        return deepcopy(wire) | {"pin_id": pin}

    service.control.begin_read = begin
    service.control.call = lambda *_, **kw: histories.copy()
    service.backend.put_durable = lambda oid, loose: objects.__setitem__(oid, decode_object(loose))

    def submit(_grant, *, edits, roots, prepare, **kw):
        with lock:
            prepare()
            for edit in edits:
                name = base64.b64encode(edit.name).decode()
                rows = {row["name_b64"]: row for row in wire["refs"]}
                previous = rows.get(name, {}).get("state", {}).get("oid")
                if previous != edit.expected.oid:
                    return {"status": "rejected", "reason": "stale_ref"}
            for edit in edits:
                name = base64.b64encode(edit.name).decode()
                wire["refs"] = [row for row in wire["refs"] if row["name_b64"] != name]
                if edit.new.oid:
                    wire["refs"].append(
                        {
                            "name_b64": name,
                            "state": edit.new.wire(format),
                            "kind": roots[edit.new.oid],
                        }
                    )
            histories.update(roots)
            return {"status": "committed"}

    service.submit = submit
    transport = NativeGitRepository(service)
    app = FastAPI()

    @app.get("/repo.git/info/refs")
    async def info(service: str, request: Request):
        return await asyncio.to_thread(
            transport.info_refs,
            grant("p"),
            service,
            protocol=request.headers.get("git-protocol", ""),
        )

    @app.post("/repo.git/{command}")
    async def rpc(command: str, request: Request):
        path = tmp_path / uuid.uuid4().hex
        path.write_bytes(await request.body())
        try:
            if command == "git-receive-pack":
                return await asyncio.to_thread(transport.receive, grant("p"), path)
            return await asyncio.to_thread(
                transport.upload, grant("p"), path, protocol=request.headers.get("git-protocol", "")
            )
        finally:
            path.unlink()

    with _serve_git_app(app) as address:
        remote = address + "/repo.git"
        source.run("-c", f"protocol.version={protocol}", "clone", remote, tmp_path / "client")
        client = Git(tmp_path / "client")
        client.run("config", "protocol.version", str(protocol))
        shallow_workflow(client, mode="deepen")
        client.run("pull", "--ff-only")
        partial_clone(client)
        protocol_version(client, version=protocol)
        client.run("pull", "--ff-only")
        client.run("tag", "boundary", "HEAD~2")
        client.run("push", "origin", "refs/tags/boundary")
        client.run("clone", "--shallow-exclude=boundary", "--no-tags", remote, tmp_path / "exclude")
        excluded = Git(tmp_path / "exclude")
        assert excluded.text("rev-list", "--count", "HEAD") == "2"
        excluded.run("fetch", "--unshallow", "origin")
        assert excluded.text("rev-parse", "--is-shallow-repository") == "false"
        client.run("clone", "--shallow-since=2025-01-01", remote, tmp_path / "since")
        assert Git(tmp_path / "since").text("rev-parse", "--is-shallow-repository") == "false"
        assert client.run(
            "clone", "--shallow-since=2027-01-01", remote, tmp_path / "future", check=False
        ).returncode
    assert not pins
