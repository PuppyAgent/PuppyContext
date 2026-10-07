import json
from types import SimpleNamespace
from typing import ClassVar

import pytest
from fastapi import HTTPException

from src.platform.authorization.models import RuntimeGrant, RuntimeMode, RuntimePrincipal
from src.platform.repository_target.models import (
    ProjectRootTarget,
    ResolvedRepositoryView,
    ScopeTarget,
)
from src.version_engine.adapters.product.commands import VersionWriteCommandService
from src.version_engine.entrypoints.http import access_point_fs as apfs
from src.version_engine.entrypoints.http.access_point_fs import _filter_entries
from src.version_engine.infrastructure.supabase.scope_repository import SupabaseScopeBackend
from src.version_engine.storage.object_store import StorageBackend


def _entry(path: str, type_: str = "file", size_bytes: int | None = None):
    name = path.rsplit("/", 1)[-1]
    return SimpleNamespace(
        path=path,
        name=name,
        type=type_,
        content_hash=None,
        size_bytes=size_bytes,
        mime_type=None,
        children_count=None,
    )


class _RangeBackend(StorageBackend):
    def __init__(self):
        self.objects = {}
        self.ranges = []

    def get(self, h: str) -> bytes:
        return self.objects[h]

    def get_range(self, h: str, start: int = 0, limit: int | None = None):
        self.ranges.append({"hash": h, "start": start, "limit": limit})
        raw = self.objects[h]
        end = len(raw) if limit is None else min(len(raw), start + limit)
        return raw[start:end], len(raw)

    def put(self, h: str, data: bytes) -> None:
        self.objects[h] = data

    def exists(self, h: str) -> bool:
        return h in self.objects

    def all_hashes(self) -> list[str]:
        return list(self.objects)

    def count(self) -> tuple[int, int]:
        return len(self.objects), sum(len(v) for v in self.objects.values())

    def delete(self, h: str) -> bool:
        return self.objects.pop(h, None) is not None


class _BulkExistsBackend(StorageBackend):
    def __init__(self):
        self.objects = {}
        self.exists_calls = []
        self.exists_many_calls = []

    def get(self, h: str) -> bytes:
        return self.objects[h]

    def put(self, h: str, data: bytes) -> None:
        self.objects[h] = data

    def exists(self, h: str) -> bool:
        self.exists_calls.append(h)
        return h in self.objects

    def exists_many(self, hashes: list[str]) -> set[str]:
        self.exists_many_calls.append(list(hashes))
        return {h for h in hashes if h in self.objects}

    def all_hashes(self) -> list[str]:
        return list(self.objects)

    def count(self) -> tuple[int, int]:
        return len(self.objects), sum(len(v) for v in self.objects.values())

    def delete(self, h: str) -> bool:
        return self.objects.pop(h, None) is not None


def _patch_auth(monkeypatch, scope_path: str = ""):
    if scope_path:
        target = ScopeTarget(project_id="project-id", scope_id="test-scope")
    else:
        target = ProjectRootTarget(project_id="project-id")
    view = ResolvedRepositoryView(
        target=target,
        path_prefix=scope_path.strip("/"),
        excludes=(),
        max_mode="rw",
    )
    monkeypatch.setattr(
        apfs,
        "resolve_access_point",
        lambda _key: (
            "project-id",
            {
                "agent": "test-ap",
                "_runtime_grant": RuntimeGrant(
                    principal=RuntimePrincipal(
                        principal_id="test-credential",
                        credential_kind="test",
                    ),
                    target=target,
                    repository_view=view,
                    mode=RuntimeMode.READ_WRITE,
                ),
            },
        ),
    )
    monkeypatch.setattr(SupabaseScopeBackend, "list_all_strict", lambda _self: [])
    monkeypatch.setattr(apfs, "admit_cli_fs_command", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        apfs,
        "_run_grep_indexed_payload",
        lambda **_kwargs: {"index_status": "missing"},
    )
    monkeypatch.setattr(
        apfs,
        "_upload_max_bytes_for_project",
        lambda _project_id: apfs.POLICY_PER_FILE_MAX_BYTES,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler_name", "expected_command", "kwargs"),
    [
        ("list_dir", "ls", {}),
        ("semantics", "semantics", {}),
        ("tree", "tree", {}),
        ("grep", "grep", {"pattern": "needle"}),
        (
            "grep_indexed",
            "grep",
            {"body": apfs._GrepIndexedRequest(pattern="needle")},
        ),
        ("read_file", "cat", {"path": "notes.md"}),
        ("raw_file", "download", {"path": "notes.md"}),
        ("upload_file", "upload", {"request": None, "path": "notes.md"}),
        ("stat", "stat", {"path": "notes.md"}),
        ("write_file", "write", {"body": apfs.WriteFileRequest(path="notes.md", content="hi")}),
        ("mkdir", "mkdir", {"body": apfs.MkdirRequest(path="docs")}),
        ("touch", "touch", {"body": apfs.TouchRequest(path="notes.md")}),
        ("move", "mv", {"body": apfs.MoveRequest(old_path="a.md", new_path="b.md")}),
        ("copy", "cp", {"body": apfs.CopyRequest(old_path="a.md", new_path="b.md")}),
        ("rmdir", "rmdir", {"body": apfs.RmdirRequest(path="docs")}),
        ("remove", "rm", {"body": apfs.RemoveRequest(path="notes.md")}),
        ("find_index", "find", {"name": "*.md"}),
    ],
)
async def test_ap_fs_handlers_admit_expected_cli_command(
    monkeypatch,
    handler_name,
    expected_command,
    kwargs,
):
    seen_commands = []

    async def _capture_auth(*_args, **auth_kwargs):
        seen_commands.append(auth_kwargs.get("command"))
        raise HTTPException(status_code=499, detail="stop after auth")

    monkeypatch.setattr(apfs, "_resolve_auth", _capture_auth)

    with pytest.raises(HTTPException) as exc:
        await getattr(apfs, handler_name)(**kwargs)

    assert exc.value.status_code == 499
    assert seen_commands == [expected_command]


class _FakeOps:
    def for_grant(self, grant):
        assert isinstance(grant, RuntimeGrant)
        self.grant = grant
        return self

    def __init__(self, *, files=None, stats=None, listing=None, list_by_path=None, tree=None):
        self.files = files or {}
        self.stats = stats or {}
        self.listing = listing or []
        self.list_by_path = list_by_path or {}
        self.tree_entries = tree or []
        self.ranges = []
        self.mkdirs = []
        self.moves = []
        self.copies = []
        self.touches = []
        self.deletes = []
        self.writes = []
        self.stat_calls = []
        self.head_calls = 0
        self.scope_head_calls = 0

    def read_file(self, _project_id, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def read_file_in_scope(self, project_id, scope_path, path):
        full = f"{scope_path.strip('/')}/{path.strip('/')}".strip("/")
        if full in self.files:
            return self.files[full]
        return self.read_file(project_id, path.strip("/"))

    def read_file_range(self, _project_id, path, *, start=0, limit=None):
        if path not in self.files:
            raise FileNotFoundError(path)
        raw = self.files[path]
        self.ranges.append({"path": path, "start": start, "limit": limit})
        end = len(raw) if limit is None else min(len(raw), start + limit)
        return SimpleNamespace(
            content=raw[start:end],
            total_size=len(raw),
            content_hash="blob-hash",
            ranged=bool(start or limit is not None),
        )

    def stat(self, _project_id, path, *, include_size=False):
        self.stat_calls.append({"path": path, "include_size": include_size})
        return self.stats.get(path)

    def stat_in_scope(self, project_id, scope_path, path, *, include_size=False):
        full = f"{scope_path.strip('/')}/{path.strip('/')}".strip("/")
        self.stat_calls.append({"path": full, "include_size": include_size})
        return self.stats.get(full) or self.stats.get(path.strip("/"))

    def list_dir(self, _project_id, _path, *, include_size=False):
        if _path in self.list_by_path:
            return self.list_by_path[_path]
        return self.listing

    def list_dir_in_scope(self, project_id, scope_path, path, *, include_size=False):
        full = f"{scope_path.strip('/')}/{path.strip('/')}".strip("/")
        return self.list_dir(project_id, full, include_size=include_size)

    def list_tree(
        self,
        _project_id,
        _path,
        max_depth=-1,
        *,
        include_size=False,
        max_entries=None,
    ):
        if max_entries is None:
            return self.tree_entries
        return self.tree_entries[:max_entries]

    def list_tree_in_scope(
        self,
        project_id,
        scope_path,
        path,
        max_depth=-1,
        *,
        include_size=False,
        max_entries=None,
    ):
        full = f"{scope_path.strip('/')}/{path.strip('/')}".strip("/")
        return self.list_tree(
            project_id,
            full,
            max_depth=max_depth,
            include_size=include_size,
            max_entries=max_entries,
        )

    def get_head_commit_id(self, _project_id):
        self.head_calls += 1
        return "head"

    def get_scope_head_commit_id(self, _project_id, _scope_path):
        self.scope_head_calls += 1
        return "scope-head"

    def get_path_timestamps(self, _project_id, paths, *, limit=5000):
        return {
            p.strip("/"): {
                "created_at": "2026-01-01T00:00:00+00:00",
                "modified_at": "2026-01-02T00:00:00+00:00",
            }
            for p in paths
        }

    async def write_file(
        self,
        project_id,
        path,
        content,
        *,
        who,
        message,
        scope="",
        base_commit_id=None,
        defer_projection=False,
        **_kwargs,
    ):
        self.writes.append(
            {
                "project_id": project_id,
                "path": path,
                "content": content,
                "who": who,
                "scope": scope,
                "message": message,
                "base_commit_id": base_commit_id,
                "defer_projection": defer_projection,
            }
        )
        return SimpleNamespace(commit_id="write-commit")

    async def mkdir(
        self,
        project_id,
        path,
        *,
        who,
        message,
        scope="",
        base_commit_id=None,
        defer_projection=False,
        **_kwargs,
    ):
        self.mkdirs.append(
            {
                "project_id": project_id,
                "path": path,
                "who": who,
                "scope": scope,
                "message": message,
                "base_commit_id": base_commit_id,
                "defer_projection": defer_projection,
            }
        )
        return SimpleNamespace(commit_id="mkdir-commit")

    async def move(
        self,
        project_id,
        old_path,
        new_path,
        *,
        who,
        message,
        scope="",
        base_commit_id=None,
        defer_projection=False,
        **_kwargs,
    ):
        self.moves.append(
            {
                "project_id": project_id,
                "old_path": old_path,
                "new_path": new_path,
                "who": who,
                "scope": scope,
                "message": message,
                "base_commit_id": base_commit_id,
                "defer_projection": defer_projection,
            }
        )
        return SimpleNamespace(commit_id="move-commit")

    async def copy(
        self,
        project_id,
        old_path,
        new_path,
        *,
        who,
        message,
        scope="",
        base_commit_id=None,
        defer_projection=False,
        **_kwargs,
    ):
        self.copies.append(
            {
                "project_id": project_id,
                "old_path": old_path,
                "new_path": new_path,
                "who": who,
                "scope": scope,
                "message": message,
                "base_commit_id": base_commit_id,
                "defer_projection": defer_projection,
            }
        )
        return SimpleNamespace(commit_id="copy-commit")

    async def touch(
        self,
        project_id,
        paths,
        *,
        who,
        message,
        scope="",
        base_commit_id=None,
        defer_projection=False,
        **_kwargs,
    ):
        self.touches.append(
            {
                "project_id": project_id,
                "paths": paths,
                "who": who,
                "scope": scope,
                "message": message,
                "base_commit_id": base_commit_id,
                "defer_projection": defer_projection,
            }
        )
        return SimpleNamespace(commit_id="touch-commit")

    async def delete(
        self,
        project_id,
        paths,
        *,
        who,
        message,
        scope="",
        base_commit_id=None,
        defer_projection=False,
        **_kwargs,
    ):
        self.deletes.append(
            {
                "project_id": project_id,
                "paths": paths,
                "who": who,
                "scope": scope,
                "message": message,
                "base_commit_id": base_commit_id,
                "defer_projection": defer_projection,
            }
        )
        return SimpleNamespace(commit_id="delete-commit")


def _commands(ops: _FakeOps) -> VersionWriteCommandService:
    return VersionWriteCommandService(ops)


def test_filter_entries_hides_dot_entries_by_default():
    scope = {"path": "", "exclude": []}
    entries = [_entry("docs/readme.md"), _entry(".internal/readme_1.md")]

    visible = _filter_entries(entries, scope)

    assert [entry.path for entry in visible] == ["docs/readme.md"]


def test_filter_entries_can_include_dot_entries():
    scope = {"path": "", "exclude": []}
    entries = [_entry("docs/readme.md"), _entry(".internal/readme_1.md")]

    visible = _filter_entries(entries, scope, include_hidden=True)

    assert [entry.path for entry in visible] == [
        "docs/readme.md",
        ".internal/readme_1.md",
    ]


@pytest.mark.asyncio
async def test_cat_defaults_to_raw_text_for_json(monkeypatch):
    _patch_auth(monkeypatch)
    raw = b'{\n  "b": 1,\n  "a": 2\n}\n'
    result = await apfs.read_file(
        path="data.json",
        structured=False,
        x_access_key="key",
        x_puppyone_user=None,
        x_puppy_client="cli",
        ops=_FakeOps(files={"data.json": raw}),
    )

    assert result.data["content"] is None
    assert result.data["content_text"] == raw.decode("utf-8")


@pytest.mark.asyncio
async def test_cat_structured_mode_parses_json(monkeypatch):
    _patch_auth(monkeypatch)
    raw = b'{\n  "b": 1,\n  "a": 2\n}\n'
    result = await apfs.read_file(
        path="data.json",
        structured=True,
        x_access_key="key",
        x_puppyone_user=None,
        x_puppy_client="cli",
        ops=_FakeOps(files={"data.json": raw}),
    )

    assert result.data["content"] == {"b": 1, "a": 2}
    assert result.data["content_text"] is None


@pytest.mark.asyncio
async def test_raw_file_returns_unmodified_bytes(monkeypatch):
    _patch_auth(monkeypatch)
    raw = b"\x00raw\nbytes"

    result = await apfs.raw_file(
        path="blob.bin",
        start=0,
        limit=None,
        x_access_key="key",
        x_puppyone_user=None,
        x_puppy_client="cli",
        ops=_FakeOps(files={"blob.bin": raw}),
    )

    assert result.body == raw
    assert result.headers["content-length"] == str(len(raw))


@pytest.mark.asyncio
async def test_raw_file_can_return_byte_slice(monkeypatch):
    _patch_auth(monkeypatch)
    raw = b"abcdef"
    ops = _FakeOps(files={"blob.bin": raw})

    result = await apfs.raw_file(
        path="blob.bin",
        start=2,
        limit=3,
        x_access_key="key",
        x_puppyone_user=None,
        x_puppy_client="cli",
        ops=ops,
    )

    assert result.body == b"cde"
    assert result.headers["x-puppyone-size"] == "6"
    assert ops.ranges == [{"path": "blob.bin", "start": 2, "limit": 3}]


@pytest.mark.asyncio
async def test_upload_writes_raw_bytes_through_product_operation_adapter(monkeypatch):
    _patch_auth(monkeypatch)
    monkeypatch.setattr(apfs, "_upload_max_bytes_for_project", lambda _project_id: 1024 * 1024)
    ops = _FakeOps()

    class _Request:
        headers: ClassVar[dict] = {}

        async def body(self):
            return b"\x00bytes"

    result = await apfs.upload_file(
        request=_Request(),
        path="bin.dat",
        base_commit_id="base",
        message="upload",
        x_access_key="key",
        x_puppyone_user=None,
        x_puppy_client="cli",
        commands=_commands(ops),
    )

    assert result.data["commit_id"] == "write-commit"
    assert result.data["size_bytes"] == 6
    assert ops.writes[0]["path"] == "bin.dat"
    assert ops.writes[0]["content"] == b"\x00bytes"
    assert ops.writes[0]["defer_projection"] is True


@pytest.mark.asyncio
async def test_write_defers_projection_for_cli_latency(monkeypatch):
    _patch_auth(monkeypatch)
    ops = _FakeOps()

    result = await apfs.write_file(
        apfs.WriteFileRequest(
            path="note.md",
            content="hello",
            node_type="markdown",
            base_commit_id="base",
        ),
        x_access_key="key",
        x_puppyone_user=None,
        x_puppy_client="cli",
        commands=_commands(ops),
    )

    assert result.data["commit_id"] == "write-commit"
    assert ops.writes[0]["path"] == "note.md"
    assert ops.writes[0]["base_commit_id"] == "base"
    assert ops.writes[0]["defer_projection"] is True


@pytest.mark.asyncio
async def test_root_stat_uses_scope_head_fast_path(monkeypatch):
    _patch_auth(monkeypatch, scope_path="docs")
    ops = _FakeOps(stats={"docs": _entry("docs", "folder")})

    result = await apfs.stat(
        path="",
        x_access_key="key",
        x_puppyone_user=None,
        x_puppy_client="cli",
        ops=ops,
    )

    assert result.data["exists"] is True
    assert result.data["type"] == "folder"
    assert result.data["path"] == ""
    assert result.data["version_path"] == "docs"
    assert result.data["head_commit_id"] == "scope-head"
    assert result.data["scope_head_commit_id"] == "scope-head"
    assert result.data["metadata_source"] == "scope_state"
    assert ops.scope_head_calls == 1
    assert ops.head_calls == 0
    assert ops.stat_calls == []


@pytest.mark.asyncio
async def test_ls_file_returns_the_file_itself(monkeypatch):
    _patch_auth(monkeypatch)
    file_entry = _entry("docs/readme.md", "markdown", size_bytes=12)
    result = await apfs.list_dir(
        path="docs/readme.md",
        include_size=True,
        x_access_key="key",
        x_puppyone_user=None,
        x_puppy_client="cli",
        ops=_FakeOps(stats={"docs/readme.md": file_entry}),
    )

    assert result.data["target_type"] == "markdown"
    assert [entry["path"] for entry in result.data["entries"]] == ["docs/readme.md"]


@pytest.mark.asyncio
async def test_ls_missing_path_raises_not_found(monkeypatch):
    _patch_auth(monkeypatch)

    with pytest.raises(HTTPException) as exc:
        await apfs.list_dir(
            path="missing",
            x_access_key="key",
            x_puppyone_user=None,
            x_puppy_client="cli",
            ops=_FakeOps(),
        )

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_tree_directories_only_filters_files(monkeypatch):
    _patch_auth(monkeypatch)
    result = await apfs.tree(
        path="",
        directories_only=True,
        x_access_key="key",
        x_puppyone_user=None,
        x_puppy_client="cli",
        ops=_FakeOps(
            stats={"": _entry("", "folder")},
            tree=[
                _entry("docs", "folder"),
                _entry("docs/readme.md", "markdown"),
                _entry("src", "folder"),
            ],
        ),
    )

    assert result.data["directories_only"] is True
    assert [entry["path"] for entry in result.data["entries"]] == ["docs", "src"]


@pytest.mark.asyncio
async def test_tree_limit_marks_truncated(monkeypatch):
    _patch_auth(monkeypatch)
    result = await apfs.tree(
        path="",
        limit=2,
        x_access_key="key",
        x_puppyone_user=None,
        x_puppy_client="cli",
        ops=_FakeOps(
            stats={"": _entry("", "folder")},
            tree=[
                _entry("a.txt", "file"),
                _entry("b.txt", "file"),
                _entry("c.txt", "file"),
            ],
        ),
    )

    assert result.data["limit"] == 2
    assert result.data["truncated"] is True
    assert result.data["complete"] is False
    assert result.data["returned_count"] == 2
    assert result.data["truncation_reason"] == "entry_limit_exceeded"
    assert [entry["path"] for entry in result.data["entries"]] == ["a.txt", "b.txt"]


@pytest.mark.asyncio
async def test_tree_missing_path_raises_not_found(monkeypatch):
    _patch_auth(monkeypatch)

    with pytest.raises(HTTPException) as exc:
        await apfs.tree(
            path="missing",
            x_access_key="key",
            x_puppyone_user=None,
            x_puppy_client="cli",
            ops=_FakeOps(),
        )

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_semantics_returns_backend_scoped_fs_capabilities(monkeypatch):
    _patch_auth(monkeypatch, scope_path="docs")

    result = await apfs.semantics(
        x_access_key="key",
        x_puppyone_user=None,
        x_puppy_client="cli",
    )

    assert result.data["scope"]["path"] == "docs"
    assert result.data["fs_semantics"]["summary"]
    assert "grep_guidance" in result.data["fs_semantics"]
    assert any(tool["name"] == "fs_grep" for tool in result.data["tools"])


@pytest.mark.asyncio
async def test_find_uses_canonical_backend_conditions(monkeypatch):
    _patch_auth(monkeypatch)

    result = await apfs.find_index(
        path="docs",
        conditions=json.dumps(
            [
                {"kind": "name", "value": "*.md", "negate": False},
                {"kind": "path", "value": "docs/tmp/*", "negate": True},
            ]
        ),
        mindepth=1,
        max_depth=2,
        limit=10,
        include_hidden=True,
        x_access_key="key",
        x_puppyone_user=None,
        x_puppy_client="cli",
        ops=_FakeOps(
            stats={"docs": _entry("docs", "folder")},
            tree=[
                _entry("docs/a.md", "markdown"),
                _entry("docs/tmp/a.md", "markdown"),
                _entry("docs/readme.txt", "file"),
            ],
        ),
    )

    assert result.data["source"] == "live_tree"
    assert result.data["path"] == "docs"
    assert result.data["entries"] == [
        {
            "name": "a.md",
            "path": "docs/a.md",
            "type": "markdown",
            "content_hash": None,
            "size_bytes": None,
            "mime_type": None,
            "children_count": None,
            "integrity_status": None,
            "created_at": None,
            "modified_at": None,
        }
    ]


@pytest.mark.asyncio
async def test_grep_uses_authoritative_indexed_backend(monkeypatch):
    _patch_auth(monkeypatch)

    def _indexed_payload(**kwargs):
        body = kwargs["body"]
        return {
            "scope": "docs",
            "pattern": body.pattern,
            "regex": body.regex,
            "ignore_case": body.ignore_case,
            "invert_match": body.invert_match,
            "only_matching": body.only_matching,
            "limit": body.limit,
            "per_file_limit": body.per_file_limit,
            "candidate_limit": body.candidate_limit,
            "candidates_examined": 1,
            "hits": [
                {
                    "path": "docs/a.md",
                    "line": 2,
                    "col": 7,
                    "match": "hello indexed",
                    "context_before": ["before"],
                    "context_after": ["after"],
                    "content_hash": "blob-hash",
                }
            ],
            "truncated": False,
            "index_status": "indexed",
            "index_freshness": {
                "indexed_commit_id": "head",
                "head_commit_id": "head",
                "commits_behind": 0,
            },
            "head_commit_id": "head",
        }

    monkeypatch.setattr(apfs, "_run_grep_indexed_payload", _indexed_payload)
    result = await apfs.grep(
        pattern="hello",
        path="docs",
        regex=False,
        ignore_case=False,
        include_hidden=False,
        limit=10,
        max_files=10,
        max_bytes=1000,
        x_access_key="key",
        x_puppyone_user=None,
        x_puppy_client="cli",
        ops=_FakeOps(stats={"docs": _entry("docs", "folder")}),
    )

    assert result.data["search_backend"] == "indexed"
    assert result.data["index_status"] == "indexed"
    assert result.data["returned_count"] == 1
    assert result.data["matches"][0]["path"] == "docs/a.md"
    assert result.data["matches"][0]["line_text"] == "hello indexed"
    assert result.data["files"] == [
        {
            "path": "docs/a.md",
            "version_path": "docs/a.md",
            "match_count": 1,
            "content_hash": "blob-hash",
        }
    ]
    assert result.data["scanned_files"] == 0


@pytest.mark.asyncio
async def test_grep_require_file_list_skips_indexed_backend(monkeypatch):
    _patch_auth(monkeypatch)

    def _unexpected_index(**_kwargs):
        raise AssertionError("indexed grep should not run when a complete file list is required")

    monkeypatch.setattr(apfs, "_run_grep_indexed_payload", _unexpected_index)
    result = await apfs.grep(
        pattern="hello",
        path="",
        regex=False,
        ignore_case=False,
        include_hidden=False,
        require_file_list=True,
        limit=10,
        max_files=10,
        max_bytes=1000,
        x_access_key="key",
        x_puppyone_user=None,
        x_puppy_client="cli",
        ops=_FakeOps(
            stats={"": _entry("", "folder")},
            tree=[
                _entry("a.md", "markdown"),
                _entry("b.md", "markdown"),
            ],
            files={
                "a.md": b"hello\n",
                "b.md": b"no match\n",
            },
        ),
    )

    assert "search_backend" not in result.data
    assert result.data["require_file_list"] is True
    assert result.data["files"] == [
        {
            "path": "a.md",
            "version_path": "a.md",
            "match_count": 1,
            "content_hash": None,
        },
        {
            "path": "b.md",
            "version_path": "b.md",
            "match_count": 0,
            "content_hash": None,
        },
    ]


@pytest.mark.asyncio
async def test_grep_indexed_backend_still_applies_walk_filters(monkeypatch):
    _patch_auth(monkeypatch)

    def _indexed_payload(**_kwargs):
        return {
            "scope": "",
            "pattern": "needle",
            "regex": False,
            "ignore_case": False,
            "invert_match": False,
            "only_matching": False,
            "limit": 10,
            "per_file_limit": 0,
            "candidate_limit": 10,
            "candidates_examined": 5,
            "hits": [
                {
                    "path": "docs/keep.md",
                    "line": 1,
                    "col": 1,
                    "match": "needle keep",
                    "context_before": [],
                    "context_after": [],
                    "content_hash": "keep-hash",
                },
                {
                    "path": ".hidden/skip.md",
                    "line": 1,
                    "col": 1,
                    "match": "needle hidden",
                    "context_before": [],
                    "context_after": [],
                    "content_hash": "hidden-hash",
                },
                {
                    "path": "docs/secret.md",
                    "line": 1,
                    "col": 1,
                    "match": "needle secret",
                    "context_before": [],
                    "context_after": [],
                    "content_hash": "secret-hash",
                },
                {
                    "path": "vendor/keep.md",
                    "line": 1,
                    "col": 1,
                    "match": "needle vendor",
                    "context_before": [],
                    "context_after": [],
                    "content_hash": "vendor-hash",
                },
                {
                    "path": "docs/not-md.txt",
                    "line": 1,
                    "col": 1,
                    "match": "needle txt",
                    "context_before": [],
                    "context_after": [],
                    "content_hash": "txt-hash",
                },
            ],
            "truncated": False,
            "index_status": "indexed",
            "index_freshness": {
                "indexed_commit_id": "head",
                "head_commit_id": "head",
                "commits_behind": 0,
            },
            "head_commit_id": "head",
        }

    monkeypatch.setattr(apfs, "_run_grep_indexed_payload", _indexed_payload)
    result = await apfs.grep(
        pattern="needle",
        path="",
        regex=False,
        ignore_case=False,
        include_hidden=False,
        include="*.md",
        exclude="secret*",
        exclude_dir="vendor",
        limit=10,
        max_files=10,
        max_bytes=1000,
        x_access_key="key",
        x_puppyone_user=None,
        x_puppy_client="cli",
        ops=_FakeOps(stats={"": _entry("", "folder")}),
    )

    assert result.data["search_backend"] == "indexed"
    assert [match["path"] for match in result.data["matches"]] == ["docs/keep.md"]
    assert result.data["matched_files"] == 1
    assert result.data["skipped"]["filtered"] == 4


@pytest.mark.asyncio
async def test_grep_indexed_backend_honors_max_depth(monkeypatch):
    _patch_auth(monkeypatch)

    def _indexed_payload(**_kwargs):
        return {
            "scope": "docs",
            "pattern": "needle",
            "regex": False,
            "ignore_case": False,
            "invert_match": False,
            "only_matching": False,
            "limit": 10,
            "per_file_limit": 0,
            "candidate_limit": 10,
            "candidates_examined": 2,
            "hits": [
                {
                    "path": "docs/keep.md",
                    "line": 1,
                    "col": 1,
                    "match": "needle keep",
                    "context_before": [],
                    "context_after": [],
                    "content_hash": "keep-hash",
                },
                {
                    "path": "docs/deep/skip.md",
                    "line": 1,
                    "col": 1,
                    "match": "needle deep",
                    "context_before": [],
                    "context_after": [],
                    "content_hash": "deep-hash",
                },
            ],
            "truncated": False,
            "index_status": "indexed",
            "index_freshness": {
                "indexed_commit_id": "head",
                "head_commit_id": "head",
                "commits_behind": 0,
            },
            "head_commit_id": "head",
        }

    monkeypatch.setattr(apfs, "_run_grep_indexed_payload", _indexed_payload)
    result = await apfs.grep(
        pattern="needle",
        path="docs",
        regex=False,
        ignore_case=False,
        include_hidden=True,
        max_depth=0,
        limit=10,
        max_files=10,
        max_bytes=1000,
        x_access_key="key",
        x_puppyone_user=None,
        x_puppy_client="cli",
        ops=_FakeOps(stats={"docs": _entry("docs", "folder")}),
    )

    assert result.data["search_backend"] == "indexed"
    assert [match["path"] for match in result.data["matches"]] == ["docs/keep.md"]
    assert result.data["skipped"]["filtered"] == 1


@pytest.mark.asyncio
async def test_grep_stale_index_falls_back_to_live_scan(monkeypatch):
    _patch_auth(monkeypatch)

    monkeypatch.setattr(
        apfs,
        "_run_grep_indexed_payload",
        lambda **_kwargs: {
            "index_status": "stale",
            "hits": [
                {
                    "path": "stale.md",
                    "line": 1,
                    "col": 1,
                    "match": "stale",
                    "context_before": [],
                    "context_after": [],
                    "content_hash": "stale-hash",
                }
            ],
        },
    )
    result = await apfs.grep(
        pattern="fresh",
        path="",
        regex=False,
        ignore_case=False,
        include_hidden=False,
        limit=10,
        max_files=10,
        max_bytes=1000,
        x_access_key="key",
        x_puppyone_user=None,
        x_puppy_client="cli",
        ops=_FakeOps(
            stats={"": _entry("", "folder")},
            tree=[_entry("fresh.md", "markdown")],
            files={"fresh.md": b"fresh\n"},
        ),
    )

    assert "search_backend" not in result.data
    assert result.data["scanned_files"] == 1
    assert [match["path"] for match in result.data["matches"]] == ["fresh.md"]


@pytest.mark.asyncio
async def test_grep_live_scans_text_files(monkeypatch):
    _patch_auth(monkeypatch)
    result = await apfs.grep(
        pattern="hello",
        path="",
        regex=False,
        ignore_case=False,
        include_hidden=False,
        max_depth=-1,
        limit=10,
        max_files=10,
        max_bytes=1000,
        x_access_key="key",
        x_puppyone_user=None,
        x_puppy_client="cli",
        ops=_FakeOps(
            stats={"": _entry("", "folder")},
            tree=[
                _entry("README.md", "markdown"),
                _entry("image.png", "file"),
            ],
            files={"README.md": b"hello\nworld\nhello again\n"},
        ),
    )

    assert result.data["complete"] is True
    assert result.data["returned_count"] == 2
    assert result.data["scanned_files"] == 1
    assert result.data["skipped"]["non_text"] == 1
    assert [m["line_number"] for m in result.data["matches"]] == [1, 3]
    assert result.data["matches"][1]["line_text"] == "hello again"
    assert result.data["head_commit_id"] == "scope-head"


@pytest.mark.asyncio
async def test_grep_regex_and_ignore_case(monkeypatch):
    _patch_auth(monkeypatch)
    result = await apfs.grep(
        pattern=r"hello\s+\w+",
        path="README.md",
        regex=True,
        ignore_case=True,
        include_hidden=False,
        max_depth=-1,
        limit=10,
        max_files=10,
        max_bytes=1000,
        x_access_key="key",
        x_puppyone_user=None,
        x_puppy_client="cli",
        ops=_FakeOps(
            stats={"README.md": _entry("README.md", "markdown")},
            files={"README.md": b"HELLO PuppyOne\nbye\n"},
        ),
    )

    assert result.data["returned_count"] == 1
    assert result.data["matches"][0]["match_start"] == 0
    assert result.data["matches"][0]["line_text"] == "HELLO PuppyOne"


@pytest.mark.asyncio
async def test_grep_result_limit_marks_truncated(monkeypatch):
    _patch_auth(monkeypatch)
    result = await apfs.grep(
        pattern="x",
        path="",
        regex=False,
        ignore_case=False,
        include_hidden=False,
        max_depth=-1,
        limit=1,
        max_files=10,
        max_bytes=1000,
        x_access_key="key",
        x_puppyone_user=None,
        x_puppy_client="cli",
        ops=_FakeOps(
            stats={"": _entry("", "folder")},
            tree=[_entry("a.md", "markdown", size_bytes=None)],
            files={"a.md": b"x\nx\n"},
        ),
    )

    assert result.data["complete"] is False
    assert result.data["truncated"] is True
    assert result.data["truncation_reason"] == "result_limit_exceeded"
    assert result.data["returned_count"] == 1


@pytest.mark.asyncio
async def test_grep_unix_flags_only_matching_max_count_and_globs(monkeypatch):
    _patch_auth(monkeypatch)
    result = await apfs.grep(
        pattern=r"alpha|beta",
        path="",
        regex=True,
        ignore_case=False,
        invert_match=False,
        only_matching=True,
        include_hidden=False,
        include="*.md",
        exclude="",
        exclude_dir="",
        max_depth=-1,
        max_count=2,
        limit=10,
        max_files=10,
        max_bytes=1000,
        x_access_key="key",
        x_puppyone_user=None,
        x_puppy_client="cli",
        ops=_FakeOps(
            stats={"": _entry("", "folder")},
            tree=[
                _entry("docs/a.md", "markdown"),
                _entry("docs/b.txt", "file"),
            ],
            files={
                "docs/a.md": b"alpha beta\nalpha\n",
                "docs/b.txt": b"alpha\n",
            },
        ),
    )

    assert result.data["complete"] is True
    assert result.data["returned_count"] == 2
    assert result.data["matches"][0]["match_text"] == "alpha"
    assert result.data["matches"][1]["match_text"] == "beta"
    assert result.data["files"] == [
        {
            "path": "docs/a.md",
            "version_path": "docs/a.md",
            "match_count": 2,
            "content_hash": None,
        },
    ]
    assert result.data["matched_files"] == 1


@pytest.mark.asyncio
async def test_grep_invert_match_returns_non_matching_lines(monkeypatch):
    _patch_auth(monkeypatch)
    result = await apfs.grep(
        pattern="skip",
        path="README.md",
        regex=False,
        ignore_case=False,
        invert_match=True,
        only_matching=False,
        include_hidden=False,
        max_depth=-1,
        max_count=0,
        limit=10,
        max_files=10,
        max_bytes=1000,
        x_access_key="key",
        x_puppyone_user=None,
        x_puppy_client="cli",
        ops=_FakeOps(
            stats={"README.md": _entry("README.md", "markdown")},
            files={"README.md": b"keep\nskip\nalso keep\n"},
        ),
    )

    assert result.data["returned_count"] == 2
    assert [m["line_text"] for m in result.data["matches"]] == ["keep", "also keep"]
    assert result.data["files"][0]["match_count"] == 2


@pytest.mark.asyncio
async def test_mkdir_without_parents_requires_existing_parent(monkeypatch):
    _patch_auth(monkeypatch)
    ops = _FakeOps()

    with pytest.raises(HTTPException) as exc:
        await apfs.mkdir(
            apfs.MkdirRequest(path="a/b"),
            x_access_key="key",
            x_puppyone_user=None,
            x_puppy_client="cli",
            commands=_commands(ops),
        )

    assert exc.value.status_code == 404
    assert ops.mkdirs == []


@pytest.mark.asyncio
async def test_mkdir_parents_allows_missing_parent(monkeypatch):
    _patch_auth(monkeypatch)
    ops = _FakeOps()

    result = await apfs.mkdir(
        apfs.MkdirRequest(path="a/b", parents=True, base_commit_id="base"),
        x_access_key="key",
        x_puppyone_user=None,
        x_puppy_client="cli",
        commands=_commands(ops),
    )

    assert result.data["commit_id"] == "mkdir-commit"
    assert ops.mkdirs[0]["path"] == "a/b"
    assert ops.mkdirs[0]["base_commit_id"] == "base"


@pytest.mark.asyncio
async def test_mkdir_parents_existing_directory_is_noop(monkeypatch):
    _patch_auth(monkeypatch)
    ops = _FakeOps(stats={"dir": _entry("dir", "folder")})

    result = await apfs.mkdir(
        apfs.MkdirRequest(path="dir", parents=True),
        x_access_key="key",
        x_puppyone_user=None,
        x_puppy_client="cli",
        commands=_commands(ops),
    )

    assert result.data["created"] is False
    assert result.data["commit_id"] == ""
    assert ops.mkdirs == []


@pytest.mark.asyncio
async def test_mkdir_existing_directory_without_parents_raises(monkeypatch):
    _patch_auth(monkeypatch)
    ops = _FakeOps(stats={"dir": _entry("dir", "folder")})

    with pytest.raises(HTTPException) as exc:
        await apfs.mkdir(
            apfs.MkdirRequest(path="dir"),
            x_access_key="key",
            x_puppyone_user=None,
            x_puppy_client="cli",
            commands=_commands(ops),
        )

    assert exc.value.status_code == 400
    assert ops.mkdirs == []


@pytest.mark.asyncio
async def test_mv_existing_directory_target_moves_inside_it(monkeypatch):
    _patch_auth(monkeypatch)
    ops = _FakeOps(
        stats={
            "a.txt": _entry("a.txt", "file"),
            "dir": _entry("dir", "folder"),
        }
    )

    result = await apfs.move(
        apfs.MoveRequest(old_path="a.txt", new_path="dir"),
        x_access_key="key",
        x_puppyone_user=None,
        x_puppy_client="cli",
        commands=_commands(ops),
    )

    assert result.data["new_path"] == "dir/a.txt"
    assert ops.moves[0]["new_path"] == "dir/a.txt"


@pytest.mark.asyncio
async def test_mv_no_clobber_skips_existing_destination(monkeypatch):
    _patch_auth(monkeypatch)
    ops = _FakeOps(
        stats={
            "a.txt": _entry("a.txt", "file"),
            "b.txt": _entry("b.txt", "file"),
        }
    )

    result = await apfs.move(
        apfs.MoveRequest(old_path="a.txt", new_path="b.txt", no_clobber=True),
        x_access_key="key",
        x_puppyone_user=None,
        x_puppy_client="cli",
        commands=_commands(ops),
    )

    assert result.data["skipped"] is True
    assert result.data["commit_id"] == ""
    assert ops.moves == []


@pytest.mark.asyncio
async def test_touch_missing_file_writes_empty_file_through_product_operation_adapter(monkeypatch):
    _patch_auth(monkeypatch)
    ops = _FakeOps()

    result = await apfs.touch(
        apfs.TouchRequest(path="new.txt", base_commit_id="base"),
        x_access_key="key",
        x_puppyone_user=None,
        x_puppy_client="cli",
        commands=_commands(ops),
    )

    assert result.data["created"] is True
    assert result.data["commit_id"] == "write-commit"
    assert ops.writes == [
        {
            "project_id": "project-id",
            "path": "new.txt",
            "content": b"",
            "who": "access_point:test-ap",
            "scope": "",
            "message": "ap touch new.txt",
            "base_commit_id": "base",
            "defer_projection": True,
        }
    ]


@pytest.mark.asyncio
async def test_touch_existing_file_records_mtime_commit(monkeypatch):
    _patch_auth(monkeypatch)
    ops = _FakeOps(stats={"existing.txt": _entry("existing.txt", "file")})

    result = await apfs.touch(
        apfs.TouchRequest(path="existing.txt"),
        x_access_key="key",
        x_puppyone_user=None,
        x_puppy_client="cli",
        commands=_commands(ops),
    )

    assert result.data["created"] is False
    assert result.data["commit_id"] == "touch-commit"
    assert ops.writes == []
    assert ops.touches[0]["paths"] == ["existing.txt"]


@pytest.mark.asyncio
async def test_cp_existing_directory_target_copies_inside_it(monkeypatch):
    _patch_auth(monkeypatch)
    ops = _FakeOps(
        stats={
            "a.txt": _entry("a.txt", "file"),
            "dir": _entry("dir", "folder"),
        }
    )

    result = await apfs.copy(
        apfs.CopyRequest(old_path="a.txt", new_path="dir"),
        x_access_key="key",
        x_puppyone_user=None,
        x_puppy_client="cli",
        commands=_commands(ops),
    )

    assert result.data["new_path"] == "dir/a.txt"
    assert result.data["commit_id"] == "copy-commit"
    assert ops.copies[0]["new_path"] == "dir/a.txt"


@pytest.mark.asyncio
async def test_cp_explicit_target_directory_requires_directory(monkeypatch):
    _patch_auth(monkeypatch)
    ops = _FakeOps(stats={"a.txt": _entry("a.txt", "file")})

    with pytest.raises(HTTPException) as exc:
        await apfs.copy(
            apfs.CopyRequest(old_path="a.txt", new_path="missing", target_directory=True),
            x_access_key="key",
            x_puppyone_user=None,
            x_puppy_client="cli",
            commands=_commands(ops),
        )

    assert exc.value.status_code == 400
    assert "Not a directory" in str(exc.value.detail)
    assert ops.copies == []


@pytest.mark.asyncio
async def test_cp_explicit_root_target_directory_uses_plain_child_path(monkeypatch):
    _patch_auth(monkeypatch)
    ops = _FakeOps(
        stats={
            "": _entry("", "folder"),
            "a.txt": _entry("a.txt", "file"),
        }
    )

    result = await apfs.copy(
        apfs.CopyRequest(old_path="a.txt", new_path=".", target_directory=True),
        x_access_key="key",
        x_puppyone_user=None,
        x_puppy_client="cli",
        commands=_commands(ops),
    )

    assert result.data["new_path"] == "a.txt"
    assert ops.copies[0]["new_path"] == "a.txt"


@pytest.mark.asyncio
async def test_cp_directory_requires_recursive(monkeypatch):
    _patch_auth(monkeypatch)
    ops = _FakeOps(stats={"docs": _entry("docs", "folder")})

    with pytest.raises(HTTPException) as exc:
        await apfs.copy(
            apfs.CopyRequest(old_path="docs", new_path="docs-copy"),
            x_access_key="key",
            x_puppyone_user=None,
            x_puppy_client="cli",
            commands=_commands(ops),
        )

    assert exc.value.status_code == 400
    assert ops.copies == []


@pytest.mark.asyncio
async def test_cp_no_clobber_skips_existing_destination(monkeypatch):
    _patch_auth(monkeypatch)
    ops = _FakeOps(
        stats={
            "a.txt": _entry("a.txt", "file"),
            "b.txt": _entry("b.txt", "file"),
        }
    )

    result = await apfs.copy(
        apfs.CopyRequest(old_path="a.txt", new_path="b.txt", no_clobber=True),
        x_access_key="key",
        x_puppyone_user=None,
        x_puppy_client="cli",
        commands=_commands(ops),
    )

    assert result.data["skipped"] is True
    assert result.data["commit_id"] == ""
    assert ops.copies == []


@pytest.mark.asyncio
async def test_mv_no_target_directory_rejects_existing_directory(monkeypatch):
    _patch_auth(monkeypatch)
    ops = _FakeOps(
        stats={
            "a.txt": _entry("a.txt", "file"),
            "dir": _entry("dir", "folder"),
        }
    )

    with pytest.raises(HTTPException) as exc:
        await apfs.move(
            apfs.MoveRequest(old_path="a.txt", new_path="dir", no_target_directory=True),
            x_access_key="key",
            x_puppyone_user=None,
            x_puppy_client="cli",
            commands=_commands(ops),
        )

    assert exc.value.status_code == 400
    assert "Is a directory" in str(exc.value.detail)
    assert ops.moves == []


@pytest.mark.asyncio
async def test_mv_no_target_directory_no_clobber_skips_existing_directory(monkeypatch):
    _patch_auth(monkeypatch)
    ops = _FakeOps(
        stats={
            "a.txt": _entry("a.txt", "file"),
            "dir": _entry("dir", "folder"),
        }
    )

    result = await apfs.move(
        apfs.MoveRequest(
            old_path="a.txt",
            new_path="dir",
            no_target_directory=True,
            no_clobber=True,
        ),
        x_access_key="key",
        x_puppyone_user=None,
        x_puppy_client="cli",
        commands=_commands(ops),
    )

    assert result.data["skipped"] is True
    assert result.data["commit_id"] == ""
    assert ops.moves == []


@pytest.mark.asyncio
async def test_mv_rejects_folder_move_into_own_descendant(monkeypatch):
    _patch_auth(monkeypatch)
    ops = _FakeOps(
        stats={
            "old": _entry("old", "folder"),
            "old/sub": _entry("old/sub", "folder"),
        }
    )

    with pytest.raises(HTTPException) as exc:
        await apfs.move(
            apfs.MoveRequest(old_path="old", new_path="old/sub"),
            x_access_key="key",
            x_puppyone_user=None,
            x_puppy_client="cli",
            commands=_commands(ops),
        )

    assert exc.value.status_code == 400
    assert exc.value.detail["error_code"] == "INVALID_MOVE_DESTINATION"
    assert "own subtree" in exc.value.detail["message"]
    assert ops.moves == []


@pytest.mark.asyncio
async def test_rmdir_removes_empty_directory_through_product_operation_adapter(monkeypatch):
    _patch_auth(monkeypatch)
    ops = _FakeOps(
        stats={"empty": _entry("empty", "folder")},
        list_by_path={"empty": []},
    )

    result = await apfs.rmdir(
        apfs.RmdirRequest(path="empty", base_commit_id="base"),
        x_access_key="key",
        x_puppyone_user=None,
        x_puppy_client="cli",
        commands=_commands(ops),
    )

    assert result.data["removed_paths"] == ["empty"]
    assert result.data["commit_id"] == "delete-commit"
    assert ops.deletes[0]["paths"] == ["empty"]
    assert ops.deletes[0]["base_commit_id"] == "base"


@pytest.mark.asyncio
async def test_rmdir_rejects_non_empty_directory(monkeypatch):
    _patch_auth(monkeypatch)
    ops = _FakeOps(
        stats={"dir": _entry("dir", "folder")},
        list_by_path={"dir": [_entry("dir/file.txt", "file")]},
    )

    with pytest.raises(HTTPException) as exc:
        await apfs.rmdir(
            apfs.RmdirRequest(path="dir"),
            x_access_key="key",
            x_puppyone_user=None,
            x_puppy_client="cli",
            commands=_commands(ops),
        )

    assert exc.value.status_code == 400
    assert ops.deletes == []


@pytest.mark.asyncio
async def test_rmdir_parents_removes_empty_parent_chain(monkeypatch):
    _patch_auth(monkeypatch)
    ops = _FakeOps(
        stats={
            "a": _entry("a", "folder"),
            "a/b": _entry("a/b", "folder"),
            "a/b/c": _entry("a/b/c", "folder"),
        },
        list_by_path={
            "a/b/c": [],
            "a/b": [_entry("a/b/c", "folder")],
            "a": [_entry("a/b", "folder")],
        },
    )

    result = await apfs.rmdir(
        apfs.RmdirRequest(path="a/b/c", parents=True),
        x_access_key="key",
        x_puppyone_user=None,
        x_puppy_client="cli",
        commands=_commands(ops),
    )

    assert result.data["removed_paths"] == ["a/b/c", "a/b", "a"]
    assert ops.deletes[0]["paths"] == ["a/b/c", "a/b", "a"]


@pytest.mark.asyncio
async def test_rm_accepts_multiple_paths(monkeypatch):
    _patch_auth(monkeypatch)
    ops = _FakeOps(
        stats={
            "a.txt": _entry("a.txt", "file"),
            "b.txt": _entry("b.txt", "file"),
        }
    )

    result = await apfs.remove(
        apfs.RemoveRequest(paths=["a.txt", "b.txt"], recursive=False),
        x_access_key="key",
        x_puppyone_user=None,
        x_puppy_client="cli",
        commands=_commands(ops),
    )

    assert result.data["paths"] == ["a.txt", "b.txt"]
    assert ops.deletes[0]["paths"] == ["a.txt", "b.txt"]
