"""Fetch graph selection and Git v0/v1/v2 framing over published objects."""

from collections import deque
from contextlib import suppress
from urllib.parse import unquote

from src.version_engine.write_engine.git_object_graph import object_edges

from .execution import MAX_OBJECTS, checkpoint
from .native_wire import oid
from .native_wire import refs as wire_refs
from .object_pack import pack_chunks
from .protocol import pkt_line


class ObjectFilter:
    def __init__(self, spec=None, depth=0):
        self.kind, self.limit, self.children = None, None, []
        if depth > 8:
            raise ValueError("filter nesting exceeds budget")
        if spec is None:
            return
        if spec == "blob:none":
            self.kind = "none"
        elif spec.startswith("blob:limit="):
            self.kind = "blob"
            value = spec[11:]
            multiplier = {"k": 1024, "m": 1024**2, "g": 1024**3}.get(value[-1:].lower(), 1)
            self.limit = int(value if multiplier == 1 else value[:-1]) * multiplier
        elif spec.startswith("tree:"):
            self.kind, self.limit = "tree", int(spec[5:])
        elif spec.startswith("combine:"):
            self.kind = "combine"
            self.children = [ObjectFilter(unquote(part), depth + 1) for part in spec[8:].split("+")]
        else:
            raise ValueError("unsupported object filter")
        if self.limit is not None and self.limit < 0:
            raise ValueError("negative filter limit")

    def include(self, kind, depth, size=None):
        if self.kind == "none" and kind == "blob":
            return False
        if self.kind == "tree" and kind in {"tree", "blob"}:
            return depth < self.limit
        if self.kind == "blob" and kind == "blob" and size is not None:
            return size < self.limit
        return all(child.include(kind, depth, size) for child in self.children)

    @property
    def needs_size(self):
        return self.kind == "blob" or any(child.needs_size for child in self.children)


class FetchRequest:
    def __init__(self, packets, object_format, protocol_version):
        self.v2 = protocol_version == 2
        self.wants, self.haves, self.shallow = [], [], set()
        self.caps, self.depth, self.done, spec = set(), None, False, None
        self.since, self.exclude = None, []
        for value in packets:
            if not isinstance(value, bytes):
                continue
            line = value.rstrip(b"\n")
            parts = line.split()
            if not parts:
                continue
            command = parts[0]
            if command in (b"want", b"have", b"shallow"):
                if len(parts) < 2:
                    raise ValueError("missing fetch object id")
                value = oid(parts[1], object_format)
                if command == b"want":
                    self.wants.append(value)
                    self.caps.update(item.decode("ascii") for item in parts[2:])
                elif command == b"have":
                    self.haves.append(value)
                else:
                    self.shallow.add(value)
            elif command == b"deepen":
                if self.depth is not None or len(parts) != 2:
                    raise ValueError("invalid deepen request")
                self.depth = int(parts[1])
                if self.depth <= 0:
                    raise ValueError("invalid shallow depth")
            elif command == b"filter":
                if spec is not None or len(parts) != 2:
                    raise ValueError("invalid filter request")
                spec = parts[1].decode("ascii")
            elif command == b"done":
                self.done = True
            elif line.startswith(b"object-format="):
                if line != b"object-format=" + object_format.encode():
                    raise ValueError("fetch object format mismatch")
            elif command == b"deepen-since":
                if len(parts) != 2 or self.since is not None:
                    raise ValueError("invalid deepen-since")
                self.since = int(parts[1])
                if self.since < 0:
                    raise ValueError("negative deepen timestamp")
            elif command == b"deepen-not":
                if len(parts) != 2:
                    raise ValueError("invalid deepen-not")
                self.exclude.append(parts[1])
            else:
                self.caps.add(line.decode("ascii"))
        self.wants = list(dict.fromkeys(self.wants))
        if any(
            cap.startswith("object-format=") and cap != "object-format=" + object_format
            for cap in self.caps
        ):
            raise ValueError("fetch object format mismatch")
        self.filter = ObjectFilter(spec)
        self.relative = "deepen-relative" in self.caps
        if self.depth and (self.since is not None or self.exclude):
            raise ValueError("depth and revision shallow selectors cannot be combined")
        self.deepening = self.depth is not None or self.since is not None or bool(self.exclude)
        self.sideband = self.v2 or "side-band-64k" in self.caps or "side-band" in self.caps


def commit_time(body):
    timestamps = [
        line.rsplit(b" ", 2)[-2]
        for line in body.partition(b"\n\n")[0].split(b"\n")
        if line.startswith(b"committer ")
    ]
    if len(timestamps) != 1:
        raise ValueError("invalid commit timestamp")
    return int(timestamps[0])


def select_objects(reader, request):
    reader.authorize([*request.wants, *request.shallow])
    # Unrelated local history is not a server object capability.
    with suppress(PermissionError):
        reader.authorize(request.haves)
    haves = set(request.haves) & reader.allowed.keys()
    excluded_commits = set()
    rows = wire_refs(reader.snapshot.to_wire(), reader.format)
    stack = []
    for selector in request.exclude:
        candidates = {
            rows[name][0]
            for name in (selector, b"refs/heads/" + selector, b"refs/tags/" + selector)
            if name in rows and rows[name][0]
        }
        if len(candidates) != 1:
            raise ValueError("unknown or ambiguous deepen-not ref")
        stack.extend(candidates)
    while stack:
        current = stack.pop()
        if current in excluded_commits:
            continue
        excluded_commits.add(current)
        kind, body = reader.get(current)
        stack.extend(
            child
            for child, expected in object_edges(kind, body, object_format=reader.format)
            if expected in {"commit", "tag"}
        )
    excluded = set(haves)
    # Only walk the trees of common commits, not their entire history or blob
    # bodies. Sending redundant ancestors is valid and avoids an unbounded
    # graph walk just to optimize an incremental fetch.
    stack = list(haves)
    visited = set()
    while stack:
        current = stack.pop()
        if current in visited or reader.allowed[current] == "blob":
            continue
        visited.add(current)
        kind, body = reader.get(current)
        edges = object_edges(kind, body, follow_history=False, object_format=reader.format)
        for child, child_kind in edges:
            excluded.add(child)
            if child_kind != "blob":
                stack.append(child)

    selected, seen, commits, boundaries = {}, {}, {}, set()
    queue = deque((root, 1, 0, None) for root in request.wants)
    while queue:
        checkpoint()
        current, depth, tree_depth, remaining = queue.popleft()
        expected = reader.allowed[current]
        if len(seen) > MAX_OBJECTS:
            raise ValueError("fetch object count budget exceeded")
        if request.relative and current in request.shallow and request.depth:
            remaining = max(remaining or 0, request.depth + 1)
        # Shared trees can occur at different depths in different commits.
        # Relative deepening can also reach a merge through several boundaries.
        # Revisit whenever the new route permits a larger reachable closure.
        if expected in {"tree", "blob"}:
            rank = tree_depth
        elif request.relative:
            rank = -remaining if remaining is not None else float("-inf")
        else:
            rank = depth
        if current in seen and seen[current] <= rank:
            continue
        seen[current] = rank
        explicit = current in request.wants
        if (
            current in excluded
            and not explicit
            and not (request.deepening and expected in {"commit", "tag"})
        ):
            continue
        if not explicit and not request.filter.include(expected, tree_depth):
            continue
        if expected == "blob" and not request.filter.needs_size:
            selected[current] = None
            continue
        kind, body = reader.get(current)
        if not explicit and not request.filter.include(kind, tree_depth, len(body)):
            continue
        if kind == "commit" and (
            current in excluded_commits
            or (request.since is not None and commit_time(body) < request.since)
        ):
            raise ValueError("no commits selected for shallow request")
        if current not in excluded or explicit:
            selected[current] = None
        edges = object_edges(kind, body, object_format=reader.format)
        if kind == "commit":
            parents = [child for child, child_kind in edges if child_kind == "commit"]
            commits[current] = parents
            cut = not request.deepening and current in request.shallow
            if request.depth:
                cut = (
                    (remaining is not None and remaining <= 1)
                    if request.relative
                    else depth >= request.depth
                )
            if cut and parents:
                boundaries.add(current)
            for child, child_kind in edges:
                if child_kind == "commit":
                    parent_excluded = child in excluded_commits
                    if request.since is not None and not parent_excluded:
                        _, parent_body = reader.get(child)
                        parent_excluded = commit_time(parent_body) < request.since
                    if parent_excluded:
                        boundaries.add(current)
                    elif not cut:
                        queue.append(
                            (child, depth + 1, 0, None if remaining is None else remaining - 1)
                        )
                else:
                    queue.append((child, depth, 0, remaining))
        else:
            queue.extend(
                (child, depth, tree_depth + 1 if kind == "tree" else tree_depth, remaining)
                for child, _ in edges
            )
    # A merge can reach a boundary's parents through a shorter path.
    boundaries = {
        current
        for current in boundaries
        if any(parent not in commits for parent in commits[current])
    }
    shallow = boundaries - request.shallow if request.deepening else set()
    unshallow = (
        {current for current in request.shallow if current in commits and current not in boundaries}
        if request.deepening
        else set()
    )
    if "include-tag" in request.caps:
        for row in reader.snapshot.to_wire()["refs"]:
            target = row.get("peeled_oid")
            root = row["state"].get("oid")
            if target in selected and root not in selected:
                while root != target:
                    kind, body = reader.get(root)
                    selected[root] = None
                    root = object_edges(kind, body, object_format=reader.format)[0].oid
    return list(selected), shallow, unshallow


def fetch_chunks(reader, request):
    if not request.wants:
        yield b"0000"
        return
    if not request.done and request.v2:
        yield pkt_line(b"acknowledgments\n") + pkt_line(b"NAK\n") + b"0000"
        return
    if not request.done and not request.deepening:
        yield pkt_line(b"NAK\n")
        return
    objects, shallow, unshallow = select_objects(reader, request)
    boundary = b"".join(pkt_line(b"shallow " + value.encode() + b"\n") for value in sorted(shallow))
    boundary += b"".join(
        pkt_line(b"unshallow " + value.encode() + b"\n") for value in sorted(unshallow)
    )
    if request.v2:
        if boundary:
            yield pkt_line(b"shallow-info\n") + boundary + b"0001"
        yield pkt_line(b"packfile\n")
    else:
        if request.deepening:
            yield boundary + b"0000"
            if not request.done and not request.haves:
                # v0/v1 first perform a separate shallow-list exchange. A NAK
                # here leaks into the next stateless round as a bogus boundary.
                return
        yield pkt_line(b"NAK\n")
        if not request.done:
            return
    for chunk in pack_chunks(reader, objects):
        if request.sideband:
            for pos in range(0, len(chunk), 65515):
                yield pkt_line(b"\x01" + chunk[pos : pos + 65515])
        elif chunk:
            yield chunk
    if request.sideband:
        yield b"0000"
