"""History derived exclusively from an admitted, pinned Git ref snapshot."""

from __future__ import annotations

import hashlib
import heapq
import json
from dataclasses import replace

from src.version_engine.read.history_facts import _git_identity_time
from src.version_engine.read.history_models import HistoryGraphTooLargeError, HistoryRef
from src.version_engine.read.native_tree_reader import NativeTreeReader
from src.version_engine.write_engine.git_object_format import decode_commit, split_author_line
from src.version_engine.write_engine.git_object_graph import object_edges
from src.version_engine.write_engine.native_tree_diff import native_tree_diff


class SnapshotObjects:
    """Read-only ObjectStore port; no filesystem or unadmitted object lookup."""

    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.object_format = snapshot.object_format

    def get_object(self, oid):
        if oid == self.snapshot.empty_tree:
            return "tree", b""
        return self.snapshot.object(oid)

    def get(self, oid):
        return self.get_object(oid)[1]


class NativeHistory:
    def __init__(self, snapshot, *, max_commits=100_000):
        self.snapshot, self.store = snapshot, SnapshotObjects(snapshot)
        self.nodes, self.refs = {}, []
        roots = []
        for name in sorted(snapshot.refs):
            state = snapshot.refs[name]
            if state.oid is None:
                continue
            oid = state.oid
            seen = set()
            while oid not in seen:
                seen.add(oid)
                kind, body = snapshot.object(oid)
                if kind != "tag":
                    break
                oid = object_edges(kind, body, object_format=snapshot.object_format)[0][0]
            else:
                raise ValueError("cyclic Git tag")
            if kind != "commit":
                continue  # A tag can legitimately point to a tree or blob.
            roots.append(oid)
            if name != b"HEAD":
                self.refs.append(
                    HistoryRef(
                        name.decode("utf-8", "backslashreplace"),
                        "branch" if name.startswith(b"refs/heads/") else "tag",
                        oid,
                    )
                )
        todo = list(roots)
        while todo:
            oid = todo.pop()
            if oid in self.nodes:
                continue
            if len(self.nodes) >= max_commits:
                raise HistoryGraphTooLargeError("history commit budget exceeded")
            kind, body = snapshot.object(oid)
            if kind != "commit":
                raise ValueError("invalid Git history edge")
            info = decode_commit(body)
            parents = list(dict.fromkeys(info["parents"]))
            timestamp, created = _git_identity_time(
                info.get("committer") or info.get("author") or ""
            )
            identity, _ = split_author_line(info.get("author") or "")
            self.nodes[oid] = {
                "commit_id": oid,
                "parent_ids": parents,
                "root_hash": info["tree"],
                "scope_hash": "",
                "scope_path": "",
                "who": _display(identity),
                "message": _display(info.get("message") or ""),
                "created_at": created,
                "timestamp": timestamp,
                "conflicts": [],
            }
            todo.extend(parents)
        # Kahn order remains correct even when Git commit timestamps go backwards.
        children = dict.fromkeys(self.nodes, 0)
        for node in self.nodes.values():
            for parent in node["parent_ids"]:
                children[parent] += 1
        ready = [
            (-self.nodes[oid]["timestamp"], oid) for oid, count in children.items() if count == 0
        ]
        heapq.heapify(ready)
        self.order = []
        while ready:
            _, oid = heapq.heappop(ready)
            self.order.append(oid)
            for parent in self.nodes[oid]["parent_ids"]:
                children[parent] -= 1
                if children[parent] == 0:
                    heapq.heappush(ready, (-self.nodes[parent]["timestamp"], parent))
        if len(self.order) != len(self.nodes):
            raise ValueError("cyclic Git ancestry")
        self.head = snapshot.revision().commit_oid or ""
        self.roots = tuple(dict.fromkeys(roots))
        self.snapshot_id = hashlib.sha256(
            json.dumps(snapshot.to_wire()["refs"], sort_keys=True).encode()
        ).hexdigest()

    def require(self, oid):
        if oid not in self.nodes:
            raise ValueError("commit is not reachable from the current repository refs")
        return self.nodes[oid]

    def changes(self, oid):
        node = self.require(oid)
        parent = node["parent_ids"][0] if node["parent_ids"] else None
        old = self.nodes[parent]["root_hash"] if parent else self.snapshot.empty_tree
        return native_tree_diff(self.store, old, node["root_hash"])

    def entry(self, oid):
        return {**self.require(oid), "changes": self.changes(oid)}

    def content(self, oid, path):
        node = self.require(oid)
        reader = NativeTreeReader(self.snapshot)
        reader.revision = replace(reader.revision, tree_oid=node["root_hash"], commit_oid=oid)
        return reader.read_file(self.snapshot.project_id, path)

    def diff(self, before, after):
        return native_tree_diff(
            self.store, self.require(before)["root_hash"], self.require(after)["root_hash"]
        )

    def linear(self, limit, since="", path=None):
        # Only the selected HEAD ancestry belongs to the linear Project view.
        reachable, todo = set(), [self.head] if self.head else []
        while todo:
            oid = todo.pop()
            if oid not in reachable:
                reachable.add(oid)
                todo.extend(self.nodes[oid]["parent_ids"])
        ordered = [oid for oid in reversed(self.order) if oid in reachable]
        if since:
            if since not in ordered:
                raise ValueError("history anchor is not in the selected branch")
            ordered = ordered[ordered.index(since) + 1 :]
        if not path:
            ordered = ordered[:limit] if since else ordered[-limit:]
            return [self.entry(oid) for oid in ordered]
        entries = []
        for oid in ordered if since else reversed(ordered):
            entry = self.entry(oid)
            if any(c["path"] == path for c in entry["changes"]):
                entries.append(entry)
                if len(entries) >= limit:
                    break
        return entries if since else list(reversed(entries))

    def timestamps(self, paths):
        wanted, result = set(paths), {}
        for entry in self.linear(len(self.nodes)):
            for change in entry["changes"]:
                path = change["path"]
                if path in wanted:
                    result.setdefault(path, {"created_at": entry["created_at"]})["modified_at"] = (
                        entry["created_at"]
                    )
        return result


def _display(value):
    return value.encode("utf-8", "surrogateescape").decode("utf-8", "backslashreplace")
