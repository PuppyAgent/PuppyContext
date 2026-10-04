"""Normalize Product commands once, then use the common native write boundary."""
from __future__ import annotations

import base64
import hashlib
import json

from src.version_engine.adapters.product.tree_patch import (
    splice_batch,
    splice_copy,
    splice_mkdir,
    splice_move,
    splice_remove,
    splice_touch,
)
from src.version_engine.write_engine.tree import read_tree_entries


def _identity(value):
    if isinstance(value, bytes):
        return {"bytes_sha256": hashlib.sha256(value).hexdigest(), "size": len(value)}
    if isinstance(value, str):
        return {"text_b64": base64.b64encode(value.encode("utf-8", "surrogateescape")).decode("ascii")}
    if isinstance(value, dict):
        return {key: _identity(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_identity(item) for item in value]
    return value


def native_path(path):
    # These names become Git tree entries, never filesystem paths. In
    # particular a backslash is a literal byte, not a Windows separator.
    if not isinstance(path, str):
        raise ValueError("invalid native path")
    clean = path.strip("/")
    if not clean or len(clean) > 500 or "\0" in clean or any(part in {"", ".", ".."} for part in clean.split("/")):
        raise ValueError("invalid native path")
    clean.encode("utf-8", "surrogateescape")
    return clean


def apply_byte_paths(arguments, byte_paths):
    """Lossless alternatives: scalar field, paths/N, or files/N/path.

    The corresponding text slot must be empty, avoiding two competing path
    identities. This changes input normalization, not authorization or scope.
    """
    args = dict(arguments)
    if "paths" in args and args["paths"] is not None:
        args["paths"] = list(args["paths"])
    if "files" in args:
        args["files"] = [dict(item) for item in args["files"]]
    for field, encoded in (byte_paths or {}).items():
        if not isinstance(encoded, str) or len(encoded) > 2700:
            raise ValueError("invalid native byte path")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid native byte path") from exc
        if (not raw or base64.b64encode(raw).decode("ascii") != encoded or raw.startswith(b"/")
                or any(part in {b"", b".", b".."} for part in raw.split(b"/")) or b"\0" in raw):
            raise ValueError("invalid native byte path")
        container, key = args, field
        parts = field.split("/")
        if len(parts) in {2, 3} and parts[0] in {"paths", "files"} and parts[1].isdigit():
            index = int(parts[1])
            items = args.get(parts[0])
            if not isinstance(items, list) or not 0 <= index < len(items) or str(index) != parts[1]:
                raise ValueError("invalid native byte path slot")
            if parts[0] == "paths" and len(parts) == 2:
                container, key = items, index
            elif parts[0] == "files" and parts[2:] == ["path"]:
                container, key = items[index], "path"
            else:
                raise ValueError("invalid native byte path slot")
        elif field not in {"path", "old_path", "new_path"} or field not in args:
            raise ValueError("invalid native byte path slot")
        if container[key] != "":
            raise ValueError("provide a text path or native byte path, not both")
        container[key] = raw.decode("utf-8", "surrogateescape")
    return args


def response_paths(response):
    """Display strings never replace the separate byte identities."""
    result = dict(response)
    for field in ("path", "old_path", "new_path"):
        if field in result:
            raw = result[field].encode("utf-8", "surrogateescape")
            result[field] = raw.decode("utf-8", "backslashreplace")
            result[field+"_bytes_b64"] = base64.b64encode(raw).decode("ascii")
    if "paths" in result:
        raw = [path.encode("utf-8", "surrogateescape") for path in result["paths"]]
        result["paths"] = [path.decode("utf-8", "backslashreplace") for path in raw]
        result["paths_bytes_b64"] = [base64.b64encode(path).decode("ascii") for path in raw]
    return result


def _entry(store, root, path):
    parts = path.split("/")
    for index, part in enumerate(parts):
        entry = next((e for e in read_tree_entries(store, root) if e.name == part), None)
        if entry is None:
            return None
        if index == len(parts)-1:
            return entry
        if not entry.is_dir:
            raise NotADirectoryError(path)
        root = entry.sha1_hex
    return None


def compile_native_command(commands, operation, arguments):
    """Return a complete normalized digest, pinned-tree splice and response paths.

    Preconditions are evaluated on the very tree used for construction, never
    through a separate latest-root lookup. The ref transaction guards that base.
    """
    args = dict(arguments)
    allowed = {
        "write": {"path", "content", "node_type"},
        "bulk_write": {"files"},
        "mkdir": {"path", "parents"},
        "move": {"old_path", "new_path", "no_clobber", "target_directory", "no_target_directory"},
        "copy": {"old_path", "new_path", "no_clobber", "target_directory", "no_target_directory", "recursive"},
        "remove": {"path", "paths", "force", "recursive"},
        "touch": {"path", "paths"},
    }
    if operation not in allowed or args.keys() - allowed[operation] - {"message", "base_commit_id"}:
        raise ValueError("unsupported native Product command semantics")
    # This v1 field/default set is a durable retry contract. New semantics
    # need a new input version, not changed defaults for outstanding requests.
    defaults = {
        "write": {"node_type": "json"}, "bulk_write": {}, "mkdir": {"parents": False},
        "move": {"no_clobber": False, "target_directory": False, "no_target_directory": False},
        "copy": {"no_clobber": False, "target_directory": False, "no_target_directory": False, "recursive": False},
        "remove": {"path": "", "paths": None, "force": False, "recursive": False},
        "touch": {"path": "", "paths": None},
    }
    args = {"message": "", "base_commit_id": None, **defaults[operation], **args}
    for flag in ("parents", "no_clobber", "target_directory", "no_target_directory", "recursive", "force"):
        if flag in args and type(args[flag]) is not bool:
            raise ValueError("invalid native command flag")
    response = {}
    if operation == "write":
        item = commands.serialize_content(args["path"], args["content"], args.get("node_type", "json"), path_validator=native_path)
        native_path(item.path)  # validate the extension-normalized result too
        args.update(path=item.path, content=item.content)
        response = {"path": item.path, "merged": False, "conflicts": 0}
        def splice(store, root):
            return splice_batch(store, root, [("put", item.path, item.content)])
    elif operation == "bulk_write":
        files = [commands.serialize_content(item["path"], item["content"], item.get("node_type", "json"), path_validator=native_path)
                 for item in args["files"]]
        for item in files:
            native_path(item.path)
        # Match the established last item at a duplicated path contract.
        unique = {item.path: item.content for item in files}
        args["files"] = list(unique.items())
        response = {"total": len(unique), "merged": False}
        def splice(store, root):
            return splice_batch(store, root, [("put", path, content) for path, content in unique.items()])
    elif operation == "mkdir":
        path = native_path(args["path"])
        args["path"] = path
        response = {"path": path}
        def splice(store, root):
            parent = path.rpartition("/")[0]
            if parent and not args.get("parents"):
                existing = _entry(store, root, parent)
                if existing is None:
                    raise FileNotFoundError(parent)
                if not existing.is_dir:
                    raise NotADirectoryError(parent)
            return splice_mkdir(store, root, path)
    elif operation in {"move", "copy"}:
        old, new = native_path(args["old_path"]), native_path(args["new_path"])
        args.update(old_path=old, new_path=new)
        response = {"old_path": old, "new_path": new}
        def splice(store, root):
            source, destination = _entry(store, root, old), _entry(store, root, new)
            if source is None:
                raise FileNotFoundError(old)
            if args.get("target_directory") and args.get("no_target_directory"):
                raise ValueError("conflicting destination flags")
            target = new
            if args.get("target_directory"):
                if destination is None or not destination.is_dir:
                    raise NotADirectoryError(new)
                target += "/" + old.rsplit("/", 1)[-1]
                destination = _entry(store, root, target)
            if args.get("no_clobber") and destination is not None:
                raise FileExistsError("destination exists (no_clobber)")
            if operation == "copy" and source.is_dir and not args.get("recursive"):
                raise IsADirectoryError(old)
            action = splice_move if operation == "move" else splice_copy
            return action(store, root, old, target)
    elif operation in {"remove", "touch"}:
        paths = [native_path(path) for path in (args.get("paths") or [args.get("path", "")]) if path]
        if not paths:
            raise ValueError("paths is empty")
        args["paths"] = paths
        response = {"paths": paths} if arguments.get("paths") else {"path": paths[0]}
        def splice(store, root):
            if operation == "remove":
                for path in paths:
                    entry = _entry(store, root, path)
                    if entry is None and not args.get("force"):
                        raise FileNotFoundError(path)
                    if entry is not None and entry.is_dir and not args.get("recursive"):
                        raise IsADirectoryError(path)
                return splice_remove(store, root, paths)
            current, changes = root, []
            for path in paths:
                current, delta = splice_touch(store, current, path)
                changes.extend(delta)
            return current, changes
    else:
        raise ValueError("unsupported native Product command")
    encoded = json.dumps(_identity({"operation": operation, "arguments": args}),
                         sort_keys=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(encoded).hexdigest(), splice, response
