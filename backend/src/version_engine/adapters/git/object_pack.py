"""Bounded Git pack codec. Scratch contains incoming objects only, never a repo.

Dulwich is used as a format/delta codec, not a repository or ref authority.
Object bytes are preserved verbatim. Outgoing packs are pulled under backpressure.
"""

import hashlib
import struct
import tempfile
import zlib

from dulwich.errors import ApplyDeltaError, ObjectFormatException
from dulwich.index import validate_path_element_hfs, validate_path_element_ntfs
from dulwich.object_format import get_object_format
from dulwich.objects import ShaFile
from dulwich.pack import apply_delta

from src.version_engine.write_engine.git_object_format import decode_tree, hash_object
from src.version_engine.write_engine.git_object_graph import object_edges

from .execution import MAX_GRAPH_BYTES, MAX_OBJECT_BYTES, MAX_OBJECTS, MAX_PACK_BYTES, checkpoint

KINDS = {1: "commit", 2: "tree", 3: "blob", 4: "tag"}
TYPES = {kind: number for number, kind in KINDS.items()}
MAX_DELTA_INSTRUCTIONS = 1_000_000


def exact(handle, size):
    value = handle.read(size)
    if len(value) != size:
        raise ValueError("truncated Git pack")
    return value


def delta_size(delta, offset):
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(delta):
            raise ValueError("truncated delta header")
        byte = delta[offset]
        offset += 1
        value |= (byte & 127) << shift
        if not byte & 128:
            return value, offset
    raise ValueError("oversized delta header")


def bounded_delta(base, delta):
    source, pos = delta_size(delta, 0)
    size, pos = delta_size(delta, pos)
    if source != len(base) or size > MAX_OBJECT_BYTES:
        raise ValueError("delta object size exceeds budget or mismatches base")
    # Validate cumulative copy/literal output before the codec allocates it.
    # A malicious delta can repeat large copies despite a tiny declared size.
    produced, instructions = 0, 0
    while pos < len(delta):
        instructions += 1
        if instructions % 4096 == 0:
            checkpoint()
        if instructions > MAX_DELTA_INSTRUCTIONS:
            raise ValueError("delta instruction budget exceeded")
        opcode = delta[pos]
        pos += 1
        if opcode & 128:
            offset, length = 0, 0
            for bit in range(7):
                if opcode & (1 << bit):
                    if pos >= len(delta):
                        raise ValueError("truncated delta instruction")
                    if bit < 4:
                        offset |= delta[pos] << (8 * bit)
                    else:
                        length |= delta[pos] << (8 * (bit - 4))
                    pos += 1
            length = length or 65536
            if offset + length > source:
                raise ValueError("delta copy outside base")
        elif opcode:
            length = opcode
            pos += length
            if pos > len(delta):
                raise ValueError("truncated delta literal")
        else:
            raise ValueError("invalid delta opcode")
        produced += length
        if produced > size:
            raise ValueError("delta expansion exceeds declared size")
    if produced != size:
        raise ValueError("delta result size mismatch")
    try:
        return b"".join(apply_delta(base, delta))
    except ApplyDeltaError as exc:
        raise ValueError("invalid Git delta") from exc


class IncomingPack:
    """One bounded private spool plus metadata; no Git directories or indexes."""

    def __init__(self, reader):
        self.reader = reader
        self.format = reader.format
        self.file = tempfile.TemporaryFile()  # noqa: SIM115 -- owned by this context manager
        self.objects = {}
        self.bytes = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.file.close()

    def save(self, kind, body):
        self.bytes += len(body)
        if self.bytes > MAX_GRAPH_BYTES:
            raise ValueError("incoming expanded pack budget exceeded")
        offset = self.file.seek(0, 2)
        self.file.write(body)
        return kind, offset, len(body)

    def body(self, record):
        kind, offset, size = record
        self.file.seek(offset)
        return kind, exact(self.file, size)

    def add(self, kind, body):
        self.reader.snapshot.check_live()
        if len(body) > MAX_OBJECT_BYTES:
            raise ValueError("Git single-object budget exceeded")
        try:
            ShaFile.from_raw_string(
                TYPES[kind], body, object_format=get_object_format(self.format)
            ).check()
        except (ObjectFormatException, ValueError, IndexError, AssertionError) as exc:
            raise ValueError("invalid incoming Git object") from exc
        if kind == "tree":
            for entry in decode_tree(body, object_format=self.format):
                name = entry.raw_name
                ntfs_base = name.partition(b":")[0].rstrip(b". ").lower()
                if not validate_path_element_ntfs(name) or ntfs_base in {b".git", b"git~1"}:
                    raise ValueError("tree contains a protected Git path")
                try:
                    name.decode("utf-8")
                except UnicodeDecodeError:
                    pass  # arbitrary non-UTF8 Git names remain valid
                else:
                    if not validate_path_element_hfs(name):
                        raise ValueError("tree contains a protected Git path")
                if name.lower() == b".gitmodules" and entry.mode == b"120000":
                    raise ValueError("gitmodules cannot be a symlink")
        object_edges(kind, body, object_format=self.format)
        oid = hash_object(kind, body, object_format=self.format)
        if oid not in self.objects:
            self.objects[oid] = self.save(kind, body)
        return oid

    def get(self, oid):
        if oid in self.objects:
            return self.body(self.objects[oid])
        self.reader.authorize([oid])
        return self.reader.get(oid)

    def read(self, handle):
        start = handle.tell()
        handle.seek(0, 2)
        end = handle.tell()
        handle.seek(start)
        if end == start:
            return  # deletion-only receive
        if end - start > MAX_PACK_BYTES:
            raise ValueError("incoming pack byte budget exceeded")
        header = exact(handle, 12)
        magic, version, count = struct.unpack("!4sII", header)
        if magic != b"PACK" or version not in (2, 3) or count > MAX_OBJECTS:
            raise ValueError("invalid pack header or object count")
        offsets, pending = {}, []
        expanded = 0
        for _ in range(count):
            checkpoint()
            position = handle.tell() - start
            byte = exact(handle, 1)[0]
            kind, size, shift = (byte >> 4) & 7, byte & 15, 4
            while byte & 128:
                if shift > 63:
                    raise ValueError("oversized pack object header")
                byte = exact(handle, 1)[0]
                size |= (byte & 127) << shift
                shift += 7
            if size > MAX_OBJECT_BYTES:
                raise ValueError("Git single-object budget exceeded")
            base = None
            if kind == 6:
                byte = exact(handle, 1)[0]
                distance = byte & 127
                while byte & 128:
                    if distance > MAX_PACK_BYTES:
                        raise ValueError("invalid delta offset")
                    byte = exact(handle, 1)[0]
                    distance = ((distance + 1) << 7) | (byte & 127)
                base = position - distance
                if base not in offsets:
                    raise ValueError("delta base is not an earlier object")
            elif kind == 7:
                base = exact(handle, 20 if self.format == "sha1" else 32).hex()
            elif kind not in KINDS:
                raise ValueError("invalid pack object type")
            decoder, body = zlib.decompressobj(), bytearray()
            while not decoder.eof:
                checkpoint()
                data = handle.read(65536)
                if not data:
                    raise ValueError("truncated compressed pack object")
                try:
                    body.extend(decoder.decompress(data, size + 1 - len(body)))
                except zlib.error as exc:
                    raise ValueError("invalid compressed pack object") from exc
                if len(body) > size or decoder.unconsumed_tail:
                    raise ValueError("pack expansion exceeds declared size")
                if decoder.eof:
                    handle.seek(-len(decoder.unused_data), 1)
            if len(body) != size:
                raise ValueError("pack object size mismatch")
            expanded += size
            if expanded > MAX_GRAPH_BYTES:
                raise ValueError("incoming pack expansion budget exceeded")
            if kind in KINDS:
                oid = self.add(KINDS[kind], bytes(body))
                offsets[position] = oid
            else:
                offsets[position] = None
                pending.append((position, base, self.save("delta", body)))
        checksum_at = handle.tell()
        expected = exact(handle, 20 if self.format == "sha1" else 32)
        if handle.read(1):
            raise ValueError("unexpected bytes after Git pack")
        digest = hashlib.new(self.format)
        handle.seek(start)
        while handle.tell() < checksum_at:
            checkpoint()
            digest.update(exact(handle, min(65536, checksum_at - handle.tell())))
        if digest.digest() != expected:
            raise ValueError("Git pack checksum mismatch")
        # REF_DELTA may precede its base. Resolve internal bases before looking
        # for external thin bases, which must be in a published closure.
        external = False
        while pending:
            deferred, progressed = [], False
            for position, base, record in pending:
                checkpoint()
                oid = offsets.get(base) if isinstance(base, int) else base
                if oid is None or (oid not in self.objects and not external):
                    deferred.append((position, base, record))
                    continue
                try:
                    kind, original = self.get(oid)
                except PermissionError:
                    # This may be a forward REF_DELTA whose own thin base is
                    # resolved later in this pass. Never probe S3 directly.
                    deferred.append((position, base, record))
                    continue
                _, delta = self.body(record)
                offsets[position] = self.add(kind, bounded_delta(original, delta))
                progressed = True
            if not progressed and external:
                raise ValueError("unresolved delta cycle or missing base")
            external = not progressed
            pending = deferred


def pack_chunks(reader, oids):
    """Generate a valid non-delta pack without a repo or an output spool."""
    digest = hashlib.new(reader.format)
    header = struct.pack("!4sII", b"PACK", 2, len(oids))
    digest.update(header)
    yield header
    for oid in oids:
        checkpoint()
        kind, body = reader.get(oid)
        size = len(body)
        byte, size = (TYPES[kind] << 4) | (size & 15), size >> 4
        header = bytearray()
        while size:
            header.append(byte | 128)
            byte, size = size & 127, size >> 7
        header.append(byte)
        digest.update(header)
        yield bytes(header)
        compressor = zlib.compressobj()
        for offset in range(0, len(body), 65536):
            checkpoint()
            chunk = compressor.compress(body[offset : offset + 65536])
            if chunk:
                digest.update(chunk)
                yield chunk
        tail = compressor.flush()
        digest.update(tail)
        yield tail
    yield digest.digest()
