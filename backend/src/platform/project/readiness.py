"""Derived Project Git and Claude readiness.

Readiness is a projection of durable facts. It is never a mutable flag on the
Project row, so a non-root push or failed push cannot accidentally unlock the
Project Agent surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.platform.project.readiness_repository import ProjectReadinessRepository


@dataclass(frozen=True, slots=True)
class ProjectReadiness:
    project_id: str
    project_git_surface_exists: bool
    project_head_exists: bool
    project_git_push_accepted: bool
    default_branch: str

    @property
    def git_state(self) -> str:
        if not self.project_git_surface_exists:
            return "git_not_created"
        if not self.project_head_exists or not self.project_git_push_accepted:
            return "awaiting_first_push"
        return "ready"

    @property
    def claude_ready(self) -> bool:
        return (
            self.project_git_surface_exists
            and self.project_head_exists
            and self.project_git_push_accepted
        )

    @property
    def blockers(self) -> list[str]:
        blockers: list[str] = []
        if not self.project_git_surface_exists:
            blockers.append("project_git_surface_missing")
        if not self.project_head_exists:
            blockers.append("project_head_missing")
        if not self.project_git_push_accepted:
            blockers.append("project_git_push_not_accepted")
        return blockers

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "git": {
                "target": {
                    "kind": "project_root",
                    "project_id": self.project_id,
                },
                "surface_exists": self.project_git_surface_exists,
                "head_exists": self.project_head_exists,
                "push_accepted": self.project_git_push_accepted,
                "default_branch": self.default_branch,
                "state": self.git_state,
            },
            "claude": {
                "ready": self.claude_ready,
                "blockers": self.blockers,
            },
        }


class ProjectReadinessService:
    def __init__(self, repository: ProjectReadinessRepository | None = None):
        self._repository = repository or ProjectReadinessRepository()

    def resolve(self, project_id: str) -> ProjectReadiness:
        facts = self._repository.load(project_id)
        project_head = str(facts["project_head_commit_id"])
        project_head_exists = len(project_head) in (40, 64) and all(
            character in "0123456789abcdef" for character in project_head
        )
        return ProjectReadiness(
            project_id=project_id,
            project_git_surface_exists=bool(facts["project_git_surface_exists"]),
            project_head_exists=project_head_exists,
            project_git_push_accepted=bool(facts["project_git_push_accepted"]),
            default_branch=str(facts["default_branch"]),
        )
