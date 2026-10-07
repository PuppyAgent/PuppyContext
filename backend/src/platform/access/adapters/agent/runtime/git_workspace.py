"""Native Git transport for an isolated Agent working copy.

Bundles carry original objects through the existing private provider channel.
Only the supervisor publishes; the sandbox never receives a cloud write key.
"""

import base64
import io

from src.version_engine.adapters.git.native_fetch import FetchRequest, select_objects
from src.version_engine.adapters.git.object_pack import IncomingPack, pack_chunks
from src.version_engine.adapters.git.object_reader import PublishedObjectReader
from src.version_engine.read.repository_snapshot import repository_snapshot
from src.version_engine.write_engine.git_object_format import encode_object
from src.version_engine.write_engine.git_object_graph import object_edges
from src.version_engine.write_engine.native_operation_writer import NativeWriteBase
from src.version_engine.write_engine.ref_transaction import RefState, validate_ref_name

MAX_GIT_BYTES = 32 * 1024 * 1024


def full_project(run):
    policy = run["policy"]
    if (
        run.get("scope_id")
        or policy["path_prefix"]
        or policy["excludes"]
        or not policy["materialize"]
    ):
        raise ValueError("Native Agent Git requires an unrestricted Project-root view")


def capture_git(reader):
    snapshot, revision = reader.snapshot, reader.revision
    if not revision.ref_name.startswith(b"refs/heads/"):
        raise ValueError("Agent Git requires a cloud branch; detached HEAD is unsupported")
    # Git itself retains byte names. The worker's selected branch is also used
    # in process arguments and therefore must be representable without loss.
    revision.ref_name.decode("utf-8", errors="strict")
    transport = PublishedObjectReader(snapshot, snapshot.control)
    rows = [(name, state.oid) for name, state in snapshot.refs.items() if state.oid]
    bundle = None
    if rows:
        header = b"# v3 git bundle\n@object-format=" + snapshot.object_format.encode() + b"\n"
        for name, oid in rows:
            header += oid.encode() + b" " + name + b"\n"
        request = FetchRequest(
            [*(b"want " + oid.encode() for _, oid in rows), b"done"], snapshot.object_format, 0
        )
        objects, _, _ = select_objects(transport, request)
        data = bytearray(header + b"\n")
        for chunk in pack_chunks(transport, objects):
            data.extend(chunk)
            if len(data) > MAX_GIT_BYTES:
                raise ValueError("Agent repository history exceeds workspace bundle limit")
        bundle = base64.b64encode(data).decode()
    return {
        "bundle": bundle,
        "object_format": snapshot.object_format,
        "head": base64.b64encode(revision.ref_name).decode(),
        "tip": revision.commit_oid,
        "index": None,
    }


def bundle_pack(value, object_format):
    if not isinstance(value, str) or len(value) > (MAX_GIT_BYTES + 2) // 3 * 4:
        raise ValueError("Agent Git bundle exceeds limit")
    data = base64.b64decode(value, validate=True)
    header, separator, pack = data.partition(b"\n\n")
    lines = header.split(b"\n")
    if not separator or lines[0] not in {b"# v2 git bundle", b"# v3 git bundle"}:
        raise ValueError("Invalid Agent Git bundle")
    fmt, refs = "sha1", {}
    for line in lines[1:]:
        if line.startswith(b"@object-format="):
            fmt = line[15:].decode("ascii")
        elif line.startswith((b"@", b"-")):
            raise ValueError("Agent recovery requires a complete unfiltered Git bundle")
        else:
            oid, space, name = line.partition(b" ")
            if not space or name in refs:
                raise ValueError("Invalid bundle ref")
            validate_ref_name(name)
            value = oid.decode("ascii")
            RefState(oid=value).wire(object_format)
            refs[name] = value
    if fmt != object_format:
        raise ValueError("Agent bundle object format mismatch")
    return refs, pack


def publish_git(service, run, checkpoint, grant):
    """Called under the existing fenced Agent ProjectWriteLease."""
    full_project(run)
    base = NativeWriteBase.parse(checkpoint["base"])
    state = checkpoint["git"]
    tip = state["tip"]
    if state["object_format"] != base.object_format or service.object_format != base.object_format:
        raise ValueError("Agent repository object format changed")
    if base64.b64decode(state["head"], validate=True) != base.ref_name:
        raise ValueError("Agent changed its publication branch")
    if tip == base.expected.oid:
        return {"status": "no_changes"}
    if run["policy"]["readonly"] or tip is None:
        raise ValueError("Agent cannot publish this Git update")
    refs, data = bundle_pack(state["bundle"], base.object_format)
    if refs.get(base.ref_name) != tip:
        raise ValueError("Agent tip does not match its bundle branch")
    with repository_snapshot(
        service.control, service.backend, grant, project_id=run["project_id"]
    ) as snapshot:
        reader = PublishedObjectReader(snapshot, service.control)
        with IncomingPack(reader) as incoming:
            incoming.read(io.BytesIO(data))
            # A complete bundle is recovery authority for this run even when
            # its old base is no longer reachable from current remote refs.
            stack, seen = [(tip, "commit")], set()
            ancestry, commits = [tip], set()
            while ancestry:
                oid = ancestry.pop()
                if oid in commits:
                    continue
                commits.add(oid)
                kind, body = incoming.get(oid)
                if kind != "commit":
                    raise ValueError("Agent branch target is not a commit")
                ancestry.extend(
                    child
                    for child, expected in object_edges(
                        kind, body, object_format=base.object_format
                    )
                    if expected == "commit"
                )
            if base.expected.oid and base.expected.oid not in commits:
                raise ValueError("Agent publication must descend from its captured base")
            while stack:
                oid, expected = stack.pop()
                if oid in seen:
                    continue
                seen.add(oid)
                kind, body = incoming.get(oid)
                if kind != expected:
                    raise ValueError("Agent object graph type mismatch")
                stack.extend(object_edges(kind, body, object_format=base.object_format))

            def prepare():
                for oid in seen:
                    snapshot.check_live()
                    kind, body = incoming.get(oid)
                    actual, loose = encode_object(kind, body, object_format=base.object_format)
                    if actual != oid:
                        raise ValueError("Agent Git object hash mismatch")
                    service.backend.put_durable(oid, loose)

            result = service.submit(
                grant,
                request_key=run["id"],
                generation=base.generation,
                edits=base.edits(tip),
                roots={tip: "commit"},
                prepare=prepare,
                message="Agent run " + run["id"],
            )
            return {**result, "commit_id": tip, "target_ref": base.ref_name.decode("utf-8")}
