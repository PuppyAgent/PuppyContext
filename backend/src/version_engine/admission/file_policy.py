"""Single-file admission over verified trees, without expanding file paths."""
from __future__ import annotations

from collections import Counter, deque

from src.version_engine.storage.publication import ClosureManifest


def oversized_blob_occurrences(manifest: ClosureManifest, tree_oid: str | None, limit: int) -> Counter:
    """Count oversized logical files per OID, preserving DAG path multiplicity.

    This supports the existing rename/grandfather vs additional-copy policy.
    History and external gitlinks do not become current-tree file occurrences.
    """
    if type(limit) is not int or limit < 0:
        raise ValueError('invalid single-file limit')
    if tree_oid is None:
        return Counter()
    records, incoming, trees, stack, oversized = manifest.objects, Counter(), set(), [tree_oid], set()
    while stack:
        oid = stack.pop()
        if oid in trees:
            continue
        record = records.get(oid)
        if record is None or record.kind != 'tree':
            raise ValueError('file policy requires a complete typed tree')
        trees.add(oid)
        for child, kind in record.edges:
            target = records.get(child)
            if target is None or target.kind != kind or kind not in {'tree', 'blob'}:
                raise ValueError('file policy requires a complete typed tree')
            if kind == 'tree':
                incoming[child] += 1
                stack.append(child)
            elif type(target.size) is not int or target.size < 0:
                raise ValueError('invalid logical file size')
            elif target.size > limit:
                oversized.add(child)
    queue = deque(oid for oid in trees if incoming[oid] == 0)
    order = []
    while queue:
        oid = queue.popleft()
        order.append(oid)
        for child, kind in records[oid].edges:
            if kind == 'tree':
                incoming[child] -= 1
                if incoming[child] == 0:
                    queue.append(child)
    if len(order) != len(trees):
        raise ValueError('logical file tree cycle')
    needed = set()
    for oid in reversed(order):
        if any(child in oversized or child in needed for child, _ in records[oid].edges):
            needed.add(oid)
    ways, result = Counter({tree_oid: 1}), Counter()
    for oid in order:
        if oid not in needed:
            continue
        for child, kind in records[oid].edges:
            if kind == 'tree' and child in needed:
                ways[child] += ways[oid]
                if ways[child] > 2**63-1:
                    raise OverflowError('logical file multiplicity exceeds the counter range')
            elif child in oversized:
                result[child] += ways[oid]
                if result[child] > 2**63-1:
                    raise OverflowError('logical file multiplicity exceeds the counter range')
    return result


def newly_oversized_logical_files(before, before_tree, after, after_tree, limit):
    """A pure move remains legal; an extra path to an oversized blob does not."""
    return oversized_blob_occurrences(after, after_tree, limit) - oversized_blob_occurrences(before, before_tree, limit)
