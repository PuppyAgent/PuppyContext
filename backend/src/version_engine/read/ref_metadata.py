"""Current-reader repository identity/refs without pins, storage or Git work."""
from __future__ import annotations

import base64
from dataclasses import dataclass

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



@dataclass(frozen=True)
class GitBranchBase:
    """Git CAS identity needs refs, not the Product tree-splice base tree."""

    object_format: str
    generation: int
    ref_name: bytes
    expected: RefState
    head_guard: RefState

    @classmethod
    def from_metadata(cls, metadata):
        refs = {decode_ref_name(row['name_b64']): row['state'] for row in metadata['refs']}
        head = refs[b'HEAD']
        if head['kind'] != 'symbolic':
            raise ValueError('Agent Git requires a selected branch')
        name = decode_ref_name(head['target_b64'])
        expected = refs.get(name, {'kind': 'absent'})
        value = cls(metadata['object_format'], metadata['generation'], name,
                    RefState(oid=expected.get('oid')), RefState(target=name))
        return cls.parse(value.wire())

    @classmethod
    def parse(cls, wire):
        if wire.get('repository_profile') != 'native' or wire.get('object_format') not in ('sha1', 'sha256'):
            raise ValueError('invalid Git repository profile')
        if type(wire.get('generation')) is not int or not 1 <= wire['generation'] < 2**63:
            raise ValueError('invalid Git repository generation')
        name = decode_ref_name(wire['target_ref_b64'])
        if not name.startswith(b'refs/heads/'):
            raise ValueError('Git branch required')
        guard = wire['head_guard']
        if guard != {'kind': 'symbolic', 'target_b64': wire['target_ref_b64']}:
            raise ValueError('Git HEAD guard mismatch')
        oid = wire['expected_oid']
        if oid is not None and not isinstance(oid, str):
            raise ValueError('invalid Git base OID')
        value = cls(wire['object_format'], wire['generation'], name, RefState(oid=oid), RefState(target=name))
        value.expected.wire(value.object_format)
        value.head_guard.wire(value.object_format)
        return value

    def wire(self):
        return {'repository_profile': 'native', 'object_format': self.object_format,
                'generation': self.generation, 'target_ref_b64': base64.b64encode(self.ref_name).decode(),
                'target_ref': self.ref_name.decode('utf-8'), 'expected_oid': self.expected.oid,
                'head_guard': self.head_guard.wire(self.object_format)}

    def edits(self, candidate):
        from src.version_engine.write_engine.ref_transaction import RefEdit
        return (RefEdit(b'HEAD', self.head_guard), RefEdit(self.ref_name, self.expected, RefState(oid=candidate)))
