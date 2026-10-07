from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

from src.platform.project.readiness import ProjectReadinessService
from src.platform.project.readiness_repository import ProjectReadinessRepository


def _resolve(*, root_surface, root_head="", child_head="", accepted_root_git_push=False):
    class Client:
        def rpc(self, name, parameters):
            assert name == "get_native_project_readiness"
            assert parameters == {"p_project_id": "project-1"}
            return self

        def execute(self):
            return SimpleNamespace(
                data={
                    "default_branch_b64": base64.b64encode(b"refs/heads/main").decode(),
                    "project_git_surface_exists": root_surface,
                    "project_head_commit_id": root_head,
                    "project_git_push_accepted": accepted_root_git_push,
                }
            )

    return ProjectReadinessService(ProjectReadinessRepository(Client())).resolve("project-1")


@pytest.mark.parametrize(
    ("surface", "head", "state", "ready"),
    [
        (False, "", "git_not_created", False),
        (True, "", "awaiting_first_push", False),
        (True, "a" * 40, "ready", True),
        (True, "a" * 64, "ready", True),
    ],
)
def test_root_readiness_state_machine(surface, head, state, ready):
    readiness = _resolve(
        root_surface=surface,
        root_head=head,
        accepted_root_git_push=ready,
    )
    assert readiness.git_state == state
    assert readiness.claude_ready is ready


def test_non_root_head_never_unlocks_claude():
    readiness = _resolve(
        root_surface=True,
        child_head="b" * 40,
        accepted_root_git_push=True,
    )
    assert readiness.git_state == "awaiting_first_push"
    assert readiness.claude_ready is False


def test_product_write_head_never_substitutes_for_first_root_git_push():
    readiness = _resolve(root_surface=True, root_head="c" * 40)
    assert readiness.project_head_exists is True
    assert readiness.project_git_push_accepted is False
    assert readiness.git_state == "awaiting_first_push"
    assert "project_git_push_not_accepted" in readiness.blockers


def test_readiness_wire_contract_names_project_root_explicitly():
    payload = _resolve(
        root_surface=True,
        root_head="d" * 40,
        accepted_root_git_push=True,
    ).as_dict()

    assert payload["git"] == {
        "target": {"kind": "project_root", "project_id": "project-1"},
        "surface_exists": True,
        "head_exists": True,
        "push_accepted": True,
        "default_branch": "main",
        "state": "ready",
    }
