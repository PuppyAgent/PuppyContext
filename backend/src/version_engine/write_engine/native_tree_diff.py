"""Byte-preserving, mode-aware structural differences between native Git trees."""

from src.version_engine.write_engine.git_object_format import decode_tree


def native_tree_diff(store, before, after, *, max_entries=100_000):
    changes, pending, visited = [], [(before, after, "")], 0

    def entries(oid):
        if not oid:
            return {}
        kind, body = store.get_object(oid)
        if kind != "tree":
            raise ValueError("expected Git tree while comparing history")
        return {entry.name: entry for entry in decode_tree(body, object_format=store.object_format)}

    while pending:
        old, new, prefix = pending.pop()
        if old == new:
            continue
        left, right = entries(old), entries(new)
        for name in sorted(left.keys() | right.keys()):
            visited += 1
            if visited > max_entries:
                raise ValueError("history diff entry budget exceeded")
            a, b = left.get(name), right.get(name)
            if a and b and a.mode == b.mode and a.sha1_hex == b.sha1_hex:
                continue
            path = prefix + "/" + name if prefix else name
            if a and b and a.is_dir and b.is_dir:
                pending.append((a.sha1_hex, b.sha1_hex, path))
                continue
            changes.append(
                {
                    "path": path,
                    "op": "added" if a is None else "deleted" if b is None else "modified",
                }
            )
            # Include descendants of added/deleted/replaced directories so file
            # history and timestamps do not lose their initial creation event.
            if (a and a.is_dir) or (b and b.is_dir):
                pending.append(
                    (
                        a.sha1_hex if a and a.is_dir else None,
                        b.sha1_hex if b and b.is_dir else None,
                        path,
                    )
                )
    return sorted(changes, key=lambda item: item["path"])
