"""Current-reader repository identity/refs without pins, storage or Git work."""
from __future__ import annotations

import base64

from src.version_engine.write_engine.ref_transaction import (
    RefState,
    admitted_actor,
    validate_ref_name,
)


def decode_ref_name(value: str) -> bytes:
    if not isinstance(value, str) or not 1 <= len(value) <= 1400:
        raise ValueError("invalid repository ref selector")
    try:
        name = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid repository ref selector") from exc
    if base64.b64encode(name).decode("ascii") != value:
        raise ValueError("noncanonical repository ref selector")
    validate_ref_name(name)
    return name


def repository_ref_metadata(control, project_id, grant, *, max_refs=100_000):
    actor = admitted_actor(grant, project_id, write=False)
    reader = getattr(control, "read_snapshot", None)
    if not callable(reader):
        raise RuntimeError("admitted repository metadata unavailable")
    wire = reader(project_id, actor)
    try:
        if (not isinstance(wire, dict) or wire.get("project_id") != project_id
                or wire.get("authority") != "native"
                or wire.get("object_format") not in ("sha1", "sha256")
                or type(wire.get("generation")) is not int or not 1 <= wire["generation"] < 2**63
                or type(wire.get("ref_sequence")) is not int or not 0 <= wire["ref_sequence"] < 2**63
                or not isinstance(wire.get("refs"), list)):
            raise ValueError("invalid repository metadata binding")
        if max_refs <= 0 or len(wire["refs"]) > max_refs:
            raise ValueError("repository metadata budget exceeded")
        fmt, refs, kinds = wire["object_format"], {}, {}
        for row in wire["refs"]:
            name = decode_ref_name(row["name_b64"])
            if name in refs:
                raise ValueError("duplicate repository ref")
            value, kind, peeled = row["state"], row.get("kind"), row.get("peeled_oid")
            if value.get("kind") == "symbolic" and set(value) == {"kind", "target_b64"}:
                if name != b"HEAD" or kind is not None or peeled is not None:
                    raise ValueError("invalid symbolic repository ref")
                state = RefState(target=decode_ref_name(value["target_b64"]))
            elif value.get("kind") == "oid" and set(value) == {"kind", "oid"}:
                if not isinstance(value["oid"], str) or kind not in ("commit", "tree", "tag", "blob"):
                    raise ValueError("missing verified ref type")
                state = RefState(oid=value["oid"])
                if kinds.setdefault(state.oid, kind) != kind:
                    raise ValueError("inconsistent repository ref type")
                if (name == b"HEAD" or name.startswith(b"refs/heads/")) and kind != "commit":
                    raise ValueError("invalid branch target type")
                if peeled is not None:
                    if kind != "tag" or not isinstance(peeled, str):
                        raise ValueError("invalid ref peel metadata")
                    RefState(oid=peeled).wire(fmt)
            else:
                raise ValueError("invalid persisted repository ref")
            try:
                display = name.decode("utf-8")
            except UnicodeDecodeError:
                display = None
            refs[name] = {"name_b64": row["name_b64"], "name": display, "state": state.wire(fmt),
                          "object_kind": kind, "peeled_oid": peeled}
        if b"HEAD" not in refs:
            raise ValueError("repository HEAD metadata unavailable")
        return {"project_id": project_id, "repository_profile": "native", "object_format": fmt,
                "generation": wire["generation"], "ref_sequence": wire["ref_sequence"],
                "refs": [refs[name] for name in sorted(refs)]}
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        # Do not leak arbitrary control-plane data or fabricate a partial/empty repo.
        raise RuntimeError("invalid admitted repository metadata") from exc
