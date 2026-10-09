"""Pinned object access for transport; cache eviction never changes correctness."""

from collections import OrderedDict

from src.version_engine.write_engine.git_object_format import decode_object, hash_object
from src.version_engine.write_engine.git_object_graph import object_edges

from .execution import MAX_GRAPH_BYTES, MAX_OBJECT_BYTES, MAX_OBJECTS


class PublishedObjectReader:
    CACHE_BYTES = 8 * 1024**2

    def __init__(self, snapshot, control, *, proof_factory=None):
        self.snapshot, self.control = snapshot, control
        self.format = snapshot.object_format
        self.allowed = dict(snapshot.roots)
        self.cache = OrderedDict()
        self.cache_bytes = 0
        self.verified = {}
        self.total_bytes = 0
        self.expanded = set()
        self.history_loaded = False
        self.proofs = (
            proof_factory(
                control,
                snapshot.project_id,
                snapshot.actor,
                snapshot.pin,
                snapshot.object_format,
                purpose="read",
            )
            if proof_factory
            else None
        )

    def get(self, oid):
        self.snapshot.check_live()
        if oid not in self.allowed:
            raise PermissionError("object is not reachable from published refs")
        if oid in self.cache:
            self.cache.move_to_end(oid)
            return self.cache[oid]
        kind, body = decode_object(
            self.snapshot.backend.get_durable(oid), max_bytes=MAX_OBJECT_BYTES
        )
        if hash_object(kind, body, object_format=self.format) != oid:
            raise ValueError("transport object hash mismatch")
        if self.allowed[oid] not in (None, kind):
            raise ValueError("transport object type mismatch")
        if oid not in self.verified:
            self.total_bytes += len(body)
            self.verified[oid] = len(body)
            if self.total_bytes > MAX_GRAPH_BYTES or len(self.verified) > MAX_OBJECTS:
                raise ValueError("Git graph budget exceeded")
        self.allowed[oid] = kind
        for child, expected in object_edges(kind, body, object_format=self.format):
            if self.allowed.get(child) not in (None, expected):
                raise ValueError("transport edge type mismatch")
            self.allowed[child] = expected
        if len(self.allowed) > MAX_OBJECTS:
            raise ValueError("Git object count budget exceeded")
        if len(body) <= self.CACHE_BYTES:
            while self.cache and self.cache_bytes + len(body) > self.CACHE_BYTES:
                _, (_, removed) = self.cache.popitem(last=False)
                self.cache_bytes -= len(removed)
            self.cache[oid] = kind, body
            self.cache_bytes += len(body)
        return kind, body

    def authorize(self, oids):
        """Prove client supplied IDs through published edges, never S3 existence.

        Only structural objects are read during this search. Retained published
        roots cover an advertisement raced by a force update or a lazy fetch.
        The repository read pin remains held through the entire operation.
        """
        pending = set(oids) - self.allowed.keys()
        if pending and self.proofs is not None:
            self.proofs.prefetch(pending)
            for oid in list(pending):
                record = self.proofs.get(oid)
                if record is not None:
                    self.allowed[oid] = record.kind
                    pending.remove(oid)
        for historical in (False, True):
            if not pending:
                return
            if historical and not self.history_loaded:
                roots = self.control.call(
                    "get_version_repository_published_roots", p_project_id=self.snapshot.project_id
                )
                for oid, kind in roots.items():
                    if self.allowed.get(oid) not in (None, kind):
                        raise ValueError("published root type mismatch")
                    self.allowed[oid] = kind
                if len(self.allowed) > MAX_OBJECTS:
                    raise ValueError("published object count budget exceeded")
                self.history_loaded = True
            stack = [
                oid
                for oid, kind in self.allowed.items()
                if kind != "blob" and oid not in self.expanded
            ]
            while stack and pending:
                oid = stack.pop()
                if oid in self.expanded:
                    continue
                kind, body = self.get(oid)
                self.expanded.add(oid)
                stack.extend(
                    child
                    for child, expected in object_edges(kind, body, object_format=self.format)
                    if expected != "blob" and child not in self.expanded
                )
                pending.difference_update(self.allowed)
            pending.difference_update(self.allowed)
        if pending:
            raise PermissionError("requested object is not published")
