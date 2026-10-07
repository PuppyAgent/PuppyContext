"""Smart HTTP discovery and bounded packet parsing for canonical native refs."""

import base64

from src.version_engine.write_engine.ref_transaction import RefEdit, RefState, validate_ref_name

from .execution import MAX_OBJECTS, MAX_REFS, checkpoint
from .protocol import pkt_line


def version(protocol):
    versions = [part for part in protocol.split(":") if part.startswith("version=")]
    if len(versions) > 1 or (
        versions and versions[0] not in {"version=0", "version=1", "version=2"}
    ):
        raise ValueError("unsupported Git protocol version")
    return int(versions[0][-1]) if versions else 0


def packet(handle):
    header = handle.read(4)
    if not header:
        return None
    if len(header) != 4 or any(c not in b"0123456789abcdefABCDEF" for c in header):
        raise ValueError("invalid pkt-line header")
    size = int(header, 16)
    if size in (0, 1, 2):
        return size
    if not 4 <= size <= 65520:
        raise ValueError("invalid pkt-line size")
    value = handle.read(size - 4)
    if len(value) != size - 4:
        raise ValueError("truncated pkt-line")
    return value


def upload_packets(path):
    result = []
    with path.open("rb") as source:
        while (value := packet(source)) is not None:
            checkpoint()
            result.append(value)
            if len(result) > MAX_OBJECTS:
                raise ValueError("too many upload commands")
    return result


def oid(value, object_format, *, zero=False):
    width = 40 if object_format == "sha1" else 64
    if len(value) != width or any(c not in b"0123456789abcdef" for c in value):
        raise ValueError("invalid Git object id")
    if not zero and value == b"0" * width:
        raise ValueError("null Git object id")
    return value.decode("ascii")


def refs(snapshot, object_format):
    if not snapshot or snapshot.get("authority") != "native":
        raise RuntimeError("native repository unavailable")
    if snapshot.get("object_format") != object_format:
        raise ValueError("repository object format mismatch")
    if len(snapshot["refs"]) > MAX_REFS:
        raise ValueError("repository ref budget exceeded")
    result = {}
    for row in snapshot["refs"]:
        name = base64.b64decode(row["name_b64"], validate=True)
        validate_ref_name(name)
        if name in result:
            raise ValueError("duplicate ref")
        state = row["state"]
        if state["kind"] == "symbolic":
            target = base64.b64decode(state["target_b64"], validate=True)
            validate_ref_name(target)
            if name != b"HEAD":
                raise ValueError("only HEAD can be symbolic")
            result[name] = (None, target, None)
        else:
            value = oid(state["oid"].encode(), object_format)
            peeled = row.get("peeled_oid")
            if peeled:
                oid(peeled.encode(), object_format)
            result[name] = (value, None, peeled)
    if b"HEAD" not in result:
        raise RuntimeError("native repository has no authoritative HEAD")
    value, target, _ = result[b"HEAD"]
    if target:
        result[b"HEAD"] = (result.get(target, (None,))[0], target, None)
    return result


def advertisement(snapshot, service, protocol, object_format):
    rows = refs(snapshot, object_format)
    v = version(protocol)
    if service == "git-upload-pack" and v == 2:
        return (
            b"".join(
                pkt_line(line + b"\n")
                for line in (
                    b"version 2",
                    b"agent=puppyone",
                    b"ls-refs=unborn",
                    b"fetch=shallow wait-for-done filter",
                    b"object-format=" + object_format.encode(),
                )
            )
            + b"0000"
        )
    caps = (
        b"report-status delete-refs side-band-64k atomic ofs-delta"
        if service == "git-receive-pack"
        else b"side-band-64k thin-pack ofs-delta shallow deepen-relative deepen-since deepen-not no-progress include-tag allow-reachable-sha1-in-want filter"
    )
    caps += b" object-format=" + object_format.encode() + b" agent=puppyone"
    head_target = rows[b"HEAD"][1]
    if head_target:
        caps += b" symref=HEAD:" + head_target
    lines = []
    for name, (value, _, peeled) in sorted(rows.items()):
        if value:
            lines.append(value.encode() + b" " + name)
            if peeled:
                lines.append(peeled.encode() + b" " + name + b"^{}")
    if not lines:
        lines = [b"0" * (40 if object_format == "sha1" else 64) + b" capabilities^{}"]
    lines[0] += b"\0" + caps
    prefix = pkt_line(b"version 1\n") if v == 1 and service == "git-upload-pack" else b""
    return prefix + b"".join(pkt_line(line + b"\n") for line in lines) + b"0000"


def ls_refs(snapshot, packets, object_format):
    rows = refs(snapshot, object_format)
    args = [line.rstrip(b"\n") for line in packets if isinstance(line, bytes)]
    prefixes = [line[11:] for line in args if line.startswith(b"ref-prefix ")]
    result = []
    for name, (value, target, peeled) in sorted(rows.items()):
        if prefixes and not any(name.startswith(prefix) for prefix in prefixes):
            continue
        if not value and b"unborn" not in args:
            continue
        line = (value.encode() if value else b"unborn") + b" " + name
        if target and b"symrefs" in args:
            line += b" symref-target:" + target
        if peeled and b"peel" in args:
            line += b" peeled:" + peeled.encode()
        result.append(pkt_line(line + b"\n"))
    return b"".join(result) + b"0000"


def receive_commands(handle, object_format):
    edits, capabilities = [], set()
    zero = "0" * (40 if object_format == "sha1" else 64)
    while True:
        value = packet(handle)
        if value == 0:
            break
        if not isinstance(value, bytes):
            raise ValueError("truncated receive request")
        if value.startswith(b"shallow "):
            oid(value.strip().split()[1], object_format)
            continue
        command, sep, caps = value.rstrip(b"\n").partition(b"\0")
        if sep:
            if edits:
                raise ValueError("capabilities after first command")
            capabilities = set(caps.decode("ascii").split())
        old, new, name = command.split(b" ", 2)
        old, new = oid(old, object_format, zero=True), oid(new, object_format, zero=True)
        edit = RefEdit(
            name,
            RefState(oid=None if old == zero else old),
            RefState(oid=None if new == zero else new),
        )
        edit.wire(object_format)
        edits.append(edit)
        if len(edits) > 256:
            raise ValueError("too many ref commands")
    if len({edit.name for edit in edits}) != len(edits):
        raise ValueError("duplicate ref command")
    for cap in capabilities:
        if cap.startswith("object-format=") and cap != "object-format=" + object_format:
            raise ValueError("receive object format mismatch")
    return edits, capabilities
