"""Pure policy over one consistent context query; saved Agent configuration stays authoritative."""

import hashlib
import json

from fastapi import HTTPException

from src.config import settings
from src.platform.access.adapters.agent.runtime.context import RunContext
from src.platform.access.adapters.agent.runtime.ports import RunStore
from src.platform.authorization.factory import build_authorization_service
from src.platform.authorization.models import ProjectAction


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def clean_path(value):
    path = (value or "").strip("/")
    if (
        "\\" in path
        or "\0" in path
        or any(part in {".", "..", ""} for part in path.split("/") if path)
    ):
        raise HTTPException(409, "Agent has an invalid repository path")
    return path


class Admission:
    def __init__(self, repository: RunStore, authorization=None, readiness=None):
        self.repository = repository
        self.authorization = authorization or build_authorization_service()
        self.readiness = readiness

    def authorize(self, user, project, action=ProjectAction.AGENT_RUN):
        return self.authorization.authorize(project, user, action)

    def surface(self, agent_id):
        # Configuration inspection only; runtime orchestration uses context.
        return self.repository.surface(agent_id)

    def visible(self, surface, user, project):
        if (
            not surface
            or surface["project_id"] != project
            or (
                (surface.get("config") or {}).get("visibility", "org") == "private"
                and surface.get("created_by") != user
            )
        ):
            raise HTTPException(404, "Agent not found")

    def evaluate(self, value, user, project):
        grant = self.authorization.authorize_facts(
            value["facts"], project, user, ProjectAction.AGENT_RUN
        )
        surface = value["surface"]
        self.visible(surface, user, project)
        if surface["status"] != "active":
            raise HTTPException(409, "Agent is paused or unavailable")
        config = surface.get("config") or {}
        view = config.get("bash_view") or {"path_prefix": "", "excludes": [], "max_mode": "r"}
        if surface.get("scope_id"):
            raise HTTPException(409, "Native Agent Git requires an unrestricted Project-root view")
        policy = {
            "revision": value["revision"],
            "surface_updated_at": surface["updated_at"],
            "scope_updated_at": None,
            "path_prefix": clean_path(view["path_prefix"]),
            "excludes": sorted({clean_path(p) for p in view.get("excludes", [])}),
            "materialize": config.get("bash_view") is not None,
            "readonly": view.get("max_mode", "r") != "rw",
            "model": config.get("llm_model") or settings.CLOUD_AGENT_DEFAULT_MODEL,
            "system_prompt": config.get("system_prompt") or "",
            "tool_ids": sorted(t["tool_id"] for t in value["tools"] if t["enabled"]),
            "tool_definitions": [t["definition"] for t in value["tools"] if t["enabled"]],
            "max_tokens": settings.CLOUD_AGENT_MAX_TOKENS,
            "context_window": settings.CLOUD_AGENT_CONTEXT_WINDOW,
        }
        if not policy["readonly"]:
            self.authorization.authorize_facts(
                value["facts"], project, user, ProjectAction.CONTENT_WRITE
            )
        return RunContext(
            user,
            project,
            surface["id"],
            grant,
            json.dumps(policy),
            json.dumps(value.get("receipt")),
        )

    def load(self, user, project, agent_id=None, scope_id=None, request_id=None):
        for attempt in range(2):
            try:
                return self._load(user, project, agent_id, scope_id, request_id)
            except HTTPException as exc:
                if attempt or exc.detail != {"code": "agent_context_changed"}:
                    raise
        raise AssertionError("unreachable")

    def _load(self, user, project, agent_id=None, scope_id=None, request_id=None):
        value = self.repository.load_run_context(user, project, agent_id, scope_id, request_id)
        self.authorization.authorize_facts(value["facts"], project, user, ProjectAction.AGENT_RUN)
        ready = value["readiness"]
        is_ready = (
            self.readiness.resolve(project).claude_ready
            if self.readiness
            else bool(
                ready
                and ready.get("project_git_surface_exists")
                and ready.get("project_head_commit_id")
                and ready.get("project_git_push_accepted")
            )
        )
        if not is_ready and not value.get("receipt"):
            raise HTTPException(409, {"code": "project_agent_not_ready"})
        if not value["surface"] and agent_id is None:
            self.authorization.authorize_facts(
                value["facts"], project, user, ProjectAction.AGENT_MANAGE
            )
            if scope_id:
                raise HTTPException(
                    409, "Native Agent Git requires an unrestricted Project-root view"
                )
            agent_id = self.repository.create_builtin(
                project=project,
                scope=None,
                user=user,
                revision=value["revision"],
                config={
                    "name": "PuppyOne Agent",
                    "type": "chat",
                    "activated": True,
                    "is_default": True,
                    "visibility": "org",
                    "bash_view": {"path_prefix": "", "excludes": [], "max_mode": "rw"},
                },
            )
            value = self.repository.load_run_context(user, project, agent_id, request=request_id)
        if scope_id and (value["surface"] or {}).get("scope_id") != scope_id:
            raise HTTPException(404, "Agent not found")
        return self.evaluate(value, user, project)

    def recheck(self, run):
        context = self.evaluate(
            self.repository.load_run_context(run["user_id"], run["project_id"], run["agent_id"]),
            run["user_id"],
            run["project_id"],
        )
        if context.policy["revision"] != run["policy"]["revision"]:
            raise HTTPException(409, "Agent configuration changed during execution")
        return context.grant
