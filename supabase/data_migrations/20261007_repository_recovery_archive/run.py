"""Immutable operator artifact. Default is read-only planning, never activation.

No imports from application code; dependencies are supplied by the locked runtime.
Use puppyone-db run after deployment and writer/GC drain. Every source byte is verified in a private archive before its old key is
removed. Historical metadata is retained. Database owner access is required.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import zlib
from urllib.parse import parse_qs, unquote, urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from dulwich.objects import ShaFile

ARTIFACT = "20261007_repository_recovery_archive"
EMPTY = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
MAX_OBJECT = 64 * 1024**2
MAX_GRAPH = 256 * 1024**2
KINDS = {"commit": 1, "tree": 2, "blob": 3, "tag": 4}


def missing_object(error):
    """Supabase can return an empty S3 error envelope with HTTP 404."""
    code = error.response.get("Error", {}).get("Code")
    return code in {"NoSuchKey", "404", "NotFound"} or (
        code in (None, "")
        and error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404
    )


def quoted(value):
    if value is None:
        return "NULL"
    if isinstance(value, (dict, list)):
        value = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return "'" + str(value).replace("'", "''") + "'"


class Database:
    def __init__(self, url):
        parts = urlsplit(url)
        if (
            parts.scheme not in {"postgres", "postgresql"}
            or not parts.hostname
            or not parts.path.strip("/")
        ):
            raise ValueError("explicit PostgreSQL URL required")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
        self.env.update(
            PGHOST=parts.hostname,
            PGPORT=str(parts.port or 5432),
            PGDATABASE=unquote(parts.path[1:]),
            PGUSER=unquote(parts.username or "postgres"),
            PGPASSWORD=unquote(parts.password or ""),
            PGCONNECT_TIMEOUT="10",
        )
        query = parse_qs(parts.query)
        if "sslmode" in query:
            self.env["PGSSLMODE"] = query["sslmode"][-1]
        self.prefix = (
            "SET ROLE postgres;" if self.env["PGUSER"].startswith("cli_login_") else ""
        )

    def sql(self, sql):
        done = subprocess.run(
            ["psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1"],
            input=self.prefix
            + "SET statement_timeout='120s';SET lock_timeout='10s';"
            + sql,
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
            timeout=135,
        )
        if done.returncode:
            # Do not echo SQL, source data, connection strings or credentials.
            code = next(
                (
                    line.split("ERROR:", 1)[1].strip()
                    for line in done.stderr.splitlines()
                    if "ERROR:" in line
                ),
                "database operation failed",
            )
            raise RuntimeError(code)
        text = done.stdout.strip()
        return json.loads(text) if text else None

    def rows(self, sql):
        return self.sql(
            "SELECT coalesce(jsonb_agg(to_jsonb(r)),'[]'::jsonb) FROM (" + sql + ") r;"
        )


def loose(kind, body):
    raw = kind.encode() + b" " + str(len(body)).encode() + b"\0" + body
    return hashlib.sha1(raw).hexdigest(), zlib.compress(raw)


def decode(data, oid):
    decoder = zlib.decompressobj()
    raw = decoder.decompress(data, MAX_OBJECT + 128)
    if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise ValueError("invalid or excessive compressed Git object")
    header, separator, body = raw.partition(b"\0")
    kind, space, size = header.partition(b" ")
    if (
        not separator
        or not space
        or kind.decode() not in KINDS
        or size != str(len(body)).encode()
    ):
        raise ValueError("invalid Git object header")
    if len(body) > MAX_OBJECT or hashlib.sha1(raw).hexdigest() != oid:
        raise ValueError("Git object size/hash mismatch")
    parsed = ShaFile.from_raw_string(KINDS[kind.decode()], body)
    parsed.check()
    return kind.decode(), body


def tree_entries(body):
    result, pos, names = [], 0, set()
    while pos < len(body):
        end = body.index(b"\0", pos)
        mode, name = body[pos:end].split(b" ", 1)
        if mode not in (b"40000", b"100644", b"100755", b"120000", b"160000"):
            raise ValueError("unsupported Git tree mode")
        if not name or b"/" in name or name in (b".", b"..", b".git") or name in names:
            raise ValueError("invalid or duplicate Git tree name")
        names.add(name)
        oid = body[end + 1 : end + 21]
        if len(oid) != 20 or oid == b"\0" * 20:
            raise ValueError("invalid tree edge")
        result.append((mode, name, oid.hex()))
        pos = end + 21
    return result


def edges(kind, body):
    if kind == "blob":
        return []
    if kind == "tree":
        return [
            (oid, "tree" if mode == b"40000" else "blob")
            for mode, _, oid in tree_entries(body)
            if mode != b"160000"
        ]
    headers = {}
    for line in body.partition(b"\n\n")[0].split(b"\n"):
        if line.startswith(b" "):
            continue
        key, _, value = line.partition(b" ")
        headers.setdefault(key, []).append(value)

    def one(key):
        values = headers.get(key, [])
        if len(values) != 1:
            raise ValueError("invalid structural Git header")
        return values[0].decode("ascii")

    if kind == "commit":
        result = [
            (one(b"tree"), "tree"),
            *[(p.decode("ascii"), "commit") for p in headers.get(b"parent", [])],
        ]
    else:
        result = [(one(b"object"), one(b"type"))]
    if any(
        not re.fullmatch(r"[0-9a-f]{40}", oid) or typ not in KINDS
        for oid, typ in result
    ):
        raise ValueError("invalid typed Git edge")
    return result


def unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON entry")
        result[key] = value
    return result


class ObjectConverter:
    def __init__(self, db, s3, bucket, project, *, apply=False):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}", project):
            raise ValueError("invalid project storage segment")
        self.db, self.s3, self.bucket, self.project, self.apply = (
            db,
            s3,
            bucket,
            project,
            apply,
        )
        self.done, self.visiting, self.objects = {}, set(), {}
        self.bytes = 0
        self.location_cache = None
        self.pending = []
        self.source_bytes = 0

    def get(self, key, *, byte_range=None, limit=MAX_OBJECT + 1024**2):
        args = {"Bucket": self.bucket, "Key": key}
        if byte_range:
            args["Range"] = byte_range
        try:
            response = self.s3.get_object(**args)
        except ClientError as exc:
            if missing_object(exc):
                return None
            raise
        stream = response["Body"]
        try:
            if response["ContentLength"] > limit:
                raise ValueError("object exceeds migration limit")
            data = stream.read(limit + 1)
            if len(data) != response["ContentLength"] or len(data) > limit:
                raise ValueError("incomplete or oversized S3 response")
            return data
        finally:
            stream.close()

    def key(self, oid, namespace="version"):
        return f"{namespace}/{self.project}/objects/{oid[:2]}/{oid[2:]}"

    def source(self, oid):
        if oid == EMPTY:
            return loose("tree", b"")[1]
        for namespace in ("version", "mut"):
            raw = self.get(self.key(oid, namespace))
            if raw is not None:
                return raw
        if self.location_cache is None:
            rows = self.db.rows(
                "SELECT object_id,pack_key,offset_bytes,size_bytes FROM public.version_object_locations WHERE project_id="
                + quoted(self.project)
                + " ORDER BY object_id LIMIT 100001"
            )
            if len(rows) > 100000:
                raise ValueError("source location inventory exceeds migration budget")
            self.location_cache = {row["object_id"]: row for row in rows}
        loc = self.location_cache.get(oid)
        if loc is None:
            raise FileNotFoundError("required source object unavailable: " + oid)
        key, offset, size = loc["pack_key"], loc["offset_bytes"], loc["size_bytes"]
        if (
            type(offset) is not int
            or type(size) is not int
            or offset < 0
            or not 0 < size <= MAX_OBJECT + 1024**2
        ):
            raise ValueError("invalid source location range")
        prefixes = [f"{ns}/{self.project}/object-bundles/" for ns in ("version", "mut")]
        actual = key.removeprefix("chunked:")
        if (
            not any(actual.startswith(p) for p in prefixes)
            or "/../" in actual
            or "\\" in actual
        ):
            raise ValueError("cross-project source location")
        if key.startswith("chunked:"):
            raw = self.get(actual, limit=4 * 1024**2)
            if raw is None:
                raise FileNotFoundError("chunk manifest missing")
            manifest = json.loads(raw, object_pairs_hook=unique_json)
            if (
                manifest.get("version") != 1
                or manifest.get("object_id") != oid
                or manifest.get("size_bytes") != size
                or offset != 0
            ):
                raise ValueError("chunk manifest identity mismatch")
            prefix = (
                next(p for p in prefixes if actual.startswith(p))
                + f"chunked/{oid[:2]}/{oid}"
            )
            immutable = manifest.get("placement") == "content-addressed-v1"
            expected = prefix + (
                "/manifest-" + hashlib.sha256(raw).hexdigest() + ".json"
                if immutable
                else ".json"
            )
            if actual != expected or ("placement" in manifest and not immutable):
                raise ValueError("invalid chunk manifest placement")
            result = bytearray()
            for index, chunk in enumerate(manifest.get("chunks", []), 1):
                part_key = chunk["key"]
                if (
                    chunk["offset_bytes"] != len(result)
                    or type(chunk["size_bytes"]) is not int
                    or chunk["size_bytes"] <= 0
                ):
                    raise ValueError("invalid chunk coverage")
                valid = (
                    re.fullmatch(
                        re.escape(prefix + "/part-") + r"[0-9a-f]{64}", part_key
                    )
                    if immutable
                    else part_key == prefix + f"/part-{index:06d}"
                )
                if not valid or len(result) + chunk["size_bytes"] > size:
                    raise ValueError("invalid chunk namespace or size")
                part = self.get(part_key, limit=chunk["size_bytes"])
                if part is None or len(part) != chunk["size_bytes"]:
                    raise ValueError("chunk missing/truncated")
                if immutable and not part_key.endswith(
                    hashlib.sha256(part).hexdigest()
                ):
                    raise ValueError("chunk digest mismatch")
                result.extend(part)
            if len(result) != size:
                raise ValueError("incomplete chunk manifest")
            return bytes(result)
        raw = self.get(
            actual, byte_range=f"bytes={offset}-{offset + size - 1}", limit=size
        )
        if raw is None or len(raw) != size:
            raise ValueError("bundle range missing/truncated")
        return raw

    def store(self, source_id, source_kind, kind, body, source_raw):
        oid, encoded = loose(kind, body)
        if oid not in self.objects:
            self.bytes += len(body)
            if (
                len(body) > MAX_OBJECT
                or self.bytes > MAX_GRAPH
                or len(self.objects) >= 100000
            ):
                raise ValueError("migration graph exceeds native capacity")
            self.objects[oid] = (kind, len(body))
        # Canonical objects may have equivalent deflate encodings. Progress
        # binds their uncompressed identity, not a compressor implementation.
        fingerprint = (
            kind.encode() + b" " + str(len(body)).encode() + b"\0" + body
            if len(source_id) == 40 and source_kind != "snapshot"
            else source_raw
        )
        digest = hashlib.sha256(fingerprint).hexdigest()
        if self.apply:
            # A loose canonical copy makes old bundle/chunk layouts irrelevant.
            # Never overwrite different/corrupt destination content silently.
            existing = self.get(self.key(oid))
            if existing is None:
                self.s3.put_object(
                    Bucket=self.bucket,
                    Key=self.key(oid),
                    Body=encoded,
                    ContentType="application/octet-stream",
                )
                existing = self.get(self.key(oid))
            if existing is None or decode(existing, oid) != (kind, body):
                raise ValueError("canonical destination verification failed")
            self.pending.append(
                {
                    "project_id": self.project,
                    "source_id": source_id,
                    "source_kind": source_kind,
                    "object_id": oid,
                    "object_kind": kind,
                    "body_bytes": len(body),
                    "source_sha256": digest,
                }
            )
            if len(self.pending) >= 200:
                self.flush()
        return oid

    def flush(self):
        if not self.pending:
            return
        # Bounded checkpoint batches avoid one remote DB connection per object.
        expected = (
            "SELECT * FROM jsonb_populate_recordset(NULL::public.version_repository_archive_git_objects,"
            + quoted(self.pending)
            + "::jsonb)"
        )
        self.db.sql(
            "BEGIN; INSERT INTO public.version_repository_archive_git_objects "
            + expected
            + " ON CONFLICT(project_id,source_id,source_kind) DO NOTHING;"
            + " DO $checkpoint$ BEGIN IF EXISTS(("
            + expected
            + ") EXCEPT SELECT * FROM public.version_repository_archive_git_objects)"
            + " THEN RAISE EXCEPTION 'migration_source_changed_across_attempts'; END IF; END $checkpoint$; COMMIT;"
        )
        self.pending.clear()

    def convert(self, oid, expected, depth=0):
        key = (oid, expected)
        if key in self.done:
            return self.done[key]
        if key in self.visiting or depth > 200:
            raise ValueError("cyclic/excessively deep object graph")
        if not re.fullmatch(r"[0-9a-f]{16}|[0-9a-f]{40}", oid):
            raise ValueError("unsupported source object identifier")
        self.visiting.add(key)
        raw = self.source(oid)
        if len(oid) == 40:
            kind, body = decode(raw, oid)
            self.source_bytes += len(body)
            if self.source_bytes > MAX_GRAPH:
                raise ValueError("source graph exceeds migration budget")
            if expected and kind != expected:
                raise ValueError("Git edge type mismatch")
            for child, child_kind in edges(kind, body):
                if self.convert(child, child_kind, depth + 1) != child:
                    raise ValueError("canonical object contains noncanonical edge")
        else:
            # The 16-character identifier is an opaque historical lookup key.
            # Record a full source SHA-256; do not assert an unknown old hash
            # algorithm or interpret malformed 40-character Git data as raw.
            kind = expected
            self.source_bytes += len(raw)
            if self.source_bytes > MAX_GRAPH:
                raise ValueError("source graph exceeds migration budget")
            if kind == "blob":
                body = raw
            elif kind == "tree":
                entries = json.loads(raw, object_pairs_hook=unique_json)
                if not isinstance(entries, dict):
                    raise ValueError("legacy tree must be an object")
                converted = []
                for name, value in entries.items():
                    if (
                        not name
                        or name in (".", "..", ".git")
                        or "/" in name
                        or "\0" in name
                    ):
                        raise ValueError("invalid legacy tree name")
                    if isinstance(value, list) and len(value) == 2:
                        typ, child = value
                    elif isinstance(value, dict):
                        typ = value.get("type") or value.get("kind")
                        child = (
                            value.get("hash") or value.get("sha1") or value.get("id")
                        )
                    else:
                        raise ValueError("unsupported legacy entry")
                    if typ in ("T", "tree", "folder", "dir", "directory"):
                        mode, child_kind = b"40000", "tree"
                    elif typ in ("B", "blob", "file"):
                        mode, child_kind = b"100644", "blob"
                    else:
                        raise ValueError("unsupported legacy entry type")
                    target = self.convert(child, child_kind, depth + 1)
                    converted.append((name.encode(), mode, target))
                converted.sort(key=lambda x: x[0] + (b"/" if x[1] == b"40000" else b""))
                body = b"".join(
                    mode + b" " + name + b"\0" + bytes.fromhex(child)
                    for name, mode, child in converted
                )
            else:
                raise ValueError(
                    "legacy commit format requires explicit snapshot import"
                )
        result = self.store(oid, expected or kind, kind, body, raw)
        self.done[key] = result
        self.visiting.remove(key)
        return result

    def snapshot(self, root, label, parent=None, metadata=None):
        tree = self.convert(root, "tree")
        parent_header = f"parent {parent}\n" if parent else ""
        details = ""
        if metadata is not None:
            details = (
                "\nLegacy metadata: "
                + json.dumps(
                    {
                        key: metadata[key]
                        for key in (
                            "id",
                            "commit_id",
                            "who",
                            "message",
                            "created_at",
                            "scope_path",
                        )
                    },
                    sort_keys=True,
                    ensure_ascii=False,
                )
                + "\n"
            )
        body = (
            f"tree {tree}\n{parent_header}author PuppyOne Migration <migration@puppyone.invalid> 0 +0000\n"
            f"committer PuppyOne Migration <migration@puppyone.invalid> 0 +0000\n\n"
            f"Imported legacy snapshot: {label}\nOriginal tree: {root}\n{details}"
        ).encode()
        return self.store(label, "snapshot", "commit", body, body)


# Explicit typed roots: never infer Git object identity from arbitrary JSON text.
ROOT_FIELDS = {
    "version_commits": {
        "root_hash": "tree",
        "scope_hash": "tree",
        "commit_id": "commit",
    },
    "version_scope_state": {"scope_hash": "tree", "head_commit_id": "commit"},
    "version_view_commits": {
        "source_scope_hash": "tree",
        "project_root_hash": "tree",
        "source_commit_id": "commit",
        "project_view_commit_id": "commit",
    },
    "version_refs": {"commit_id": None},
    "version_conflicts": {
        "base_tree_id": "tree",
        "current_tree_id": "tree",
        "proposed_tree_id": "tree",
        "base_commit_id": "commit",
        "current_commit_id": "commit",
        "client_commit_id": "commit",
        "resolution_commit_id": "commit",
    },
    "local_shadow_snapshots": {"tree_hash": "tree"},
    "version_outbox": {"commit_id": "commit"},
}


OLD_DEFAULT_PROMPT = 'You are connected to a PuppyOne repo via the mut protocol.\n\nThe mut protocol gives you read+write access to a versioned, scoped subtree of files. You can clone the current state, push your changes back, and pull the latest from other agents working on the same repo.\n\nTo work with this repo:\n  - Use `mut clone <url>` to fetch the current state of your scope.\n  - Use `mut push` to commit and upload your changes.\n  - Use `mut pull` to get changes from other agents or web users.\n\nYour working scope is constrained — paths outside the scope are invisible. The repo URL above already encodes which scope you have access to.\n\nWhen the user asks you to make a change to the repo, prefer making it locally first, running tests if applicable, then `mut push` once the change is verified.'
NATIVE_DEFAULT_PROMPT = 'This PuppyOne repository uses standard Git. The remote URL is a locator; authenticate with your separately issued Git credential.\n\nUse git clone to obtain the repository. Make local changes, run appropriate checks, then use git add, git commit and git push to publish. Use git fetch and reconcile concurrent changes before pushing again. Permissions and repository policy are checked by the server; a local checkout or remote URL does not grant additional access.'


class RecoveryArchive:
    def __init__(self, db, s3, bucket, project, *, apply=False):
        self.converter = ObjectConverter(db, s3, bucket, project, apply=apply)
        self.db, self.s3, self.bucket, self.project, self.apply = (
            db,
            s3,
            bucket,
            project,
            apply,
        )

    def is_source_key(self, key):
        if key.startswith((f"mut/{self.project}/", f"shadow-snapshots/{self.project}/")):
            return True
        prefix = f"version/{self.project}/objects/"
        if not key.startswith(prefix):
            return False
        # Only the former truncated content IDs. Never include native Git OIDs.
        return re.fullmatch(r"[0-9a-f]{2}/[0-9a-f]{14}", key[len(prefix):]) is not None

    def inventory(self):
        """Inventory old objects and private manifests without sweeping native Git."""
        result = {}
        for prefix in (
            f"mut/{self.project}/",
            f"shadow-snapshots/{self.project}/",
            f"version/{self.project}/objects/",
        ):
            token = None
            for _ in range(1000):
                kwargs = {"Bucket": self.bucket, "Prefix": prefix, "MaxKeys": 1000}
                if token:
                    kwargs["ContinuationToken"] = token
                page = self.s3.list_objects_v2(**kwargs)
                for item in page.get("Contents", []):
                    key = item["Key"]
                    if not key.startswith(prefix) or key in result:
                        raise ValueError("invalid source inventory")
                    if self.is_source_key(key):
                        result[key] = {"size": item["Size"], "etag": item["ETag"]}
                if not page.get("IsTruncated"):
                    break
                next_token = page.get("NextContinuationToken")
                if not next_token or next_token == token:
                    raise ValueError("incomplete source inventory")
                token = next_token
            else:
                raise ValueError("source inventory limit exceeded")
        return result

    def digest(self, key, size):
        response = self.s3.get_object(Bucket=self.bucket, Key=key)
        stream, digest, received = response["Body"], hashlib.sha256(), 0
        try:
            if response["ContentLength"] != size or size > 5 * 1024**3:
                raise ValueError(
                    "source size changed or exceeds single-object archive limit"
                )
            while part := stream.read(1024**2):
                received += len(part)
                if received > size:
                    raise ValueError("source size changed")
                digest.update(part)
            if received != size:
                raise ValueError("truncated archive object")
            return digest.hexdigest()
        finally:
            stream.close()

    def archive_bytes(self, key, metadata):
        digest = self.digest(key, metadata["size"])
        destination = (
            f"version/{self.project}/recovery-archive/sha256/{digest[:2]}/{digest}"
        )
        row = {
            "project_id": self.project,
            "source_key": key,
            "destination_key": destination,
            "body_sha256": digest,
            "size_bytes": metadata["size"],
        }
        if not self.apply:
            return row
        try:
            self.s3.head_object(Bucket=self.bucket, Key=destination)
        except ClientError as exc:
            if not missing_object(exc):
                raise
            self.s3.copy_object(
                Bucket=self.bucket,
                Key=destination,
                CopySource={"Bucket": self.bucket, "Key": key},
                CopySourceIfMatch=metadata["etag"],
            )
        if self.digest(destination, metadata["size"]) != digest:
            raise ValueError("recovery archive destination corrupted")
        expected = (
            "SELECT * FROM jsonb_populate_record(NULL::public.version_repository_archive_objects,"
            + quoted(row)
            + "::jsonb)"
        )
        self.db.sql(
            "BEGIN; INSERT INTO public.version_repository_archive_objects "
            + expected
            + " ON CONFLICT DO NOTHING; DO $proof$ BEGIN IF EXISTS(("
            + expected
            + ") EXCEPT SELECT * FROM public.version_repository_archive_objects) THEN RAISE EXCEPTION 'archive_source_changed'; END IF; END $proof$; COMMIT;"
        )
        return row

    def finish(self):
        """Resume cleanup only from the durable verified manifest.

        Verify ALL destinations and remaining source bytes before the first
        deletion. An unexpected key or changed byte stops cleanup; no blind
        prefix deletion. Partial deletion is resumable without old source reads.
        Writers/GC remain drained for this complete operator invocation.
        """
        job = self.db.sql("SELECT to_jsonb(a) FROM public.version_repository_archives a WHERE project_id=" + quoted(self.project))
        if job["state"] != "verified" or not job["manifest_sha256"]:
            raise ValueError("verified archive required before cleanup")
        rows = self.db.rows("SELECT * FROM public.version_repository_archive_objects WHERE project_id=" + quoted(self.project) + " ORDER BY source_key")
        roots = self.db.rows("SELECT * FROM public.version_repository_archive_roots WHERE project_id=" + quoted(self.project) + " ORDER BY source_table,source_row,source_field")
        # Ordering is part of the manifest contract, independent of retry order.
        expected = hashlib.sha256(json.dumps({"source": job["source"], "roots": roots, "objects": rows},
            sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if expected != job["manifest_sha256"]:
            raise ValueError("recovery archive manifest changed")
        current = self.inventory()
        known = {r["source_key"]: r for r in rows}
        if current.keys() - known.keys():
            raise ValueError("unexpected source key after archive")
        for row in rows:
            destination = f"version/{self.project}/recovery-archive/sha256/{row['body_sha256'][:2]}/{row['body_sha256']}"
            if row["destination_key"] != destination or not self.is_source_key(row["source_key"]):
                raise ValueError("archive key binding mismatch")
            if self.digest(destination, row["size_bytes"]) != row["body_sha256"]:
                raise ValueError("recovery archive destination corrupted")
            if row["source_key"] in current and self.digest(row["source_key"], row["size_bytes"]) != row["body_sha256"]:
                raise ValueError("source bytes changed after archive")
        for key in sorted(current):
            # The old namespace is fenced at the database and operator writers
            # are drained. Conditional copy + verified manifest protect bytes.
            self.s3.delete_object(Bucket=self.bucket, Key=key)
        if self.inventory():
            raise ValueError("source namespace is not empty after cleanup")
        # Preserve original prompt text in the source manifest, and update only
        # the exact shipped default. Never rewrite user-authored instructions.
        self.db.sql("BEGIN; UPDATE public.projects SET prompt_template="
            + quoted(NATIVE_DEFAULT_PROMPT) + " WHERE id=" + quoted(self.project)
            + " AND prompt_template=" + quoted(OLD_DEFAULT_PROMPT)
            + "; SELECT public.finish_repository_recovery_archive(" + quoted(self.project) + "); COMMIT;")
        return {"project": self.project, "state": "verified", "private_roots": len(roots),
                "source_objects": len(rows), "manifest_sha256": expected, "source_deleted": True}

    def prepare(self):
        if self.apply:
            job = self.db.sql(
                "SELECT public.begin_repository_recovery_archive("
                + quoted(self.project)
                + ");"
            )
            if job["state"] == "verified":
                return self.finish()
            source = job["source"]
        else:
            source = self.db.sql(
                "SELECT public._repository_recovery_source("
                + quoted(self.project)
                + ");"
            )
        before = self.inventory()
        roots = []
        for table, fields in ROOT_FIELDS.items():
            for row in source.get(table, []):
                for field, kind in fields.items():
                    old = row.get(field)
                    if not old:
                        continue
                    # Snapshot sequence identifiers were not Git commits. Preserve
                    # them in metadata; their typed trees are converted separately.
                    if kind == "commit" and len(old) != 40:
                        continue
                    oid = self.converter.convert(old, kind)
                    actual_kind = self.converter.objects[oid][0]
                    root = {
                        "project_id": self.project,
                        "source_table": table,
                        "source_row": str(row["id"]),
                        "source_field": field,
                        "source_id": old,
                        "object_id": oid,
                        "object_kind": actual_kind,
                    }
                    roots.append(root)
                    if self.apply:
                        expected = (
                            "SELECT * FROM jsonb_populate_record(NULL::public.version_repository_archive_roots,"
                            + quoted(root)
                            + "::jsonb)"
                        )
                        self.db.sql(
                            "BEGIN; INSERT INTO public.version_repository_archive_roots "
                            + expected
                            + " ON CONFLICT DO NOTHING; DO $proof$ BEGIN IF EXISTS(("
                            + expected
                            + ") EXCEPT SELECT * FROM public.version_repository_archive_roots) THEN RAISE EXCEPTION 'archive_root_changed'; END IF; END $proof$; COMMIT;"
                        )
        # Preserve every old physical object, including unindexed/unclassified
        # recovery bytes. These opaque archive bodies are never a runtime store.
        objects = [
            self.archive_bytes(key, metadata)
            for key, metadata in sorted(before.items())
        ]
        if self.inventory() != before:
            raise ValueError("source inventory changed during archive")
        roots.sort(key=lambda r: (r["source_table"], r["source_row"], r["source_field"]))
        manifest = hashlib.sha256(
            json.dumps(
                {"source": source, "roots": roots, "objects": objects},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        if self.apply:
            self.converter.flush()
            self.db.sql(
                "DO $verify$ BEGIN IF public._repository_recovery_source("
                + quoted(self.project)
                + ") IS DISTINCT FROM "
                + quoted(source)
                + "::jsonb THEN RAISE EXCEPTION 'recovery_source_changed'; END IF; END $verify$; "
                "UPDATE public.version_repository_archives SET state='verified',manifest_sha256="
                + quoted(manifest)
                + ",verified_at=now() WHERE project_id="
                + quoted(self.project)
                + ";"
            )
        if self.apply:
            return self.finish()
        return {
            "project": self.project,
            "state": "verified" if self.apply else "planned",
            "private_roots": len(roots),
            "source_objects": len(objects),
            "manifest_sha256": manifest,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.apply and os.environ.get("NATIVE_MIGRATION_WRITERS_DRAINED") != "yes":
        parser.error(
            "stop/drain writers and GC before archive: NATIVE_MIGRATION_WRITERS_DRAINED=yes"
        )
    db = Database(os.environ["DATA_MIGRATION_DATABASE_URL"])
    # Inspect all Projects. Empty native repositories are cheap and receive a
    # receipt too; unknown/unindexed old objects cannot evade the inventory.
    projects = db.rows("SELECT id FROM public.projects ORDER BY id")
    client = boto3.client(
        "s3",
        endpoint_url=os.environ["S3_ENDPOINT_URL"],
        region_name=os.environ["S3_REGION"],
        aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            connect_timeout=10,
            read_timeout=60,
            retries={"max_attempts": 2},
        ),
    )
    try:
        for project in projects:
            result = RecoveryArchive(
                db,
                client,
                os.environ["S3_BUCKET_NAME"],
                project["id"],
                apply=args.apply,
            ).prepare()
            print(json.dumps(result), flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    main()
