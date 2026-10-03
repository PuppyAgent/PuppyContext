"""Negotiation, shallow clients and computations on a fresh fetched repository."""

import io
import tarfile
from functools import partial

from tests.repository_hosting.harness.conformance import Workflow, reachable_objects
from tests.repository_hosting.harness.git import Git
from tests.repository_hosting.harness.http import AUTH, clone_client


def protocol_version(git, *, version):
    destination = git.path.parent / (git.path.name + "-protocol")
    remote = git.text("remote", "get-url", "origin")
    result = git.run(
        "-c", f"protocol.version={version}", "-c", AUTH,
        "clone", remote, destination, trace_packets=True,
    )
    # Configuring protocol.version alone is not evidence of actual negotiation.
    if version:
        assert f"version {version}".encode() in result.stderr, result.stderr.decode(errors="replace")
    else:
        assert b"version 1" not in result.stderr and b"version 2" not in result.stderr
    client = Git(destination)
    client.run("config", "http.extraHeader", AUTH.split("=", 1)[1])
    client.commit({"protocol.txt": b"negotiated\n"}, "protocol client write")
    client.run("push", "origin", "main")


def shallow_workflow(git, *, mode):
    for number in range(3):
        git.commit({"history.txt": f"{number}\n".encode()}, f"history {number}")
    git.run("push", "origin", "main")
    remote = git.text("remote", "get-url", "origin")
    shallow = clone_client(remote, git.path.parent / (git.path.name + "-shallow"), git, "--depth=1")
    assert shallow.text("rev-parse", "--is-shallow-repository") == "true"
    assert shallow.text("rev-list", "--count", "HEAD") == "1"
    if mode == "deepen":
        shallow.run("fetch", "--deepen=1", "origin")
        assert shallow.text("rev-list", "--count", "HEAD") == "2"
        shallow.run("fetch", "--unshallow", "origin")
        assert shallow.text("rev-parse", "--is-shallow-repository") == "false"
        assert shallow.text("rev-list", "--count", "HEAD") == "4"
    shallow.commit({"shallow-write.txt": b"ancestors already on server\n"}, "shallow client write")
    shallow.run("push", "origin", "main")


def partial_clone(git):
    git.commit({"lazy.txt": b"fetch only when requested\n"}, "promisor seed")
    git.run("push", "origin", "main")
    remote = git.text("remote", "get-url", "origin")
    destination = git.path.parent / (git.path.name + "-partial")
    result = git.run("-c", AUTH, "clone", "--filter=blob:none", "--no-checkout", remote, destination)
    assert b"filtering not recognized" not in result.stderr, result.stderr.decode(errors="replace")
    client = Git(destination)
    client.run("config", "http.extraHeader", AUTH.split("=", 1)[1])
    oid = git.text("rev-parse", "HEAD:lazy.txt")
    missing = client.text("rev-list", "--objects", "--all", "--missing=print").splitlines()
    assert "?" + oid in missing, "filter must actually omit the blob, not silently clone everything"
    assert client.run("show", "HEAD:lazy.txt").stdout == b"fetch only when requested\n"
    assert "?" + oid not in client.text("rev-list", "--objects", "--all", "--missing=print").splitlines()


def query_history(git):
    base = git.text("rev-parse", "HEAD")
    git.commit({"read.txt": b"needle\nline two\n"}, "query seed")
    git.run("tag", "query-v1")
    git.run("mv", "read.txt", "renamed.txt")
    git.run("commit", "-m", "rename for diff")
    git.run("push", "origin", "main")
    git.run("push", "origin", "query-v1")
    client = clone_client(
        git.text("remote", "get-url", "origin"),
        git.path.parent / (git.path.name + "-queries"), git,
    )
    commands = [
        ("log", "--all", "--topo-order", "--format=raw"),
        ("show", "--format=fuller", "HEAD"),
        ("diff", "--binary", "--find-renames", "HEAD^", "HEAD"),
        ("blame", "--porcelain", "HEAD", "--", "renamed.txt"),
        ("merge-base", base, "HEAD"),
        ("rev-list", "--left-right", "--count", base + "...HEAD"),
        ("describe", "--tags", "--always"),
        ("range-diff", base + "..HEAD", base + "..HEAD"),
        ("grep", "-n", "--fixed-strings", "needle", "HEAD"),
    ]
    for command in commands:
        assert client.run(*command).stdout == git.run(*command).stdout, command
    assert client.text("rev-list", "--left-right", "--count", base + "...HEAD").split() == ["0", "2"]


def bisect_history(git):
    good = git.text("rev-parse", "HEAD")
    git.commit({"good.txt": b"still good\n"}, "good")
    bad = git.commit({"regression.txt": b"introduced\n"}, "first bad")
    git.commit({"later.txt": b"later\n"}, "after regression")
    git.run("push", "origin", "main")
    client = clone_client(
        git.text("remote", "get-url", "origin"),
        git.path.parent / (git.path.name + "-bisect"), git,
    )
    tip = client.text("rev-parse", "HEAD")
    client.run("bisect", "start", tip, good)
    client.run("bisect", "run", "sh", "-c", "test ! -f regression.txt")
    assert client.text("rev-parse", "refs/bisect/bad") == bad
    client.run("bisect", "reset")
    assert client.text("rev-parse", "HEAD") == tip


def client_maintenance(git):
    git.commit({"retained.txt": b"retained\n"}, "maintenance seed")
    git.run("push", "origin", "main")
    mirror = clone_client(
        git.text("remote", "get-url", "origin"),
        git.path.parent / (git.path.name + "-maintenance"), git, "--mirror",
    )
    before = mirror.refs(), reachable_objects(mirror)
    mirror.run("repack", "-ad")
    mirror.run("pack-refs", "--all")
    mirror.run("commit-graph", "write", "--reachable")
    mirror.run("commit-graph", "verify")
    mirror.run("multi-pack-index", "write")
    mirror.run("multi-pack-index", "verify")
    indexes = list((mirror.path / "objects" / "pack").glob("*.idx"))
    assert indexes
    for index in indexes:
        mirror.run("verify-pack", "-v", index)
    mirror.run("reflog", "expire", "--expire=now", "--all")
    mirror.run("gc", "--prune=now")
    mirror.run("fsck", "--full", "--strict")
    assert (mirror.refs(), reachable_objects(mirror)) == before


def client_archive(git):
    git.commit({"archive.txt": b"archive contents\n"}, "archive seed")
    git.run("push", "origin", "main")
    client = clone_client(
        git.text("remote", "get-url", "origin"),
        git.path.parent / (git.path.name + "-archive"), git,
    )
    archive = client.run("archive", "--format=tar", "HEAD").stdout
    assert archive == git.run("archive", "--format=tar", "HEAD").stdout
    # Inspect only; never extract an untrusted archive onto the host filesystem.
    with tarfile.open(fileobj=io.BytesIO(archive)) as opened:
        assert opened.extractfile("archive.txt").read() == b"archive contents\n"


NETWORK_WORKFLOWS = [
    *[Workflow(f"protocol-v{version}", ("G44", "G46"), ("clone", "push"), partial(protocol_version, version=version), "HTTP must negotiate the requested protocol version, not silently fall back" if version else "") for version in (0, 1, 2)],
    *[Workflow("shallow-" + mode, ("G47", "G48") if mode == "deepen" else ("G48",), ("clone", "fetch", "push") if mode == "deepen" else ("clone", "push"), partial(shallow_workflow, mode=mode)) for mode in ("deepen", "push")],
    Workflow("partial-clone-lazy-fetch", ("G49",), ("clone", "rev-list", "show"), partial_clone, "filter/promisor negotiation and authorized lazy fetch are not implemented"),
    Workflow("fresh-clone-history-queries", ("G39", "G40", "G41", "G42"), ("log", "show", "diff", "blame", "merge-base", "rev-list", "describe", "range-diff", "grep"), query_history),
    Workflow("fresh-clone-bisect", ("G42",), ("bisect",), bisect_history),
    Workflow("client-maintenance", ("G54", "G56"), ("repack", "pack-refs", "commit-graph", "multi-pack-index", "verify-pack", "reflog", "gc", "fsck"), client_maintenance),
    Workflow("client-archive", ("G53",), ("archive",), client_archive),
]
