"""Byte-level native Git oracle for hosted workflows, not a Cloud capability claim."""

from collections.abc import Callable
from dataclasses import dataclass

from tests.repository_hosting.harness.git import Git
from tests.repository_hosting.harness.http import AUTH


@dataclass(frozen=True)
class Workflow:
    name: str
    capabilities: tuple[str, ...]
    commands: tuple[str, ...]
    execute: Callable[[Git], None]
    gap: str = ""


@dataclass(frozen=True)
class RemoteSnapshot:
    refs: bytes
    head: bytes
    objects: dict[str, tuple[str, bytes]]


def reachable_objects(repo):
    # A replace ref is user data, not permission to rewrite the oracle's graph.
    ids = repo.text(
        "--no-replace-objects", "rev-list", "--objects", "--all", "--no-object-names",
    ).splitlines()
    result = {}
    for oid in sorted(set(ids)):
        kind = repo.text("--no-replace-objects", "cat-file", "-t", oid)
        result[oid] = (kind, repo.run("--no-replace-objects", "cat-file", kind, oid).stdout)
    return result


def remote_snapshot(source, remote, destination):
    """Fresh mirror: no alternates, shared object directory, or previous client cache."""
    source.run("-c", AUTH, "clone", "--mirror", remote, destination)
    mirror = Git(destination)
    mirror.run("--no-replace-objects", "fsck", "--full", "--strict", "--no-reflogs")
    refs = mirror.run("for-each-ref", "--format=%(refname) %(objectname)").stdout
    head = source.run("-c", AUTH, "ls-remote", "--symref", remote, "HEAD").stdout
    return RemoteSnapshot(refs, head, reachable_objects(mirror))
