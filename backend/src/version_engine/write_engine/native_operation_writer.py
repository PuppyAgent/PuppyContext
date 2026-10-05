"""Product tree splices -> durable objects -> the existing native ref transaction.

No Git transport/cache materialization, legacy root, implicit enrollment or
latest-head substitution. Callers own normalization and hash the complete input;
this boundary additionally binds the selected revision and commit message.
"""
from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.version_engine.domain.errors import NativeObjectNotFoundError, ObjectNotFoundError
from src.version_engine.read.repository_snapshot import repository_snapshot
from src.version_engine.storage.object_store import ObjectStore
from src.version_engine.write_engine.errors import NativeRevisionConflictError
from src.version_engine.write_engine.git_object_format import encode_commit, encode_object
from src.version_engine.write_engine.ref_transaction import (
    RefEdit,
    RefState,
    admitted_actor,
    publication_pin_id,
    validate_ref_name,
)


def _byte_name(value):
    if not isinstance(value, str) or len(value) > 1400:
        raise ValueError("invalid native ref identity")
    try:
        name = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid native ref identity") from exc
    validate_ref_name(name)
    if base64.b64encode(name).decode("ascii") != value:
        raise ValueError("noncanonical native ref identity")
    return name


@dataclass(frozen=True)
class NativeWriteBase:
    object_format: str
    generation: int
    ref_name: bytes
    expected: RefState
    tree_oid: str
    head_guard: RefState | None

    @classmethod
    def parse(cls, wire):
        required = {"repository_profile", "object_format", "generation", "target_ref_b64",
                    "expected_oid", "tree_oid", "head_guard"}
        if not isinstance(wire, dict) or not required <= wire.keys() or wire["repository_profile"] != "native":
            raise ValueError("complete native starting revision required")
        fmt, generation = wire["object_format"], wire["generation"]
        if not isinstance(fmt, str) or fmt not in {"sha1", "sha256"} or type(generation) is not int or not 1 <= generation < 2**63:
            raise ValueError("invalid native starting revision")
        name = _byte_name(wire["target_ref_b64"])
        if name != b"HEAD" and not name.startswith(b"refs/heads/"):
            raise ValueError("native Product edits require a branch or detached HEAD")
        if wire["expected_oid"] is not None and not isinstance(wire["expected_oid"], str):
            raise ValueError("invalid native starting OID")
        expected = RefState(oid=wire["expected_oid"])
        expected.wire(fmt)
        tree = wire["tree_oid"]
        if not isinstance(tree, str):
            raise ValueError("native starting tree required")
        RefState(oid=tree).wire(fmt)
        guard = None
        if wire["head_guard"] is not None:
            value = wire["head_guard"]
            if not isinstance(value, dict) or set(value) != {"kind", "target_b64"} or value["kind"] != "symbolic":
                raise ValueError("invalid native HEAD guard")
            guard = RefState(target=_byte_name(value["target_b64"]))
            guard.wire(fmt)
            if guard.target != name:
                raise ValueError("native target and HEAD guard differ")
        return cls(fmt, generation, name, expected, tree, guard)

    def identity(self):
        return {"object_format": self.object_format, "generation": self.generation,
                "target_ref_b64": base64.b64encode(self.ref_name).decode("ascii"),
                "expected": self.expected.wire(self.object_format), "tree_oid": self.tree_oid,
                "head_guard": self.head_guard.wire(self.object_format) if self.head_guard else None}

    def edits(self, new_oid):
        update = RefEdit(self.ref_name, self.expected, RefState(oid=new_oid) if new_oid else None)
        return (RefEdit(b"HEAD", self.head_guard), update) if self.head_guard else (update,)


class _DraftObjects:
    """Only changed objects on private scratch disk; never a durable backend."""

    def __init__(self, directory, snapshot, verifier):
        self.directory, self.snapshot = Path(directory), snapshot
        self.max_bytes, self.max_objects = verifier.max_bytes, verifier.max_objects
        self.bytes = 0
        self.objects = {}

    def put(self, oid, loose):
        _kind, body = ObjectStore._decode_verified(oid, loose, object_format=self.snapshot.object_format)
        if oid in self.objects:
            return
        if len(self.objects) >= self.max_objects or self.bytes + len(body) > self.max_bytes:
            raise ValueError("native Product draft budget exceeded")
        path = self.directory / oid
        path.write_bytes(loose)
        self.objects[oid] = path
        self.bytes += len(body)

    def get(self, oid):
        if oid in self.objects:
            return self.objects[oid].read_bytes()
        kind, body = self.snapshot.object(oid)
        return encode_object(kind, body, object_format=self.snapshot.object_format)[1]

    def exists(self, oid):
        self.get(oid)
        return True

    def publish(self, backend):
        for oid, path in self.objects.items():
            backend.put_durable(oid, path.read_bytes())


class NativeOperationWriter:
    def __init__(self, service):
        if (service.capacity is None or service.billing is None or service.policy is None
                or service.backend.publication_project_id != service.project_id
                or not callable(getattr(service.control, "begin_product_operation", None))
                or not callable(getattr(service.control, "prepare_product_operation", None))
                or not callable(getattr(service.control, "open_product_attempt", None))):
            raise RuntimeError("complete native Product admission required")
        self.service = service

    def _request(self, grant, request_key, base, input_sha256, message):
        actor = admitted_actor(grant, self.service.project_id, write=False)
        if not isinstance(request_key, str):
            raise ValueError("native Product request UUID required")
        key = str(uuid.UUID(request_key))
        selected = NativeWriteBase.parse(base)
        if selected.object_format != self.service.object_format:
            raise ValueError("repository object format mismatch")
        if (not isinstance(input_sha256, str) or len(input_sha256) != 64
                or not set(input_sha256) <= set("0123456789abcdef")):
            raise ValueError("complete normalized Product input digest required")
        if not isinstance(message, str) or len(message.encode("utf-8")) > 8192 or "\0" in message:
            raise ValueError("invalid native Product message")
        identity = {"version": 1, "base": selected.identity(), "input_sha256": input_sha256, "message": message}
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")).hexdigest()
        return actor, key, selected, digest

    def _record(self, record, actor, key, base, digest):
        expected = {"project_id": self.service.project_id, "actor": actor, "request_key": key,
                    "input_sha256": digest, "object_format": base.object_format, "generation": base.generation}
        if not isinstance(record, dict) or any(record.get(k) != v for k, v in expected.items()):
            raise RuntimeError("invalid native Product journal binding")
        if "proposal" not in record or "result" not in record or not isinstance(record.get("created_at"), str):
            raise RuntimeError("incomplete native Product journal")
        if record["result"] is not None:
            result = record["result"]
            if (not isinstance(result, dict) or result.get("project_id") != self.service.project_id
                    or result.get("status") not in {"committed", "rejected"}
                    or not isinstance(record["proposal"], dict)):
                raise RuntimeError("invalid native Product result")
        return record

    @staticmethod
    def _result(record):
        result = record["result"]
        if (not isinstance(result, dict) or result.get("project_id") != record["project_id"]
                or type(result.get("generation")) is not int or result["generation"] != record["generation"]
                or result.get("status") not in {"committed", "rejected"}):
            raise RuntimeError("invalid native Product result binding")
        result = dict(result)
        product = record["proposal"].get("product_result")
        if product is not None:
            result["product"] = product
        return result

    def replay(self, grant, *, request_key, base, input_sha256, message=""):
        actor, key, selected, digest = self._request(grant, request_key, base, input_sha256, message)
        reader = getattr(self.service.control, "read_product_operation", None)
        if not callable(reader):
            raise RuntimeError("native Product result lookup unavailable")
        record = reader(self.service.project_id, actor, key, digest, selected.generation)
        if record is None:
            return None
        record = self._record(record, actor, key, selected, digest)
        return self._result(record) if record["result"] is not None else None

    @contextmanager
    def _draft(self, grant, actor, key, selected, record, splice, message):
        service = self.service
        with repository_snapshot(service.control, service.backend, grant, project_id=service.project_id,
                                 max_bytes=service.verifier.max_bytes) as snapshot:
            if (snapshot.object_format != selected.object_format or snapshot.generation != selected.generation
                    or snapshot.refs.get(selected.ref_name, RefState()) != selected.expected
                    or (selected.head_guard is not None and snapshot.refs.get(b"HEAD") != selected.head_guard)):
                raise NativeRevisionConflictError("native starting ref or HEAD changed")
            revision = snapshot.revision(b"HEAD" if selected.head_guard else selected.ref_name, allow_absent=True)
            if revision.tree_oid != selected.tree_oid:
                raise NativeRevisionConflictError("native starting tree differs from its commit")
            with tempfile.TemporaryDirectory(prefix="puppyone-product-draft-") as directory:
                draft = _DraftObjects(directory, snapshot, service.verifier)
                store = ObjectStore(Path(directory), backend=draft, object_format=selected.object_format)
                tree, changes = splice(store, revision.tree_oid)
                if store.get_object(tree)[0] != "tree":
                    raise ValueError("native Product splice must produce a tree")
                new_oid = None
                if tree != revision.tree_oid:
                    created = datetime.fromisoformat(record["created_at"].replace("Z", "+00:00"))
                    if created.utcoffset() is None:
                        raise RuntimeError("native Product journal clock lacks timezone")
                    clock = f"{int(created.timestamp())} +0000"
                    identity = "".join("_" if c in "\r\n\0<>" else c for c in actor) + " <version@puppyone.invalid>"
                    new_oid = store.put_commit(encode_commit(tree, revision.commit_oid, identity, clock,
                                                             identity, clock, message))
                edits = selected.edits(new_oid)
                pin = publication_pin_id(service.project_id, actor, key) if new_oid else None
                proposal = {"updates": [edit.wire(selected.object_format) for edit in edits],
                            "receipt_id": pin, "message": message,
                            "product_result": {"tree_oid": tree, "commit_oid": new_oid or revision.commit_oid,
                                               "changes": [[action, base64.b64encode(path.encode("utf-8", "surrogateescape")).decode("ascii")]
                                                           for action, path in changes]}}
                yield draft, proposal

    def _submit(self, grant, actor, key, selected, record, prepare, message):
        proposal = record["proposal"]
        updates = proposal["updates"]
        new_oid = updates[-1].get("new", {}).get("oid")
        edits = selected.edits(new_oid)
        pin = str(uuid.UUID(proposal["receipt_id"])) if new_oid else None
        if (updates != [edit.wire(selected.object_format) for edit in edits]
                or proposal["receipt_id"] != pin or proposal["message"] != message):
            raise RuntimeError("native Product candidate binding mismatch")
        try:
            result = self.service.submit(grant, request_key=key, generation=selected.generation, edits=edits,
                                         roots={new_oid: "commit"} if new_oid else {}, prepare=prepare,
                                         message=message, publication_id=pin)
        except Exception:
            # A different physical attempt may have won while this worker was
            # fenced. Recover only that committed result through current read
            # admission; this does not settle this invocation's uncertain I/O.
            try:
                raced = self._record(self.service.control.read_product_operation(
                    self.service.project_id, actor, key, record["input_sha256"], selected.generation),
                    actor, key, selected, record["input_sha256"])
            except Exception:
                raced = None
            if (raced is not None and raced["result"] is not None
                    and raced["result"].get("status") == "committed"
                    and raced["result"].get("receipt_id") not in {None, pin}):
                return self._result(raced)
            raise
        return self._result({**record, "result": result})

    def apply(self, grant, *, request_key, base, input_sha256, splice, message=""):
        service = self.service
        actor, key, selected, digest = self._request(grant, request_key, base, input_sha256, message)
        def begin():
            return self._record(service.control.begin_product_operation(
                service.project_id, actor, key, digest, selected.generation), actor, key, selected, digest)
        record = begin()
        if record["result"] is not None:
            return self._result(record)
        admitted_actor(grant, service.project_id, write=True)
        attempt_id = str(uuid.uuid4())
        def open_attempt():
            return self._record(service.control.open_product_attempt(
                service.project_id, actor, key, digest, selected.generation, attempt_id),
                actor, key, selected, digest)
        try:
            if record["proposal"] is not None:
                original_proposal = record["proposal"]
                record = open_attempt()
                if record["result"] is not None:
                    return self._result(record)
                def prepare():
                    with self._draft(grant, actor, key, selected, record, splice, message) as (draft, proposal):
                        if proposal != original_proposal:
                            raise ValueError("request_key_reused")
                        draft.publish(service.backend)
                # Live verified receipts resume without splice/PUT. New upload
                # attempts get distinct pins; retired I/O claims remain intact.
                return self._submit(grant, actor, key, selected, record, prepare, message)
            with self._draft(grant, actor, key, selected, record, splice, message) as (draft, proposal):
                record = self._record(service.control.prepare_product_operation(
                    service.project_id, actor, key, digest, proposal), actor, key, selected, digest)
                if record["result"] is not None:
                    return self._result(record)
                record = open_attempt()
                if record["result"] is not None:
                    return self._result(record)
                return self._submit(grant, actor, key, selected, record,
                                    lambda: draft.publish(service.backend), message)
        except NativeRevisionConflictError:
            raced = begin()
            if raced["result"] is not None:
                return self._result(raced)
            raise
        except ObjectNotFoundError as exc:
            raise NativeObjectNotFoundError("Canonical repository object unavailable") from exc
