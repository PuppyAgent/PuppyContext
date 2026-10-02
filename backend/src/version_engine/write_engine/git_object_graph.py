"""Shared typed edges for the current SHA-1 Git object store.

Transport and maintenance must agree about closure: tags are edges, gitlinks
are not. Parse only structural ASCII headers; arbitrary message/signature/name
bytes never get re-encoded into a new object. This is not a ref publication API
and does not enable native-ref hosting or SHA-256 repositories by itself.
"""

from __future__ import annotations

from typing import Literal, NamedTuple, cast

from src.version_engine.write_engine.git_object_format import decode_tree

ObjectKind = Literal["blob", "tree", "commit", "tag"]
_KINDS = frozenset({"blob", "tree", "commit", "tag"})


class ObjectEdge(NamedTuple):
    oid: str
    kind: ObjectKind


def _oid(value: bytes) -> str:
    if len(value) != 40 or any(char not in b"0123456789abcdef" for char in value):
        raise ValueError("invalid SHA-1 object reference")
    if value == b"0" * 40:
        raise ValueError("null object reference")
    return value.decode("ascii")


def _headers(body: bytes) -> dict[bytes, list[bytes]]:
    result: dict[bytes, list[bytes]] = {}
    for line in body.partition(b"\n\n")[0].split(b"\n"):
        # Continuations of gpgsig/mergetag are NOT independent graph headers.
        if line.startswith(b" "):
            continue
        key, _, value = line.partition(b" ")
        result.setdefault(key, []).append(value)
    return result


def _single(headers: dict[bytes, list[bytes]], name: bytes) -> bytes:
    values = headers.get(name, [])
    if len(values) != 1:
        raise ValueError(f"expected exactly one {name.decode('ascii')} header")
    return values[0]


def object_edges(
    kind: str, body: bytes, *, follow_history: bool = True,
) -> list[ObjectEdge]:
    """Return local dependencies, raising rather than truncating an invalid graph.

    ``follow_history=False`` omits commit parents for an explicitly shallow
    materialization. GC always uses the full closure. Parent ordering is kept.
    The consumer must verify each child's actual type and object hash on read.
    """
    if kind == "blob":
        return []
    if kind == "tree":
        edges: list[ObjectEdge] = []
        for entry in decode_tree(body):
            oid = _oid(entry.sha1_hex.encode("ascii"))
            if not entry.is_gitlink:
                edges.append(ObjectEdge(oid, "tree" if entry.is_dir else "blob"))
        return edges
    if kind == "commit":
        headers = _headers(body)
        tree = ObjectEdge(_oid(_single(headers, b"tree")), "tree")
        parents = [ObjectEdge(_oid(value), "commit") for value in headers.get(b"parent", [])]
        return [tree, *(parents if follow_history else [])]
    if kind == "tag":
        headers = _headers(body)
        target = _oid(_single(headers, b"object"))
        target_kind = _single(headers, b"type").decode("ascii")
        if target_kind not in _KINDS:
            raise ValueError(f"invalid tag target type: {target_kind!r}")
        return [ObjectEdge(target, cast(ObjectKind, target_kind))]
    raise ValueError(f"unsupported Git object type: {kind!r}")
