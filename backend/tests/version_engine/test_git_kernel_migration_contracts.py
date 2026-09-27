"""Migration guardrails for the Git-kernel refactor."""

from __future__ import annotations

import ast
import random
import re
import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.ingest.file.jobs.jobs import stage_blob_from_s3
from src.platform.authorization.models import RuntimeGrant, RuntimeMode, RuntimePrincipal
from src.platform.repository_target.models import ResolvedRepositoryView, ScopeTarget
from src.version_engine.adapters.git.object_quarantine import GitObjectQuarantine
from src.version_engine.admission.repo_facade import repo_facade_from_auth
from src.version_engine.domain.errors import ObjectNotFoundError
from src.version_engine.infrastructure.supabase.db_names import OBJECT_LOCATIONS_TABLE
from src.version_engine.storage.backends.s3 import S3StorageBackend
from src.version_engine.storage.io_strategy import IOStorageStrategy
from src.version_engine.storage.object_store import ObjectStore, StorageBackend
from src.version_engine.write_engine.git_object_format import (
    EMPTY_TREE_LOOSE_BYTES,
    EMPTY_TREE_SHA1,
    MODE_DIR,
    MODE_FILE,
    TreeEntry,
    decode_commit,
    decode_object,
    decode_tag,
    decode_tree,
    encode_commit,
    encode_object,
    encode_tree,
    hash_object,
)
from src.version_engine.write_engine.path_utils import normalize_path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_ROOT.parent

PRODUCT_WRITE_MODULES = (
    "src/version_engine/adapters/product/operation_adapter.py",
    "src/version_engine/write_engine/engine.py",
    "src/version_engine/derived/projection.py",
    "src/version_engine/derived/hooks.py",
    "src/version_engine/entrypoints/http/content_write.py",
)

ACTIVE_RUNTIME_SCAN_ROOTS = (
    "AGENTS.md",
    "backend/AGENTS.md",
    "backend/CLAUDE.md",
    "backend/mcp_service",
    "backend/scripts",
    "backend/src/repo",
    "backend/src/version_engine",
    "backend/tests/conflicts",
    "backend/tests/mcp_service",
    "backend/tests/version_engine",
    "cli/src",
    "docs/README.md",
    "docs/architecture",
    "docs/cli",
    "frontend/lib",
    "frontend/app/(main)/projects/[projectId]",
    "frontend/components",
    "scripts",
    "tests/e2e",
)

ALLOWED_DEFERRED_DB_NAME_FILES = {
    "backend/src/version_engine/infrastructure/supabase/db_names.py",
}


def test_git_object_helpers_match_git_hash_object() -> None:
    content = b"hello from puppyone\n"

    object_id, loose = encode_object("blob", content)
    expected = (
        subprocess.run(
            ["git", "hash-object", "--stdin"],
            input=content,
            stdout=subprocess.PIPE,
            check=True,
        )
        .stdout.decode("ascii")
        .strip()
    )

    assert object_id == expected
    assert hash_object("blob", content) == expected
    assert decode_object(loose) == ("blob", content)


def test_git_tree_and_commit_helpers_round_trip() -> None:
    blob_id, _loose = encode_object("blob", b"data\n")
    tree_body = encode_tree(
        [
            TreeEntry(name="src", mode=MODE_DIR, sha1_hex="1" * 40),
            TreeEntry(name="README.md", mode=MODE_FILE, sha1_hex=blob_id),
        ]
    )

    entries = decode_tree(tree_body)
    assert [entry.name for entry in entries] == ["README.md", "src"]
    assert entries[0].sha1_hex == blob_id
    assert entries[1].is_dir

    commit_body = encode_commit(
        tree_sha1="2" * 40,
        parent_sha1="3" * 40,
        author="A <a@example.com>",
        author_time="1767225600 +0000",
        committer="C <c@example.com>",
        committer_time="1767225601 +0000",
        message="hello\n",
    )
    commit = decode_commit(commit_body)
    assert commit["tree"] == "2" * 40
    assert commit["parents"] == ["3" * 40]
    assert commit["message"] == "hello"

    tag = decode_tag(
        (
            f"object {'4' * 40}\n"
            "type commit\n"
            "tag v1.0\n"
            "tagger A <a@example.com> 1767225602 +0000\n"
            "\nRelease v1.0\n"
        ).encode("utf-8")
    )
    assert tag["object"] == "4" * 40
    assert tag["type"] == "commit"
    assert tag["tag"] == "v1.0"
    assert tag["message"] == "Release v1.0"


def test_git_tree_encoder_rejects_legacy_short_object_ids() -> None:
    with pytest.raises(ValueError, match="40 hex"):
        encode_tree(
            [
                TreeEntry(name="legacy", mode=MODE_DIR, sha1_hex="28a44dbeee08f49e"),
            ]
        )


def test_empty_git_tree_is_virtual_builtin_object(tmp_path) -> None:
    class _NoStorageBackend(StorageBackend):
        def get(self, h: str) -> bytes:
            raise AssertionError("empty tree should not hit object storage")

        def put(self, h: str, loose_bytes: bytes) -> None:
            raise AssertionError("empty tree should not be persisted as loose data")

        def exists(self, h: str) -> bool:
            raise AssertionError("empty tree should not need an existence probe")

        def all_hashes(self) -> list[str]:
            return []

        def count(self) -> tuple[int, int]:
            return 0, 0

        def delete(self, h: str) -> bool:
            return False

    store = ObjectStore(tmp_path / "objects", backend=_NoStorageBackend())

    assert EMPTY_TREE_SHA1 == hash_object("tree", b"")
    assert decode_object(EMPTY_TREE_LOOSE_BYTES) == ("tree", b"")
    assert store.exists(EMPTY_TREE_SHA1) is True
    assert store.get_object(EMPTY_TREE_SHA1) == ("tree", b"")
    assert store.get_loose(EMPTY_TREE_SHA1) == EMPTY_TREE_LOOSE_BYTES
    assert store.put_tree(b"") == EMPTY_TREE_SHA1


def test_git_quarantine_promotion_blind_writes_receive_pack_new_objects(
    tmp_path,
    monkeypatch,
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    _run_git_cmd(["init"], work)
    _run_git_cmd(["config", "user.name", "Git User"], work)
    _run_git_cmd(["config", "user.email", "git@example.com"], work)

    (work / "a.txt").write_text("a\n", encoding="utf-8")
    _run_git_cmd(["add", "a.txt"], work)
    _run_git_cmd(["commit", "-m", "initial"], work)
    first = _run_git_cmd(["rev-parse", "HEAD"], work).decode("ascii").strip()

    (work / "b.txt").write_text("b\n", encoding="utf-8")
    _run_git_cmd(["add", "b.txt"], work)
    _run_git_cmd(["commit", "-m", "second"], work)
    second = _run_git_cmd(["rev-parse", "HEAD"], work).decode("ascii").strip()

    expected_new = _git_rev_list_objects(work / ".git", second, exclude=first)
    assert expected_new
    assert first not in expected_new

    class _FakeStore:
        def __init__(self):
            self.exists_many_calls: list[list[str]] = []
            self.puts: dict[str, bytes] = {}

        def exists_many(self, hashes: list[str]) -> set[str]:
            raise AssertionError("receive-pack promotion should not probe existence")

        def put_loose(self, object_id: str, loose: bytes) -> None:
            self.puts[object_id] = loose

    class _FakeRepo:
        def __init__(self):
            self.store = _FakeStore()

    flushes: list[int] = []

    @contextmanager
    def _fake_stage_object_writes(_store):
        yield SimpleNamespace(flush=lambda: flushes.append(1))

    monkeypatch.setattr(
        "src.version_engine.adapters.git.object_quarantine.stage_object_writes",
        _fake_stage_object_writes,
    )
    repo = _FakeRepo()
    quarantine = GitObjectQuarantine(
        repo=repo,
        bare_dir=work / ".git",
        roots=[second],
        exclude_roots=[first],
    )

    quarantine.promote_reachable()

    assert repo.store.exists_many_calls == []
    assert set(repo.store.puts) == expected_new
    assert flushes == [1]


def _run_git_cmd(args: list[str], cwd: Path) -> bytes:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout


def _git_rev_list_objects(git_dir: Path, root: str, *, exclude: str = "") -> set[str]:
    args = ["git", "--git-dir", str(git_dir), "rev-list", "--objects", root]
    if exclude:
        args.extend(["--not", exclude])
    out = subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout
    return {line.split(maxsplit=1)[0].decode("ascii") for line in out.splitlines() if line}


@pytest.mark.asyncio
async def test_object_batch_writes_one_bundle_with_location_index() -> None:
    blob_id, blob_loose = encode_object("blob", b"hello\n")
    tree_id, tree_loose = encode_object("tree", encode_tree([]))
    commit_id, commit_loose = encode_object(
        "commit",
        f"tree {tree_id}\n\nbundle test\n".encode("ascii"),
    )

    class _FakeS3:
        def __init__(self):
            self.bucket_name = "bucket"
            self.uploads: dict[str, bytes] = {}
            self.download_file_keys: list[str] = []
            self.download_range_keys: list[str] = []
            self.file_exists_keys: list[str] = []

        async def upload_file(self, key, content, content_type=None, metadata=None):
            self.uploads[key] = content
            return SimpleNamespace(key=key)

        async def download_file(self, key):
            self.download_file_keys.append(key)
            if key not in self.uploads:
                raise FileNotFoundError("not found")
            return self.uploads[key]

        async def download_file_range(self, key, start=0, limit=None):
            self.download_range_keys.append(key)
            if key not in self.uploads:
                raise FileNotFoundError("not found")
            content = self.uploads[key]
            end = len(content) if limit is None else min(len(content), start + limit)
            return content[start:end], len(content)

        async def file_exists(self, key):
            self.file_exists_keys.append(key)
            return key in self.uploads

    class _FakeTable:
        def __init__(self, db):
            self.db = db
            self.filters = {}
            self._upsert_rows = None

        def upsert(self, rows, on_conflict=None):
            self._upsert_rows = rows
            return self

        def select(self, *_args):
            return self

        def eq(self, key, value):
            self.filters[key] = value
            return self

        def in_(self, key, values):
            self.filters[key] = list(values)
            return self

        def limit(self, _value):
            return self

        def execute(self):
            if self._upsert_rows is not None:
                for row in self._upsert_rows:
                    self.db.rows[(row["project_id"], row["object_id"])] = row
                return SimpleNamespace(data=self._upsert_rows)
            self.db.select_calls += 1
            project_id = self.filters.get("project_id")
            object_ids = self.filters.get("object_id")
            if isinstance(object_ids, list):
                data = [
                    row
                    for oid in object_ids
                    if (row := self.db.rows.get((project_id, oid))) is not None
                ]
                return SimpleNamespace(data=data)
            row = self.db.rows.get((project_id, object_ids))
            return SimpleNamespace(data=[row] if row else [])

    class _FakeSupabase:
        def __init__(self):
            self.client = self
            self.rows = {}
            self.select_calls = 0

        def table(self, name):
            assert name == OBJECT_LOCATIONS_TABLE
            return _FakeTable(self)

    s3 = _FakeS3()
    supabase = _FakeSupabase()
    backend = S3StorageBackend(s3, "proj", supabase=supabase)

    await backend.async_put_many(
        {
            blob_id: blob_loose,
            tree_id: tree_loose,
            commit_id: commit_loose,
        },
        skip_exists=True,
    )

    assert len(s3.uploads) == 1
    [pack_key] = list(s3.uploads)
    assert "/object-bundles/" in pack_key
    assert all("/objects/" not in key for key in s3.uploads)
    assert len(supabase.rows) == 3

    # Simulate a fresh backend instance: lookup must work through the durable
    # location index, not through the writer's in-memory cache.
    cold_backend = S3StorageBackend(s3, "proj", supabase=supabase)
    assert cold_backend.exists(blob_id) is True
    assert cold_backend.get(blob_id) == blob_loose
    assert cold_backend.get(tree_id) == tree_loose
    assert cold_backend.get(commit_id) == commit_loose
    blob_slice, blob_total = await cold_backend.async_get_range(blob_id, start=1, limit=6)
    assert blob_total == len(blob_loose)
    assert blob_slice == blob_loose[1:7]
    assert s3.download_file_keys == []
    assert s3.file_exists_keys == []
    assert s3.download_range_keys
    assert all("/object-bundles/" in key for key in s3.download_range_keys)

    bulk_backend = S3StorageBackend(s3, "proj", supabase=supabase)
    s3.file_exists_keys.clear()
    supabase.select_calls = 0
    assert bulk_backend.exists_many([blob_id, tree_id, commit_id]) == {
        blob_id,
        tree_id,
        commit_id,
    }
    assert supabase.select_calls == 1
    assert s3.file_exists_keys == []


@pytest.mark.asyncio
async def test_object_batch_chunks_large_objects_below_storage_object_cap() -> None:
    rng = random.Random(0)
    large_id, large_loose = encode_object(
        "blob",
        bytes(rng.randrange(256) for _ in range(4096)),
    )
    small_id, small_loose = encode_object("blob", b"small\n")

    class _FakeS3:
        def __init__(self):
            self.uploads: dict[str, bytes] = {}
            self.upload_sizes: list[int] = []

        async def upload_file(self, key, content, content_type=None, metadata=None):
            self.upload_sizes.append(len(content))
            if len(content) > 4096:
                raise AssertionError(f"physical S3 object too large: {len(content)}")
            self.uploads[key] = content
            return SimpleNamespace(key=key)

        async def download_file(self, key):
            if key not in self.uploads:
                raise FileNotFoundError("not found")
            return self.uploads[key]

        async def download_file_range(self, key, start=0, limit=None):
            if key not in self.uploads:
                raise FileNotFoundError("not found")
            content = self.uploads[key]
            end = len(content) if limit is None else min(len(content), start + limit)
            return content[start:end], len(content)

        async def file_exists(self, key):
            return key in self.uploads

    class _FakeTable:
        def __init__(self, db):
            self.db = db
            self.filters = {}
            self._upsert_rows = None

        def upsert(self, rows, on_conflict=None):
            self._upsert_rows = rows
            return self

        def select(self, *_args):
            return self

        def eq(self, key, value):
            self.filters[key] = value
            return self

        def in_(self, key, values):
            self.filters[key] = list(values)
            return self

        def execute(self):
            if self._upsert_rows is not None:
                for row in self._upsert_rows:
                    self.db.rows[(row["project_id"], row["object_id"])] = row
                return SimpleNamespace(data=self._upsert_rows)
            project_id = self.filters.get("project_id")
            object_ids = self.filters.get("object_id")
            if isinstance(object_ids, list):
                return SimpleNamespace(
                    data=[
                        row
                        for oid in object_ids
                        if (row := self.db.rows.get((project_id, oid))) is not None
                    ]
                )
            row = self.db.rows.get((project_id, object_ids))
            return SimpleNamespace(data=[row] if row else [])

    class _FakeSupabase:
        def __init__(self):
            self.client = self
            self.rows = {}

        def table(self, name):
            assert name == OBJECT_LOCATIONS_TABLE
            return _FakeTable(self)

    s3 = _FakeS3()
    supabase = _FakeSupabase()
    backend = S3StorageBackend(
        s3,
        "proj",
        supabase=supabase,
        io_strategy=IOStorageStrategy(
            bundle_target_bytes=1024,
            chunk_bytes=512,
        ),
    )

    await backend.async_put_many(
        {
            large_id: large_loose,
            small_id: small_loose,
        },
        skip_exists=True,
    )

    large_location = supabase.rows[("proj", large_id)]
    small_location = supabase.rows[("proj", small_id)]
    assert large_location["pack_key"].startswith("chunked:")
    assert "/object-bundles/" in small_location["pack_key"]
    assert small_location["pack_key"].endswith(".pob")
    assert any("/chunked/" in key and "/part-" in key for key in s3.uploads)

    cold_backend = S3StorageBackend(s3, "proj", supabase=supabase)
    assert cold_backend.get(large_id) == large_loose
    assert cold_backend.get(small_id) == small_loose
    large_slice, large_total = await cold_backend.async_get_range(large_id, 10, 25)
    assert large_total == len(large_loose)
    assert large_slice == large_loose[10:35]


def test_s3_backend_reads_deferred_namespace_but_writes_final_namespace() -> None:
    object_id, loose = encode_object("blob", b"from deferred storage\n")
    deferred_namespace = "".join(("m", "ut"))
    deferred_key = f"{deferred_namespace}/proj/objects/{object_id[:2]}/{object_id[2:]}"

    class _FakeS3:
        def __init__(self):
            self.uploads: dict[str, bytes] = {deferred_key: loose}
            self.uploaded_keys: list[str] = []

        async def upload_file(self, key, content, content_type=None, metadata=None):
            self.uploads[key] = content
            self.uploaded_keys.append(key)
            return SimpleNamespace(key=key)

        async def download_file(self, key):
            if key not in self.uploads:
                raise FileNotFoundError("not found")
            return self.uploads[key]

        async def download_file_range(self, key, start=0, limit=None):
            if key not in self.uploads:
                raise FileNotFoundError("not found")
            content = self.uploads[key]
            end = len(content) if limit is None else min(len(content), start + limit)
            return content[start:end], len(content)

        async def file_exists(self, key):
            return key in self.uploads

    s3 = _FakeS3()
    backend = S3StorageBackend(s3, "proj", allow_deferred_namespace_reads=True)

    assert backend.get(object_id) == loose
    assert backend.exists(object_id) is True

    new_id, new_loose = encode_object("blob", b"new canonical write\n")
    backend.put(new_id, new_loose)

    expected_new_key = f"version/proj/objects/{new_id[:2]}/{new_id[2:]}"
    assert s3.uploaded_keys == [expected_new_key]
    assert s3.uploads[expected_new_key] == new_loose

    strict_backend = S3StorageBackend(s3, "proj", allow_deferred_namespace_reads=False)
    with pytest.raises(ObjectNotFoundError):
        strict_backend.get(object_id)


def test_path_normalization_is_owned_by_puppyone() -> None:
    assert normalize_path("/docs/readme.md/") == "docs/readme.md"
    assert normalize_path("") == ""
    with pytest.raises(ValueError, match="path traversal"):
        normalize_path("docs/../secret.md")


def test_access_point_auth_maps_to_repo_facade_not_physical_repo() -> None:
    target = ScopeTarget(project_id="proj-1", scope_id="scope-123")
    auth = {
        "agent": "scope:scope-123",
        "_project_id": "proj-1",
        "_runtime_grant": RuntimeGrant(
            principal=RuntimePrincipal(
                principal_id="legacy-credential",
                credential_kind="legacy_access_key",
            ),
            target=target,
            repository_view=ResolvedRepositoryView(
                target=target,
                path_prefix="docs",
                excludes=("tmp", "cache"),
                max_mode="rw",
            ),
            mode=RuntimeMode.READ_WRITE,
        ),
        "_repo_facade": {
            "id": "scope-123",
            "kind": "access_point",
            "ref": "refs/heads/main",
            "object_store_scope": "project-shared",
        },
    }

    facade = repo_facade_from_auth("proj-1", auth, kind="access_point")

    assert facade.project_id == "proj-1"
    assert facade.repo_id == "scope-123"
    assert facade.kind == "access_point"
    assert facade.scope_path == "docs"
    assert facade.excludes == ("tmp", "cache")
    assert facade.ref == "refs/heads/main"
    assert facade.object_store_scope == "project-shared"
    assert facade.read_only is False


@pytest.mark.asyncio
async def test_upload_staging_writes_git_loose_blob_bytes() -> None:
    raw = b"raw upload bytes"
    source_key = "uploads/file.bin"

    class _Client:
        def head_object(self, *, Bucket, Key):
            assert Bucket == "bucket"
            assert Key == source_key
            return {"ContentLength": len(raw)}

    class _FakeS3:
        bucket_name = "bucket"

        def __init__(self):
            self.client = _Client()
            self.uploads: dict[str, bytes] = {}

        async def download_file_stream(self, key: str, chunk_size: int):
            assert key == source_key
            yield raw[:4]
            yield raw[4:]

        async def object_exists(self, key: str) -> bool:
            return key in self.uploads

        async def upload_file(self, key: str, content: bytes, content_type: str | None = None):
            assert content_type == "application/octet-stream"
            self.uploads[key] = content

    s3 = _FakeS3()
    ref = await stage_blob_from_s3(s3, project_id="project-1", src_key=source_key)

    expected_hash = hash_object("blob", raw)
    expected_key = f"version/project-1/objects/{expected_hash[:2]}/{expected_hash[2:]}"
    assert ref.hash == expected_hash
    assert ref.size == len(raw)
    assert set(s3.uploads) == {expected_key}
    assert decode_object(s3.uploads[expected_key]) == ("blob", raw)


@pytest.mark.asyncio
async def test_upload_staging_overwrites_legacy_raw_bytes_at_dst_key() -> None:
    """Regression: a pre-existing **raw-bytes** entry at the version object
    key (left behind by the historic ``copy_object``-based finalize path
    described in ``infra/s3/service.py::copy_object``) must be detected
    via a size mismatch and overwritten with the proper Git loose-object
    bytes — otherwise the subsequent ``zlib.decompress`` on read blows
    up with ``invalid git loose object … incorrect header check`` and
    bulk push fails for the whole batch.
    """
    from datetime import datetime, timezone

    from src.infra.s3.exceptions import S3FileNotFoundError
    from src.infra.s3.schemas import FileMetadata

    raw = b"hello world content for the legacy raw bytes test"
    source_key = "uploads/legacy-collision.bin"
    blob_hash, loose_bytes = encode_object("blob", raw)
    dst_key = f"version/project-2/objects/{blob_hash[:2]}/{blob_hash[2:]}"

    class _Client:
        def head_object(self, *, Bucket, Key):
            assert Bucket == "bucket"
            assert Key == source_key
            return {"ContentLength": len(raw)}

    class _FakeS3:
        bucket_name = "bucket"

        def __init__(self):
            self.client = _Client()
            # Seed dst_key with RAW bytes — mimicking what the old
            # CopyObject finalize would have left behind.
            self.uploads: dict[str, bytes] = {dst_key: raw}

        async def download_file_stream(self, key, chunk_size):
            assert key == source_key
            yield raw

        async def object_exists(self, key):
            return key in self.uploads

        async def get_file_metadata(self, key):
            if key not in self.uploads:
                raise S3FileNotFoundError(key)
            data = self.uploads[key]
            return FileMetadata(
                key=key,
                bucket="bucket",
                size=len(data),
                etag="x",
                last_modified=datetime.now(timezone.utc),
                content_type=None,
                metadata={},
            )

        async def upload_file(self, key, content, content_type=None):
            self.uploads[key] = content

    s3 = _FakeS3()
    ref = await stage_blob_from_s3(s3, project_id="project-2", src_key=source_key)

    # The legacy raw bytes happened to be exactly len(raw) — different
    # from len(loose_bytes) since zlib framing changes the size. The
    # staging path must have detected the mismatch and overwritten.
    assert ref.hash == blob_hash
    assert s3.uploads[dst_key] == loose_bytes
    assert decode_object(s3.uploads[dst_key]) == ("blob", raw)


def test_backend_python_no_longer_imports_external_legacy_version_package() -> None:
    offenders: list[str] = []
    legacy_prefix = "".join(("m", "ut."))

    for root in (BACKEND_ROOT / "src", BACKEND_ROOT / "tests"):
        paths = root.rglob("*.py")
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if module == "legacy" or module.startswith(legacy_prefix):
                        offenders.append(f"{path.relative_to(BACKEND_ROOT)}:{node.lineno}")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == "legacy" or alias.name.startswith(legacy_prefix):
                            offenders.append(f"{path.relative_to(BACKEND_ROOT)}:{node.lineno}")

    assert offenders == []


def test_active_runtime_surfaces_do_not_reintroduce_removed_protocol_names() -> None:
    """Keep product code, CLI, frontend, and agent instructions on final names.

    Temporary physical-name compatibility is confined to SQL migrations; active
    runtime and its identifier constants use canonical Version Engine names.
    """

    old_protocol_upper = "".join(("M", "UT"))
    old_protocol_title = "".join(("M", "ut"))
    old_protocol_lower = "".join(("m", "ut"))
    banned_patterns = (
        (old_protocol_upper, re.compile(rf"\b{old_protocol_upper}\b")),
        (old_protocol_title, re.compile(rf"\b{old_protocol_title}\b")),
        (old_protocol_lower, re.compile(rf"\b{old_protocol_lower}\b")),
        (
            f"{old_protocol_lower}_engine",
            re.compile(re.escape(f"{old_protocol_lower}_engine")),
        ),
        (f"/{old_protocol_lower}/ap", re.compile(re.escape(f"/{old_protocol_lower}/ap"))),
        (f"X-{old_protocol_title}-User", re.compile(re.escape(f"X-{old_protocol_title}-User"))),
        (
            "_".join(("permanent", "delete")),
            re.compile(re.escape("_".join(("permanent", "delete")))),
        ),
        (
            "_".join(("active", "access", "point")),
            re.compile(re.escape("_".join(("active", "access", "point")))),
        ),
        (
            "_".join(("legacy", "access", "point")),
            re.compile(re.escape("_".join(("legacy", "access", "point")))),
        ),
    )
    suffixes = {".md", ".py", ".js", ".jsx", ".ts", ".tsx"}
    offenders: list[str] = []

    for rel_root in ACTIVE_RUNTIME_SCAN_ROOTS:
        root = REPO_ROOT / rel_root
        paths = [root] if root.is_file() else root.rglob("*")
        for path in paths:
            if not path.is_file() or path.suffix not in suffixes:
                continue
            rel = str(path.relative_to(REPO_ROOT))
            if rel in ALLOWED_DEFERRED_DB_NAME_FILES:
                continue
            text = path.read_text(encoding="utf-8")
            for line_no, line in enumerate(text.splitlines(), start=1):
                for label, pattern in banned_patterns:
                    if pattern.search(line):
                        offenders.append(f"{rel}:{line_no}:{label}")

    assert offenders == []


def test_runtime_no_longer_uses_scope_publish_path() -> None:
    """Root-first writes must not regress to the old scope-authoritative RPC."""

    banned = (
        "publish_scope_update",
        "publish_scope_promotion",
        "PUBLISH_SCOPE_UPDATE_RPC",
        "ScopePromoteIntent",
        "parent_scope_promote",
    )
    suffixes = {".py"}
    offenders: list[str] = []

    for root in (
        BACKEND_ROOT / "src",
        BACKEND_ROOT / "scripts",
        BACKEND_ROOT / "tests" / "version_engine",
    ):
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in suffixes:
                continue
            rel = path.relative_to(BACKEND_ROOT)
            if rel == Path("tests/version_engine/test_git_kernel_migration_contracts.py"):
                continue
            text = path.read_text(encoding="utf-8")
            for line_no, line in enumerate(text.splitlines(), start=1):
                for token in banned:
                    if token in line:
                        offenders.append(f"{rel}:{line_no}:{token}")

    assert offenders == []


def test_core_no_longer_imports_removed_protocol_normalize_path() -> None:
    offenders: list[str] = []
    removed_protocol_module = "".join(("m", "ut.core.protocol"))

    for path in (BACKEND_ROOT / "src").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == removed_protocol_module
                and any(alias.name == "normalize_path" for alias in node.names)
            ):
                offenders.append(f"{path.relative_to(BACKEND_ROOT)}:{node.lineno}")

    assert offenders == []


def test_access_point_auth_uses_repo_scopes_without_legacy_fallback() -> None:
    checked = (
        BACKEND_ROOT / "src/version_engine/entrypoints/http/access_point.py",
        BACKEND_ROOT / "src/version_engine/admission/identity.py",
    )

    offenders = [
        str(path.relative_to(BACKEND_ROOT))
        for path in checked
        if '.table("access_points")' in path.read_text(encoding="utf-8")
    ]

    assert offenders == []


def test_version_storage_uses_canonical_physical_names() -> None:
    from src.version_engine.infrastructure.supabase import db_names

    values = {
        value for name, value in vars(db_names).items() if name.isupper() and isinstance(value, str)
    }
    assert values
    assert all(not value.startswith("mut_") for value in values)
    assert all("_mut_" not in value for value in values)

    migration = (
        REPO_ROOT
        / "supabase/archive/before_b1/migrations/20260711040000_version_physical_rename.sql"
    ).read_text(encoding="utf-8")
    for table in (
        "version_commits",
        "version_scope_state",
        "version_view_commits",
        "version_outbox",
        "version_conflicts",
        "version_object_locations",
    ):
        assert f"RENAME TO {table}" in migration
    assert "sync_project_version_columns" in migration
    assert "sync_github_version_columns" in migration
    assert "FROM PUBLIC,anon,authenticated" in migration


def test_access_tables_are_confined_to_repository_boundaries() -> None:
    table_names = {
        "access_surfaces",
        "repository_scopes",
        "repo_scopes",
        "access_tools",
    }
    offenders: list[str] = []
    for path in (BACKEND_ROOT / "src").rglob("*.py"):
        rel = path.relative_to(BACKEND_ROOT).as_posix()
        allowed = (
            "/repository.py" in rel
            or rel.endswith("_repository.py")
            or "/supabase/" in rel
            or rel.endswith("access_credentials.py")
            or rel.endswith("access_surface_repository.py")
            or rel.endswith("scope_repository.py")
        )
        if allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "table" or not node.args:
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and arg.value in table_names:
                offenders.append(f"{rel}:{node.lineno}:{arg.value}")
    assert offenders == []


def test_scope_credentials_are_hash_only_access_surface_credentials() -> None:
    scope_repo = (BACKEND_ROOT / "src/repo/scope_repository.py").read_text(encoding="utf-8")
    surface_repo = (BACKEND_ROOT / "src/platform/access/surface_repository.py").read_text(
        encoding="utf-8"
    )
    assert '.eq("access_key"' not in scope_repo
    assert '"access_key": access_key' not in scope_repo
    assert "resolve_scope_credential" in surface_repo
    assert "store_scope_credential" in surface_repo

    access_router = (BACKEND_ROOT / "src/platform/access/router.py").read_text(encoding="utf-8")
    assert "_access_key_for" not in access_router

    migration = (
        REPO_ROOT
        / "supabase/archive/before_b1/migrations/20260711070000_move_scope_credentials_to_access_credentials.sql"
    ).read_text(encoding="utf-8")
    assert "INSERT INTO public.access_surface_credentials" in migration
    assert "legacy credential backfill is incomplete" in migration
    for column in ("access_key", "access_key_hash", "access_key_revoked_at"):
        assert f"DROP COLUMN IF EXISTS {column}" in migration
    assert "NOT (config ? 'access_key')" in migration

    cutover = (
        REPO_ROOT
        / "supabase/archive/before_b1/migrations/20260715000000_project_owned_repository_targets_contract_cutover.sql"
    ).read_text(encoding="utf-8")
    assert "legacy Scope credential columns remain" in cutover

    assert not (BACKEND_ROOT / "scripts/migrate_access_points_to_v2.py").exists()


def test_object_gc_has_durable_recovery_window_and_metrics() -> None:
    migration = (
        REPO_ROOT
        / "supabase/archive/before_b1/migrations/20260711080000_object_gc_quarantine_and_metrics.sql"
    ).read_text(encoding="utf-8")
    assert "version_object_gc_candidates" in migration
    assert "version_object_gc_runs" in migration
    assert "sync_version_object_gc_candidates" in migration
    assert "first_seen_at" in migration
    assert "make_interval" in migration
    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "FROM PUBLIC, anon, authenticated" in migration

    worker = (BACKEND_ROOT / "src/version_engine/derived/object_gc_worker.py").read_text(
        encoding="utf-8"
    )
    assert "VERSION_OBJECT_GC_QUARANTINE_SECONDS" in worker
    assert "version_object_gc_runs" in worker


def test_object_gc_sync_rpc_ambiguity_has_forward_fix_and_execution_smoke() -> None:
    fix = (
        REPO_ROOT
        / "supabase/archive/before_b1/migrations/20260711130000_fix_object_gc_candidate_sync_ambiguity.sql"
    ).read_text(encoding="utf-8")
    assert "RETURNS TABLE(object_id text)" in fix
    assert "ON CONFLICT ON CONSTRAINT version_object_gc_candidates_pkey" in fix
    assert "ON CONFLICT (project_id, object_id)" not in fix

    smoke_adapter = REPO_ROOT / "supabase/tests/smoke_test_triggers.sql"
    smoke_contracts = REPO_ROOT / "supabase/tests/_support/schema_contracts.inc"
    smoke = smoke_adapter.read_text(encoding="utf-8") + smoke_contracts.read_text(encoding="utf-8")
    assert r"\ir _support/schema_contracts.inc" in smoke_adapter.read_text(encoding="utf-8")
    assert "sync_version_object_gc_candidates" in smoke
    assert "__object_gc_rpc_smoke__" in smoke
    assert "ROLLBACK;" in smoke


def test_irrecoverable_root_incidents_are_private_and_durable() -> None:
    migration = (
        REPO_ROOT
        / "supabase/archive/before_b1/migrations/20260711140000_version_project_root_integrity_incidents.sql"
    ).read_text(encoding="utf-8")
    assert "version_project_root_integrity_incidents" in migration
    assert "REFERENCES public.projects(id) ON DELETE CASCADE" in migration
    assert "^[0-9a-f]{40}$" in migration
    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "FOR ALL TO service_role" in migration
    assert "FROM PUBLIC, anon, authenticated" in migration

    repair = (BACKEND_ROOT / "scripts/repair_missing_project_roots.py").read_text(encoding="utf-8")
    assert "mark_project_root_irrecoverable" in repair
    assert (
        "cas_update_root_hash"
        not in repair.split("def mark_project_root_irrecoverable", 1)[1].split(
            "def _project_ids", 1
        )[0]
    )


def test_legacy_mcp_and_sandbox_runtime_packages_are_retired() -> None:
    legacy_mcp = BACKEND_ROOT / "src/infra/mcp_server"
    assert not legacy_mcp.exists() or not any(legacy_mcp.glob("*.py"))
    legacy_sandbox = BACKEND_ROOT / "src/infra/sandbox"
    assert not legacy_sandbox.exists() or not any(legacy_sandbox.glob("*.py"))

    migration = (
        REPO_ROOT / "supabase/archive/before_b1/migrations/20260711050000_retire_legacy_mcps.sql"
    ).read_text(encoding="utf-8")
    assert "INSERT INTO public.access_surfaces" in migration
    assert "DROP TABLE IF EXISTS public.mcps CASCADE" in migration

    config = (BACKEND_ROOT / "src/config.py").read_text(encoding="utf-8")
    assert 'SCOPE_SANDBOX_STORE: Literal["memory", "supabase"] = "supabase"' in config


def test_mcp_transport_has_one_backend_runtime_and_one_binding_store() -> None:
    removed = (
        "mcp_service/cache.py",
        "mcp_service/core/config_loader.py",
        "mcp_service/tool/fs_tool.py",
        "mcp_service/tool/table_tool.py",
        "script/migrate_mcp_to_v2.py",
    )
    for relative in removed:
        assert not (BACKEND_ROOT / relative).exists()

    transport = (BACKEND_ROOT / "mcp_service/server.py").read_text(encoding="utf-8")
    rpc_client = (BACKEND_ROOT / "mcp_service/rpc/client.py").read_text(encoding="utf-8")
    assert "list_mcp_runtime_tools" in transport
    assert "call_mcp_runtime_tool" in transport
    for legacy_symbol in (
        "load_mcp_config",
        "FsToolImplementation",
        "TableToolImplementation",
        "_build_agent_tools_list",
        "node_{idx}",
    ):
        assert legacy_symbol not in transport
    assert "/internal/mcp-runtime/tools" in rpc_client
    assert "/internal/mcp-runtime/call" in rpc_client
    assert "/internal/nodes/" not in rpc_client
    assert "/internal/tables/" not in rpc_client

    migration = (
        REPO_ROOT
        / "supabase/archive/before_b1/migrations/20260711060000_unify_mcp_tool_bindings.sql"
    ).read_text(encoding="utf-8")
    assert "idx_access_tools_surface_tool_unique" in migration
    assert "replace_mcp_surface_policy" in migration
    assert "DELETE FROM public.access_tools" in migration
    assert "REVOKE ALL ON FUNCTION" in migration

    guide = (BACKEND_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "MCP 四层职责与唯一数据路径" in guide
    assert "version_engine/scoped_fs" in guide


def test_version_alias_views_are_not_exposed_as_rls_bypasses() -> None:
    migration = (
        REPO_ROOT
        / "supabase/archive/before_b1/migrations/20260711020000_secure_version_alias_views.sql"
    ).read_text(encoding="utf-8")
    views = (
        "version_commits",
        "version_scope_state",
        "version_view_commits",
        "version_outbox",
        "version_conflicts",
        "version_object_locations",
    )

    for view in views:
        assert f"ALTER VIEW IF EXISTS public.{view}" in migration
        assert "security_invoker = true" in migration
        assert f"REVOKE ALL ON public.{view} FROM PUBLIC, anon, authenticated;" in migration
    assert "GRANT SELECT ON public.version_commits TO authenticated" not in migration


def test_product_write_path_does_not_import_git_transport_materialization() -> None:
    offenders: list[str] = []
    banned_modules = {
        "src.version_engine.adapters.git.object_quarantine",
        "src.version_engine.adapters.git.upload_pack",
        "src.version_engine.adapters.git.receive_pack",
    }
    banned_names = {
        "temporary_bare_repo",
        "temporary_transport_bare_repo",
        "copy_reachable_objects_to_bare",
        "copy_store_objects_to_bare",
        "quarantine_pack",
    }

    for rel in PRODUCT_WRITE_MODULES:
        path = BACKEND_ROOT / rel
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                imported = {alias.name for alias in node.names}
                if module in banned_modules:
                    offenders.append(f"{rel}:{node.lineno}:{module}")
                if imported & banned_names:
                    offenders.append(
                        f"{rel}:{node.lineno}:{','.join(sorted(imported & banned_names))}"
                    )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in banned_modules:
                        offenders.append(f"{rel}:{node.lineno}:{alias.name}")

    assert offenders == []


def test_product_write_path_does_not_depend_on_git_view_projection() -> None:
    offenders: list[str] = []
    banned_names = {
        "git_compatible_head_commit",
        "git_view_head_commit",
    }

    for rel in PRODUCT_WRITE_MODULES:
        path = BACKEND_ROOT / rel
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                imported = {alias.name for alias in node.names}
                if module == "src.version_engine.adapters.git.view_projection":
                    offenders.append(f"{rel}:{node.lineno}:{module}")
                if imported & banned_names:
                    offenders.append(
                        f"{rel}:{node.lineno}:{','.join(sorted(imported & banned_names))}"
                    )

    assert offenders == []
