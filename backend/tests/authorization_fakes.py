"""Hermetic facts for exercising the real Project authorization policy.

Route tests override only the repository boundary. They intentionally keep
``AuthorizationService`` itself real so role/capability behavior cannot be
mocked into an always-allow boolean.
"""

from __future__ import annotations

from collections.abc import Iterable
from types import SimpleNamespace

from fastapi import FastAPI

from src.exception_handler import app_exception_handler
from src.exceptions import AppException
from src.platform.authorization.dependencies import get_authorization_service
from src.platform.authorization.repository import ProjectAuthorizationFacts
from src.platform.authorization.service import AuthorizationService


class StaticAuthorizationRepository:
    def __init__(self, facts: Iterable[ProjectAuthorizationFacts]):
        self._facts = {fact.project_id: fact for fact in facts}

    def load_project_facts(self, project_id: str, user_id: str) -> ProjectAuthorizationFacts | None:
        return self._facts.get(project_id)

    def load_project_facts_batch(
        self, project_ids: list[str], user_id: str
    ) -> dict[str, ProjectAuthorizationFacts]:
        return {
            project_id: self._facts[project_id]
            for project_id in project_ids
            if project_id in self._facts
        }


def authorization_for(
    *project_ids: str,
    role: str = "admin",
    visibility: str = "private",
    org_role: str = "member",
    org_id: str = "org-1",
) -> AuthorizationService:
    return AuthorizationService(
        StaticAuthorizationRepository(
            ProjectAuthorizationFacts(
                project_id=project_id,
                org_id=org_id,
                visibility=visibility,
                org_role=org_role,
                project_role=role,
                project_member_org_id=org_id,
            )
            for project_id in project_ids
        )
    )


def install_authorization(app: FastAPI, authorization: AuthorizationService) -> None:
    app.dependency_overrides[get_authorization_service] = lambda: authorization
    app.add_exception_handler(AppException, app_exception_handler)


def authorization_facts_response(tables, name, params):
    assert name in {"authorization_project_facts", "authorization_project_facts_batch"}
    projects = params.get("p_projects", [params.get("p_project")])
    user = params["p_user"]
    rows = []
    for project in tables.get("projects", []):
        if project["id"] not in projects or project["lifecycle_status"] != "ready":
            continue
        org = next(
            (
                m
                for m in tables.get("org_members", [])
                if m["org_id"] == project["org_id"] and m["user_id"] == user
            ),
            {},
        )
        member = next(
            (
                m
                for m in tables.get("project_members", [])
                if m["project_id"] == project["id"] and m["user_id"] == user
            ),
            {},
        )
        rows.append(
            dict(
                project_id=project["id"],
                org_id=project["org_id"],
                visibility=project["visibility"],
                org_role=org.get("role"),
                project_role=member.get("role"),
                project_member_org_id=member.get("org_id"),
            )
        )
    data = rows if name.endswith("_batch") else (rows[0] if rows else None)
    return SimpleNamespace(execute=lambda: SimpleNamespace(data=data))
