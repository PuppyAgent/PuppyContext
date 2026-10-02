"""PuppyOne-owned Git object format helpers.

These helpers intentionally model Git's loose-object, tree, and commit
formats directly. They are small enough to own locally, and keeping them in
PuppyOne lets the version engine own Git-kernel primitives locally.
"""

from __future__ import annotations

import hashlib
import zlib
from collections.abc import Iterable
from typing import NamedTuple


def _frame(obj_type: str, content: bytes) -> bytes:
    return f"{obj_type} {len(content)}".encode("ascii") + b"\x00" + content


def hash_object(obj_type: str, content: bytes) -> str:
    """Return the SHA-1 hex id for a Git object body."""

    return hashlib.sha1(_frame(obj_type, content)).hexdigest()


def encode_object(obj_type: str, content: bytes) -> tuple[str, bytes]:
    """Return ``(sha1_hex, zlib_compressed_loose_bytes)``."""

    framed = _frame(obj_type, content)
    return hashlib.sha1(framed).hexdigest(), zlib.compress(framed)


EMPTY_TREE_CONTENT = b""
EMPTY_TREE_SHA1, EMPTY_TREE_LOOSE_BYTES = encode_object("tree", EMPTY_TREE_CONTENT)


def decode_object(loose_bytes: bytes) -> tuple[str, bytes]:
    """Decode Git loose-object bytes into ``(type, content)``."""

    framed = zlib.decompress(loose_bytes)
    nul = framed.index(b"\x00")
    header = framed[:nul].decode("ascii")
    obj_type, size_text = header.split(" ", 1)
    content = framed[nul + 1 :]
    size = int(size_text)
    if len(content) != size:
        raise ValueError(
            f"git object size mismatch: header says {size}, got {len(content)}"
        )
    return obj_type, content


MODE_FILE = b"100644"
MODE_EXECUTABLE = b"100755"
MODE_SYMLINK = b"120000"
MODE_GITLINK = b"160000"
MODE_DIR = b"40000"

# Every blob mode Git can put in a tree, plus the directory mode. PuppyOne
# owns the Git tree format, so it must round-trip executables (100755),
# symlinks (120000), and submodule gitlinks (160000) — not just regular
# files — or it corrupts/loses content the client pushed.
_BLOB_MODES = (MODE_FILE, MODE_EXECUTABLE, MODE_SYMLINK, MODE_GITLINK)
_ALLOWED_TREE_MODES = frozenset(_BLOB_MODES + (MODE_DIR,))


class TreeEntry(NamedTuple):
    name: str
    mode: bytes
    sha1_hex: str

    @property
    def is_dir(self) -> bool:
        return self.mode == MODE_DIR

    @property
    def is_gitlink(self) -> bool:
        """A submodule OID belongs to another repository, not this tree closure."""
        return self.mode == MODE_GITLINK

    @property
    def raw_name(self) -> bytes:
        """Lossless Git bytes; surrogate escapes are internal, not display text."""
        return self.name.encode("utf-8", "surrogateescape")


def _validate_sha1_hex(sha1_hex: str) -> None:
    if len(sha1_hex) != 40:
        raise ValueError(
            f"git tree entry object id must be 40 hex characters, "
            f"got {len(sha1_hex)}",
        )
    if any(char not in "0123456789abcdefABCDEF" for char in sha1_hex):
        raise ValueError("git tree entry object id must be hexadecimal")


def _validate_tree_name(raw_name: bytes) -> None:
    if raw_name in {b"", b".", b".."} or b"/" in raw_name or b"\x00" in raw_name:
        raise ValueError("invalid git tree entry name")


def encode_tree(entries: Iterable[TreeEntry]) -> bytes:
    """Encode Git tree entries in Git's tree binary format."""

    sorted_entries = sorted(
        entries,
        key=lambda entry: entry.raw_name + (b"/" if entry.is_dir else b""),
    )
    out = bytearray()
    seen_names: set[bytes] = set()
    for entry in sorted_entries:
        if entry.mode not in _ALLOWED_TREE_MODES:
            raise ValueError(f"unsupported git tree mode: {entry.mode!r}")
        _validate_sha1_hex(entry.sha1_hex)
        raw_name = entry.raw_name
        _validate_tree_name(raw_name)
        if raw_name in seen_names:
            raise ValueError("duplicate git tree entry name")
        seen_names.add(raw_name)
        out += (
            entry.mode
            + b" "
            + raw_name
            + b"\x00"
            + bytes.fromhex(entry.sha1_hex)
        )
    return bytes(out)


def decode_tree(content: bytes) -> list[TreeEntry]:
    """Decode a Git tree body into entries."""

    entries: list[TreeEntry] = []
    seen_names: set[bytes] = set()
    index = 0
    while index < len(content):
        space = content.index(b" ", index)
        mode = content[index:space]
        if mode not in _ALLOWED_TREE_MODES:
            raise ValueError(f"unsupported git tree mode: {mode!r}")
        nul = content.index(b"\x00", space)
        raw_name = content[space + 1 : nul]
        _validate_tree_name(raw_name)
        if raw_name in seen_names:
            raise ValueError("duplicate git tree entry name")
        seen_names.add(raw_name)
        name = raw_name.decode("utf-8", "surrogateescape")
        if nul + 21 > len(content):
            raise ValueError("truncated git tree entry object id")
        sha1_hex = content[nul + 1 : nul + 21].hex()
        entries.append(TreeEntry(name=name, mode=mode, sha1_hex=sha1_hex))
        index = nul + 21
    return entries


def encode_commit(
    tree_sha1: str,
    parent_sha1: str | None,
    author: str,
    author_time: str,
    committer: str,
    committer_time: str,
    message: str,
) -> bytes:
    """Encode a Git commit object body."""

    parts = [f"tree {tree_sha1}"]
    if parent_sha1:
        parts.append(f"parent {parent_sha1}")
    parts.append(f"author {author} {author_time}")
    parts.append(f"committer {committer} {committer_time}")
    parts.append("")
    parts.append(message.rstrip("\n") + "\n")
    return "\n".join(parts).encode("utf-8")


def decode_commit(content: bytes) -> dict:
    """Decode metadata without rejecting non-UTF8 identities/messages.

    Raw object bytes remain authoritative; this projection is never used to
    reconstruct signed commits. Consumers rendering text must escape it.
    """

    text = content.decode("utf-8", "surrogateescape")
    head, _, message = text.partition("\n\n")
    info: dict = {"parents": [], "message": message.rstrip("\n")}
    for line in head.split("\n"):
        if not line:
            continue
        key, _, value = line.partition(" ")
        if key == "tree":
            info["tree"] = value
        elif key == "parent":
            info["parents"].append(value)
        elif key == "author":
            info["author"] = value
        elif key == "committer":
            info["committer"] = value
    return info


def decode_tag(content: bytes) -> dict:
    """Decode an annotated Git tag body into its target metadata.

    Lightweight tags point directly at a commit and have no tag object. This
    helper covers the annotated form whose first-class object can itself point
    at a commit, tree, blob, or another annotated tag.
    """

    text = content.decode("utf-8", "surrogateescape")
    head, _, message = text.partition("\n\n")
    info: dict = {"message": message.rstrip("\n")}
    for line in head.split("\n"):
        if not line:
            continue
        key, _, value = line.partition(" ")
        if key in {"object", "type", "tag", "tagger"}:
            info[key] = value
    if not info.get("object") or not info.get("type"):
        raise ValueError("annotated tag is missing object/type headers")
    _validate_sha1_hex(info["object"])
    return info


def split_author_line(line: str) -> tuple[str, str]:
    """Split ``Name <email> <unix_ts> <tz>`` into identity and time."""

    parts = line.rsplit(" ", 2)
    if len(parts) >= 3:
        return parts[0], f"{parts[1]} {parts[2]}"
    return line, "0 +0000"
