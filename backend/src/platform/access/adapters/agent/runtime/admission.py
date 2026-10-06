"""Resolve saved Agent configuration without widening its repository view."""

import hashlib
import json

from fastapi import HTTPException

from src.config import settings
from src.platform.access.adapters.agent.config.service import AgentConfigService
from src.platform.authorization.factory import build_authorization_service
from src.platform.authorization.models import ProjectAction
from src.platform.project.readiness import ProjectReadinessService
from src.repo.scope_repository import RepositoryScopeRepository


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
    def __init__(self, repository, authorization=None, configs=None, readiness=None, scopes=None):
        self.repository = repository
        self.authorization = authorization or build_authorization_service()
        self.configs = configs or AgentConfigService()
        self.readiness = readiness or ProjectReadinessService()
        self.scopes = scopes or RepositoryScopeRepository()

    def authorize(self, user, project, action=ProjectAction.AGENT_RUN):
        return self.authorization.authorize(project, user, action)

    def surface(self, agent_id):
        surface = self.repository.surface(agent_id)
        if surface is None:
            raise HTTPException(404, "Agent not found")
        return surface

    def resolve(self, user, project, agent_id, scope_id=None):
        self.authorize(user, project)
        ready = self.readiness.resolve(project)
        if not ready.claude_ready:
            raise HTTPException(
                409, {"code": "project_agent_not_ready", "blockers": ready.blockers}
            )
        if not agent_id:
            rows = self.repository.target_agents(project, scope_id)
            visible = [
                row
                for row in rows
                if (row.get("config") or {}).get("visibility", "org") != "private"
                or row.get("created_by") == user
            ]
            if visible:
                agent_id = visible[0]["id"]
            else:
                self.authorize(user, project, ProjectAction.AGENT_MANAGE)
                view = self.target_view(project, scope_id)
                config = {
                    "name": "PuppyOne Agent",
                    "type": "chat",
                    "activated": True,
                    "is_default": True,
                    "visibility": "org",
                    "bash_view": view,
                }
                agent_id = self.repository.rpc(
                    "builtin", project=project, scope=scope_id, user=user, config=config
                )
        surface = self.surface(agent_id)
        if surface["project_id"] != project or (
            scope_id is not None and surface.get("scope_id") != scope_id
        ):
            raise HTTPException(404, "Agent not found")
        if not self.configs.is_visible_to(agent_id, user):
            raise HTTPException(404, "Agent not found")
        if surface["status"] != "active":
            raise HTTPException(409, "Agent is paused or unavailable")
        return surface

    def target_view(self, project, scope_id):
        if not scope_id:
            return {"path_prefix": "", "excludes": [], "max_mode": "rw"}
        scope = self.scopes.get(scope_id)
        if scope is None or scope.project_id != project:
            raise HTTPException(404, "Agent Scope not found")
        return {
            "path_prefix": scope.path,
            "excludes": list(scope.exclude),
            "max_mode": scope.max_mode,
        }

    def policy(self, surface):
        config = surface.get("config") or {}
        target = self.target_view(surface["project_id"], surface.get("scope_id"))
        operational = config.get("bash_view")
        view = operational or {
            "path_prefix": target["path_prefix"],
            "excludes": [],
            "max_mode": "r",
        }
        prefix, boundary = clean_path(view["path_prefix"]), clean_path(target["path_prefix"])
        if boundary and prefix != boundary and not prefix.startswith(boundary + "/"):
            raise HTTPException(409, "Agent path exceeds its Scope")
        excludes = sorted({clean_path(p) for p in [*target["excludes"], *view.get("excludes", [])]})
        agent = self.configs.get_agent(surface["id"])
        scope = self.scopes.get(surface["scope_id"]) if surface.get("scope_id") else None
        return {
            "surface_updated_at": surface["updated_at"],
            "scope_updated_at": str(scope.updated_at) if scope else None,
            "path_prefix": prefix,
            "excludes": excludes,
            "materialize": operational is not None,
            "readonly": view.get("max_mode", "r") != "rw" or target["max_mode"] != "rw",
            "model": config.get("llm_model") or settings.CLOUD_AGENT_DEFAULT_MODEL,
            "system_prompt": config.get("system_prompt") or "",
            "tool_ids": sorted(t.tool_id for t in agent.tools if t.enabled),
            "max_tokens": settings.CLOUD_AGENT_MAX_TOKENS,
            "context_window": settings.CLOUD_AGENT_CONTEXT_WINDOW,
        }

    def recheck(self, run):
        surface = self.resolve(run["user_id"], run["project_id"], run["agent_id"])
        if digest(self.policy(surface)) != digest(run["policy"]):
            raise HTTPException(409, "Agent configuration changed during execution")
        if not run["policy"]["readonly"]:
            self.authorize(run["user_id"], run["project_id"], ProjectAction.CONTENT_WRITE)
        return self.authorization.resolve_project_grant(run["project_id"], run["user_id"])
