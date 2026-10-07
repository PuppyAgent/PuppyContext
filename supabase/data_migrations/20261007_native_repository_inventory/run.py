"""Immutable operator artifact. Default is read-only planning, never activation.

No imports from application code; dependencies are supplied by the locked runtime.
Use puppyone-db run after deployment and writer/GC drain. Source objects and
metadata are never removed. Database owner access is required intentionally.
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

ARTIFACT = "20261007_native_repository_inventory"
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


class Migration:
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
            "SELECT * FROM jsonb_populate_recordset(NULL::public.version_repository_migration_objects,"
            + quoted(self.pending)
            + "::jsonb)"
        )
        self.db.sql(
            "BEGIN; INSERT INTO public.version_repository_migration_objects "
            + expected
            + " ON CONFLICT(project_id,source_id,source_kind) DO NOTHING;"
            + " DO $checkpoint$ BEGIN IF EXISTS(("
            + expected
            + ") EXCEPT SELECT * FROM public.version_repository_migration_objects)"
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

    def read_canonical(self, oid):
        raw = self.get(self.key(oid)) if self.apply else self.source(oid)
        if raw is None:
            raise FileNotFoundError("canonical object missing")
        return decode(raw, oid)

    def logical(self, root):
        # Count every path, including duplicate blobs/subtrees, not unique OIDs.
        visits = [0]

        def measure(oid, depth=0):
            visits[0] += 1
            if visits[0] > 100000 or depth > 200:
                raise ValueError("logical tree traversal limit exceeded")
            kind, body = self.read_canonical(oid)
            if kind == "blob":
                return len(body)
            if kind != "tree":
                raise ValueError("invalid logical tree")
            return sum(
                measure(child, depth + 1)
                for mode, _, child in tree_entries(body)
                if mode != b"160000"
            )

        return measure(root)

    def prepare(self):
        if self.apply:
            job = self.db.sql(
                "SELECT public.begin_native_repository_migration("
                + quoted(self.project)
                + ");"
            )
            if job["state"] == "activated":
                return {"state": "activated", "project": self.project}
            source = job["source"]
        else:
            source = self.db.sql(
                "SELECT public._native_migration_source(" + quoted(self.project) + ");"
            )
        root = source["root"]
        if not root:
            raise ValueError("missing current project root")
        current_tree = self.convert(root, "tree")
        refs, candidates = {}, set()
        for scope in source["scopes"]:
            if (
                scope["scope_path"] == ""
                and scope["scope_hash"] == root
                and scope["head_commit_id"]
            ):
                candidates.add(scope["head_commit_id"])
        for view in source["views"]:
            if view["scope_path"] == "" and view["project_root_hash"] == root:
                candidates.add(view["project_view_commit_id"])
        for commit in source["commits"]:
            if (
                commit["root_hash"] == root
                and commit["scope_path"] == ""
                and len(commit["commit_id"]) == 40
            ):
                candidates.add(commit["commit_id"])
        # Prefer the authoritative root-scope HEAD; historical equal trees can
        # have many different commits and must never be selected by timestamp.
        heads = [
            s["head_commit_id"]
            for s in source["scopes"]
            if s["scope_path"] == "" and s["scope_hash"] == root and s["head_commit_id"]
        ]
        if heads:
            candidates = set(heads)
        if len(candidates) > 1:
            raise ValueError("ambiguous full-project HEAD")
        if candidates and re.fullmatch(r"[0-9a-f]{40}", next(iter(candidates))):
            head = self.convert(next(iter(candidates)), "commit")
            _, raw = self.read_canonical(head)
            if edges("commit", raw)[0][0] != current_tree:
                raise ValueError("full-project HEAD tree mismatch")
        else:
            head = None
        prior_snapshot = None
        for commit in source["commits"]:
            old_root = commit["root_hash"]
            if not old_root:
                raise ValueError("history row missing project root")
            label = "history-" + str(commit["id"])
            if commit["scope_path"] == "" and re.fullmatch(
                r"[0-9a-f]{40}", commit["commit_id"]
            ):
                history = self.convert(commit["commit_id"], "commit")
                _, history_body = self.read_canonical(history)
                if edges("commit", history_body)[0][0] != self.convert(
                    old_root, "tree"
                ):
                    raise ValueError("history commit/project tree mismatch")
            else:
                history = self.snapshot(
                    old_root, label, parent=prior_snapshot, metadata=commit
                )
            prior_snapshot = history
            refs["refs/puppyone/migrated-history/" + str(commit["id"])] = history
        if head is None:
            head = self.snapshot(root, "current", parent=prior_snapshot)
        refs["refs/heads/main"] = head
        for view in source["views"]:
            # Full-project view commits encode scoped edits into a full tree.
            if view["scope_path"] == "":
                oid = self.convert(view["project_view_commit_id"], "commit")
                refs["refs/puppyone/migrated-views/" + str(view["id"])] = oid
        for ref in source["refs"]:
            if ref["scope_path"]:
                raise ValueError("scoped named refs require explicit archival mapping")
            name = ref["ref_name"]
            if name in refs or not name.startswith(("refs/heads/", "refs/tags/")):
                raise ValueError("duplicate or invalid imported ref")
            oid = self.convert(
                ref["commit_id"], "commit" if ref["ref_type"] == "branch" else None
            )
            refs[name] = oid
        if not self.apply:
            # Dry-run validates source conversion without requiring newly
            # converted objects to exist remotely; logical metrics are apply-only.
            return {
                "state": "planned",
                "project": self.project,
                "objects": len(self.objects),
                "body_bytes": self.bytes,
                "refs": len(refs),
                "source_format": "git" if len(root) == 40 else "legacy_raw",
            }
        logical = self.logical(current_tree)
        self.flush()
        # Index reads must no longer resolve to mutable/deferred placements.
        # Only entries for verified canonical objects are removed; source bundles
        # and all unrelated records are retained. Owner-only frozen operation.
        self.db.sql(
            "DELETE FROM public.version_object_locations l USING public.version_repository_migration_objects m"
            " WHERE l.project_id=m.project_id AND l.object_id=m.object_id AND m.project_id="
            + quoted(self.project)
            + ";"
        )
        for oid in sorted(set(refs.values())):
            kind, body = self.read_canonical(oid)
            peeled = None
            if kind == "tag":
                target_kind, target = kind, oid
                for _ in range(200):
                    target_kind, body = self.read_canonical(target)
                    if target_kind != "tag":
                        break
                    target = edges(target_kind, body)[0][0]
                else:
                    raise ValueError("tag chain too deep")
                peeled = target
            self.db.sql(
                "INSERT INTO public.version_repository_root_metadata(project_id,oid,object_format,kind,peeled_oid) VALUES("
                + ",".join(map(quoted, (self.project, oid, "sha1", kind, peeled)))
                + ") ON CONFLICT DO NOTHING;"
            )
        manifest = hashlib.sha256(
            json.dumps(
                {
                    "refs": refs,
                    "objects": sorted(self.objects.items()),
                    "logical_bytes": logical,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        self.db.sql(
            "SELECT public.prepare_native_repository_migration("
            + ",".join(map(quoted, (self.project, refs, logical, manifest)))
            + ");"
        )
        return {
            "state": "prepared",
            "project": self.project,
            "objects": len(self.objects),
            "body_bytes": self.bytes,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.apply and os.environ.get("NATIVE_MIGRATION_WRITERS_DRAINED") != "yes":
        parser.error(
            "apply requires stopped/drained writers and GC: NATIVE_MIGRATION_WRITERS_DRAINED=yes"
        )
    db = Database(os.environ["DATA_MIGRATION_DATABASE_URL"])
    projects = db.rows(
        "SELECT p.id,p.org_id FROM public.projects p LEFT JOIN public.version_repositories r ON r.project_id=p.id"
        " WHERE coalesce(r.authority,'legacy')<>'native' ORDER BY p.org_id,p.id"
    )
    s3 = boto3.client(
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
        failures = []
        for project in projects:
            try:
                result = Migration(
                    db,
                    s3,
                    os.environ["S3_BUCKET_NAME"],
                    project["id"],
                    apply=args.apply,
                ).prepare()
                print(json.dumps(result), flush=True)
            except Exception as exc:  # noqa: BLE001 - isolate and report each blocked project; fail the whole release below.
                failures.append(project["id"])
                print(
                    json.dumps(
                        {
                            "project": project["id"],
                            "state": "blocked",
                            "error_type": type(exc).__name__,
                            "reason": str(exc)
                            if isinstance(
                                exc, (ValueError, FileNotFoundError, RuntimeError)
                            )
                            else "external service failure",
                        }
                    ),
                    flush=True,
                )
        if failures:
            raise RuntimeError(
                f"{len(failures)} projects blocked; sources retained; no organization activated"
            )
        if args.apply:
            for org in sorted({p["org_id"] for p in projects}):
                db.sql(
                    "SELECT public.activate_native_repository_migrations("
                    + quoted(org)
                    + ");"
                )
    finally:
        s3.close()


if __name__ == "__main__":
    main()
