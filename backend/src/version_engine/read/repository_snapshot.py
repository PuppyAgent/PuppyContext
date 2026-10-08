"""Pinned, format-declared repository revisions for admitted internal readers.

This is not authentication, repository enrollment or a transport cache. Callers
must retain the context while reading. Objects become readable only through a
captured published ref or a verified graph edge, never through mere existence.
"""

from __future__ import annotations

import base64
import time
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType

from src.utils.logger import log_warning
from src.version_engine.domain.errors import RepositoryRefNotFoundError, RepositoryRefTypeError
from src.version_engine.infrastructure.owned_work import checkpoint
from src.version_engine.write_engine.git_object_format import (
    decode_object,
    encode_object,
    hash_object,
)
from src.version_engine.write_engine.git_object_graph import object_edges
from src.version_engine.write_engine.ref_transaction import (
    RefEdit,
    RefState,
    admitted_actor,
    validate_ref_name,
)


@dataclass(frozen=True)
class RepositoryRevision:
    selector: bytes
    ref_name: bytes
    expected: RefState
    commit_oid: str | None
    tree_oid: str
    head_guard: RefState | None = None

    def edit(self, new_oid: str) -> tuple[RefEdit, ...]:
        """CAS the selected ref AND the default-branch selector, when used."""
        update = RefEdit(self.ref_name, self.expected, RefState(oid=new_oid))
        if self.head_guard is not None:
            return RefEdit(b"HEAD", self.head_guard), update
        return (update,)


class RepositorySnapshot:
    def __init__(self, control, backend, *, project_id, actor, pin, wire, max_bytes=8 * 1024**3):
        if wire.get("authority") != "native":
            raise RuntimeError("native repository unavailable")
        if wire.get("project_id") != project_id or wire.get("pin_id") != pin:
            raise ValueError("repository snapshot binding mismatch")
        self.project_id, self.actor, self.pin = project_id, actor, pin
        self.control, self.backend = control, backend
        self.object_format = wire["object_format"]
        self.generation, self.ref_sequence = wire["generation"], wire["ref_sequence"]
        self.empty_tree, _ = encode_object("tree", b"", object_format=self.object_format)
        states, permitted = {}, {}
        for row in wire["refs"]:
            name = base64.b64decode(row["name_b64"], validate=True)
            validate_ref_name(name)
            if name in states:
                raise ValueError("duplicate snapshot ref")
            value = row["state"]
            if value["kind"] == "symbolic":
                if name != b"HEAD":
                    raise ValueError("only HEAD can be symbolic")
                state = RefState(target=base64.b64decode(value["target_b64"], validate=True))
            elif value["kind"] == "oid":
                state = RefState(oid=value["oid"])
                kind = row.get("kind")
                if name == b"HEAD" or name.startswith(b"refs/heads/"):
                    if kind not in {None, "commit"}:
                        raise ValueError("branch target is not a commit")
                    kind = "commit"
                if kind not in {None, "commit", "tree", "tag", "blob"}:
                    raise ValueError("invalid snapshot object kind")
                previous = permitted.get(state.oid)
                if previous is not None and kind is not None and previous != kind:
                    raise ValueError("inconsistent snapshot object kind")
                permitted[state.oid] = kind or previous
            else:
                raise ValueError("invalid persisted ref state")
            state.wire(self.object_format)
            states[name] = state
        if b"HEAD" not in states:
            raise RuntimeError("repository_metadata_incomplete")
        self.refs = MappingProxyType(states)
        self.roots = MappingProxyType(permitted.copy())
        self._wire = deepcopy(wire)
        self._permitted = permitted
        self._next_renewal = time.monotonic() + 30
        self._closed = False
        self._remaining_bytes = max_bytes
        self._verified_sizes = {}
        # Only verified bytes read during this pin are reusable. No global cache
        # or location snapshot is required, and publication still reads durably.
        self._objects = OrderedDict()
        self._cache_bytes = 0
        self._cache_limit = min(max_bytes, 8 * 1024**2)

    def to_wire(self) -> dict:
        return deepcopy(self._wire)

    def check_live(self):
        checkpoint()
        if self._closed:
            raise RuntimeError("repository snapshot is closed")
        if time.monotonic() >= self._next_renewal:
            self.control.renew(self.project_id, self.actor, self.pin)
            self._next_renewal = time.monotonic() + 30

    def object(self, oid: str) -> tuple[str, bytes]:
        self.check_live()
        if oid not in self._permitted:
            raise PermissionError("object is not reachable from the captured refs")
        if oid in self._objects:
            self._objects.move_to_end(oid)
            return self._objects[oid]
        budget = self._verified_sizes.get(oid, self._remaining_bytes)
        kind, body = decode_object(self.backend.get_durable(oid), max_bytes=budget)
        if hash_object(kind, body, object_format=self.object_format) != oid:
            raise ValueError("snapshot object hash mismatch")
        expected = self._permitted[oid]
        if expected is not None and kind != expected:
            raise ValueError("snapshot object type mismatch")
        edges = object_edges(kind, body, object_format=self.object_format)
        additions = {oid: kind}
        for child, child_kind in edges:
            previous = additions.get(child, self._permitted.get(child))
            if previous is not None and previous != child_kind:
                raise ValueError("snapshot edge type mismatch")
            additions[child] = child_kind
        self._permitted.update(additions)
        if oid not in self._verified_sizes:
            self._remaining_bytes -= len(body)
            self._verified_sizes[oid] = len(body)
        if len(body) <= self._cache_limit:
            while self._objects and (
                self._cache_bytes + len(body) > self._cache_limit or len(self._objects) >= 1024
            ):
                _, (_, removed) = self._objects.popitem(last=False)
                self._cache_bytes -= len(removed)
            self._objects[oid] = (kind, body)
            self._cache_bytes += len(body)
        return kind, body

    def revision(
        self, selector: bytes = b"HEAD", *, allow_absent: bool = False
    ) -> RepositoryRevision:
        self.check_live()
        validate_ref_name(selector)
        state = self.refs.get(selector, RefState())
        ref_name, head_guard = selector, None
        if state.target is not None:
            ref_name, head_guard = state.target, state
            state = self.refs.get(ref_name, RefState())
        if state.oid is None:
            if head_guard is None and not allow_absent:
                raise RepositoryRefNotFoundError("repository ref does not exist")
            return RepositoryRevision(selector, ref_name, state, None, self.empty_tree, head_guard)
        oid, visited = state.oid, set()
        while True:
            if oid in visited:
                raise ValueError("snapshot tag cycle")
            visited.add(oid)
            kind, body = self.object(oid)
            edges = object_edges(kind, body, object_format=self.object_format)
            if kind == "tag":
                oid = edges[0][0]
                continue
            if kind == "commit":
                tree = next(child for child, child_kind in edges if child_kind == "tree")
                return RepositoryRevision(selector, ref_name, state, oid, tree, head_guard)
            if kind == "tree":
                return RepositoryRevision(selector, ref_name, state, None, oid, head_guard)
            raise RepositoryRefTypeError("repository ref has no tree")

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._objects.clear()
        self._cache_bytes = 0
        try:
            self.control.release(self.project_id, self.actor, self.pin)
        except Exception:
            log_warning("native read pin release deferred to expiry")


@contextmanager
def repository_snapshot(control, backend, grant, *, project_id, max_bytes=8 * 1024**3):
    if max_bytes < 0:
        raise ValueError("negative snapshot byte budget")
    actor = admitted_actor(grant, project_id, write=False)
    binding = backend.publication_project_id
    if binding is not None and binding != project_id:
        raise ValueError("object storage Project binding mismatch")
    pin = str(uuid.uuid4())
    wire = control.begin_read(project_id, actor, pin)
    try:
        snapshot = RepositorySnapshot(
            control,
            backend,
            project_id=project_id,
            actor=actor,
            pin=pin,
            wire=wire,
            max_bytes=max_bytes,
        )
    except Exception:
        try:
            control.release(project_id, actor, pin)
        except Exception:
            log_warning("native read pin release deferred to expiry")
        raise
    try:
        yield snapshot
    finally:
        snapshot.close()
