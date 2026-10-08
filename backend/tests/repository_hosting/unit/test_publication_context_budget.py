"""Publication batch boundaries and Git CAS metadata do not hide extra reads."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.version_engine.infrastructure.supabase.capacity_repository import RepositoryCapacity
from src.version_engine.read.ref_metadata import GitBranchBase
from src.version_engine.write_engine.ref_transaction import RefState


@pytest.mark.parametrize("count", [1, 199, 200, 201, 400, 401])
def test_final_capacity_batch_and_seal_are_one_operation(count):
    calls = []
    objects = {
        f"{index:040x}": SimpleNamespace(kind="blob", size=index + 1) for index in range(count)
    }
    manifest = SimpleNamespace(
        objects=objects,
        new_objects=None,
        digest="d" * 64,
        object_format="sha1",
        roots={"a" * 40: "commit"},
        root_details=lambda: {},
    )

    def call(name, **kwargs):
        calls.append((name, kwargs))
        if name == "reserve_version_object_capacity":
            return {"new_body_bytes": 0, "new_objects": 0}
        assert name == "seal_version_capacity_batch"
        return {
            "id": "pin",
            "project_id": "project",
            "manifest_sha256": manifest.digest,
            "object_format": "sha1",
            "roots": manifest.roots,
        }

    RepositoryCapacity(SimpleNamespace(call=call)).seal("project", "actor", "pin", manifest)
    assert len(calls) == (count + 199) // 200
    assert calls[-1][0] == "seal_version_capacity_batch"
    rows = [row for _, args in calls for row in args["p_objects"]]
    assert [row["object_id"] for row in rows] == sorted(objects)
    assert all(1 <= len(args["p_objects"]) <= 200 for _, args in calls)


@pytest.mark.parametrize("fmt,width", [("sha1", 40), ("sha256", 64)])
def test_git_base_preserves_branch_and_head_guard_without_tree_lookup(fmt, width):
    import base64

    branch = b"refs/heads/notes"
    wire = {
        "repository_profile": "native",
        "object_format": fmt,
        "generation": 7,
        "target_ref": branch.decode(),
        "target_ref_b64": base64.b64encode(branch).decode(),
        "expected_oid": "a" * width,
        "head_guard": RefState(target=branch).wire(fmt),
    }
    base = GitBranchBase.parse(wire)
    assert base.wire() == wire
    edits = base.edits("b" * width)
    assert edits[0].name == b"HEAD" and edits[0].expected.target == branch
    assert edits[1].expected.oid == "a" * width and edits[1].new.oid == "b" * width
    with pytest.raises(ValueError, match="guard"):
        GitBranchBase.parse({**wire, "head_guard": RefState(target=b"refs/heads/other").wire(fmt)})


def test_result_query_uses_metadata_only_operation():
    from src.version_engine.adapters.git.run_transport import RunGitTransport

    manager = SimpleNamespace(
        get_native_operation_status=Mock(return_value={"result": {"status": "committed"}}),
        get_native_service=Mock(side_effect=AssertionError("constructed full service")),
    )
    assert RunGitTransport(manager).result("project", "grant", "request") == {"status": "committed"}
    manager.get_native_service.assert_not_called()
    manager.get_native_operation_status.assert_called_once_with("project", "grant", "request")
