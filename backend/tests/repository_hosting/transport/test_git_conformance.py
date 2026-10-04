"""Same recipes against native bare Git and production HTTP, then cold-cache readback.

This is the full Project target profile, NOT permission to broaden legacy Scope.
The native oracle runs in fixture setup: a broken recipe cannot be hidden as an
expected Cloud capability gap. PG/Auth are still doubles and storage is disk.
"""

import json
import shutil

import pytest

from tests.repository_hosting.harness.conformance import remote_snapshot
from tests.repository_hosting.harness.git import Git
from tests.repository_hosting.harness.history_workflows import HISTORY_WORKFLOWS
from tests.repository_hosting.harness.http import clone_client
from tests.repository_hosting.harness.network_workflows import NETWORK_WORKFLOWS
from tests.repository_hosting.harness.ref_workflows import REF_WORKFLOWS

WORKFLOWS = [*HISTORY_WORKFLOWS, *REF_WORKFLOWS, *NETWORK_WORKFLOWS]
pytestmark = pytest.mark.hosting_component


@pytest.fixture
def reference_workflow(hosted, workflow, tmp_path, request, monkeypatch):
    source, _, _, _, _ = hosted
    request.node.user_properties.extend([
        ("git_workflow", workflow.name),
        ("git_capabilities", json.dumps(workflow.capabilities)),
        ("git_commands", json.dumps(workflow.commands)),
        ("git_profile", "full-project-sha1-target"),
        ("git_oracle", "native bare Git; explicit uploadpack filter support"),
        ("storage_evidence", "disk objects; memory control plane; disposable transport cache"),
    ])
    native = Git.init(tmp_path / "oracle.git", bare=True)
    # These are explicit native reference-server capabilities, not host settings
    # or a change to PuppyOne's receive-pack guards.
    native.run("config", "uploadpack.allowFilter", "true")
    native.run("config", "uploadpack.allowAnySHA1InWant", "true")
    remote = native.path.as_uri()  # local path cloning would ignore --depth/filter
    source.run("push", remote, "main")
    commands = set()
    run = Git.run

    def record_command(repo, *args, **kwargs):
        index = 0
        while str(args[index]).startswith("-"):
            index += 2 if args[index] == "-c" else 1
        commands.add(str(args[index]))
        return run(repo, *args, **kwargs)

    with monkeypatch.context() as trace:
        trace.setattr(Git, "run", record_command)
        client = clone_client(remote, tmp_path / "oracle-client", source)
        workflow.execute(client)
        result = remote_snapshot(source, remote, tmp_path / "oracle-readback.git")
    assert set(workflow.commands) <= commands, "declared commands must actually execute"
    request.node.user_properties.append(("git_executed_commands", json.dumps(sorted(commands))))
    return result


@pytest.mark.parametrize("hosted", [""], indirect=True, ids=["project"])
@pytest.mark.parametrize("workflow", [
    pytest.param(
        case, id=case.name,
        marks=pytest.mark.hosting_gap(case.gap) if case.gap else (),
    ) for case in WORKFLOWS
])
def test_native_git_workflow_matches_hosted_repository(
    hosted, workflow, reference_workflow, tmp_path,
):
    source, remote, state, _, _ = hosted
    client = clone_client(remote, tmp_path / "hosted-client", source)
    workflow.execute(client)
    # Erase only this fixture's owned cache. The next mirror must reconstruct
    # from control-plane refs + object storage, not the push process's objects.
    cache = state.git_cache_root
    assert cache.is_relative_to(tmp_path) and cache.exists()
    shutil.rmtree(cache)
    assert not cache.exists()
    actual = remote_snapshot(source, remote, tmp_path / "hosted-readback.git")
    assert actual.refs == reference_workflow.refs
    assert actual.head == reference_workflow.head
    assert actual.objects == reference_workflow.objects
