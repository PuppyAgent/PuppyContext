from __future__ import annotations

from io import BytesIO
from zipfile import ZipFile

import pytest

from src.version_engine.write_engine.diff import diff_trees
from src.version_engine.write_engine.tree_delta import (
    ContentDelta,
    ContentDeltaRegistry,
    build_default_content_delta_registry,
    build_file_map_delta,
    build_tree_delta,
    changed_relative_paths,
    changes_from_file_maps,
    changes_from_tree_delta,
    paths_from_tree_delta,
)
from src.version_engine.write_engine.tree_delta.directory import expand_directory_changes
from src.version_engine.write_engine.tree_objects import build_tree_from_files


def test_tree_delta_records_structural_changes_with_object_identity(memory_store):
    old_tree = build_tree_from_files(
        memory_store,
        {
            "same.md": b"same",
            "edit.md": b"old",
            "removed.md": b"gone",
            "shape": b"file",
        },
    )
    new_tree = build_tree_from_files(
        memory_store,
        {
            "same.md": b"same",
            "edit.md": b"new",
            "added.md": b"hello",
            "shape/nested.md": b"folder now",
        },
    )

    delta = build_tree_delta(memory_store, old_tree, new_tree)

    by_path = {change.path: change for change in delta.changes}
    assert set(by_path) == {"added.md", "edit.md", "removed.md", "shape"}
    assert by_path["added.md"].action == "add"
    assert by_path["added.md"].new_type == "blob"
    assert by_path["added.md"].new_oid
    assert by_path["edit.md"].action == "update"
    assert by_path["edit.md"].old_type == "blob"
    assert by_path["edit.md"].new_type == "blob"
    assert by_path["edit.md"].old_oid != by_path["edit.md"].new_oid
    assert by_path["removed.md"].action == "delete"
    assert by_path["removed.md"].old_type == "blob"
    assert by_path["shape"].action == "update"
    assert by_path["shape"].old_type == "blob"
    assert by_path["shape"].new_type == "tree"


def test_tree_delta_expands_added_directories_for_write_history(memory_store):
    from types import SimpleNamespace

    server_repo = SimpleNamespace(store=memory_store)
    new_tree = build_tree_from_files(
        server_repo.store,
        {
            "dir/a.md": b"a",
            "dir/b.md": b"b",
        },
    )
    raw_delta = build_tree_delta(server_repo.store, "", new_tree)

    assert [(c.path, c.action, c.new_type) for c in raw_delta.changes] == [
        ("dir", "add", "tree"),
    ]

    expanded = expand_directory_changes(server_repo, raw_delta, "", new_tree)

    assert paths_from_tree_delta(expanded) == ["dir/a.md", "dir/b.md"]
    assert [change.new_oid for change in expanded.changes] == [
        server_repo.store.put_blob(b"a"),
        server_repo.store.put_blob(b"b"),
    ]
    assert changes_from_tree_delta(expanded, "docs") == [
        {"path": "docs/dir/a.md", "action": "add"},
        {"path": "docs/dir/b.md", "action": "add"},
    ]


def test_tree_delta_file_map_projection_is_the_single_changeset_contract():
    old_files = {
        "keep.md": b"same",
        "edit.md": b"old",
        "remove.md": b"gone",
    }
    new_files = {
        "keep.md": b"same",
        "edit.md": b"new",
        "add.md": b"hi",
    }

    delta = build_file_map_delta(old_files, new_files)

    assert changed_relative_paths(old_files, new_files) == [
        "add.md",
        "edit.md",
        "remove.md",
    ]
    assert changes_from_file_maps("scope", old_files, new_files) == [
        {"path": "scope/add.md", "action": "add"},
        {"path": "scope/edit.md", "action": "update"},
        {"path": "scope/remove.md", "action": "delete"},
    ]
    assert changes_from_tree_delta(delta, "scope") == [
        {"path": "scope/add.md", "action": "add"},
        {"path": "scope/edit.md", "action": "update"},
        {"path": "scope/remove.md", "action": "delete"},
    ]


def test_legacy_diff_wrapper_keeps_old_compact_shape(memory_store):
    old_tree = build_tree_from_files(memory_store, {"a.md": b"old"})
    new_tree = build_tree_from_files(memory_store, {"a.md": b"new", "b.md": b"new"})

    assert diff_trees(memory_store, old_tree, new_tree) == [
        {"path": "a.md", "op": "modified"},
        {"path": "b.md", "op": "added"},
    ]


def test_content_delta_registry_routes_optional_machine_diff_strategies():
    class JsonKeyStrategy:
        id = "json-key"

        def supports(self, path, old_data, new_data):
            return path.endswith(".json")

        def changed_regions(self, path, old_data, new_data):
            return {"keys": ["title"]}

    class MarkdownHeadingStrategy:
        id = "markdown-heading"

        def supports(self, path, old_data, new_data):
            return path.endswith(".md")

        def changed_regions(self, path, old_data, new_data):
            return {"headings": ["Intro"]}

    registry = ContentDeltaRegistry((JsonKeyStrategy(), MarkdownHeadingStrategy()))

    json_delta = registry.changed_regions(
        "config.json",
        b'{"title":"old"}',
        b'{"title":"new"}',
    )
    assert json_delta == (
        ContentDelta(path="config.json", strategy_id="json-key", regions={"keys": ["title"]})
    )
    markdown_delta = registry.changed_regions("notes.md", b"# Old", b"# Intro")
    assert markdown_delta.strategy_id == "markdown-heading"
    assert registry.changed_regions("archive.docx", b"old", b"new") is None


def test_content_delta_registry_rejects_ambiguous_strategy_ids():
    class Strategy:
        id = "same"

        def supports(self, path, old_data, new_data):
            return True

        def changed_regions(self, path, old_data, new_data):
            return None

    registry = ContentDeltaRegistry((Strategy(),))

    with pytest.raises(ValueError, match="duplicate content delta strategy id"):
        registry.register(Strategy())


def test_default_content_delta_registry_covers_json_text_and_docx_machine_diffs():
    registry = build_default_content_delta_registry()

    json_delta = registry.changed_regions(
        "config.json",
        b'{"title":"old","keep":true}',
        b'{"title":"new","keep":true}',
    )
    assert json_delta.strategy_id == "json-paths"
    assert json_delta.regions["changed_paths"] == [
        {
            "path": "$.title",
            "action": "update",
            "old_type": "str",
            "new_type": "str",
        },
    ]

    markdown_delta = registry.changed_regions("README.md", b"# Old\nsame", b"# New\nsame")
    assert markdown_delta.strategy_id == "text-lines"
    assert markdown_delta.regions["kind"] == "text-lines"
    assert markdown_delta.regions["changed_ranges"] == [
        {
            "action": "replace",
            "old_start": 1,
            "old_end": 1,
            "new_start": 1,
            "new_end": 1,
        },
    ]

    docx_delta = registry.changed_regions(
        "proposal.docx",
        _docx_bytes("<w:document><w:t>old</w:t></w:document>"),
        _docx_bytes("<w:document><w:t>new</w:t></w:document>"),
    )
    assert docx_delta.strategy_id == "docx-package-parts"
    assert docx_delta.regions["kind"] == "docx-package-parts"
    assert docx_delta.regions["changed_parts"][0]["part"] == "word/document.xml"
    assert docx_delta.regions["changed_parts"][0]["action"] == "update"


def _docx_bytes(document_xml: str) -> bytes:
    out = BytesIO()
    with ZipFile(out, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types></Types>")
        archive.writestr("word/document.xml", document_xml)
    return out.getvalue()
