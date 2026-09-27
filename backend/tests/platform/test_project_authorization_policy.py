from __future__ import annotations

import itertools

import pytest

from src.exceptions import (
    PermissionException,
    ServiceUnavailableException,
)
from src.platform.authorization.models import (
    ROLE_CAPABILITIES,
    GrantSource,
    ProjectAction,
    ProjectRole,
)
from src.platform.authorization.repository import ProjectAuthorizationFacts
from src.platform.authorization.service import AuthorizationService


class FactsRepository:
    def __init__(self, facts=None, error: Exception | None = None):
        self.facts = facts
        self.error = error

    def load_project_facts(self, _project_id, _user_id):
        if self.error:
            raise self.error
        return self.facts

    def load_project_facts_batch(self, project_ids, _user_id):
        if self.error:
            raise self.error
        return {project_id: self.facts for project_id in project_ids if self.facts is not None}


def _facts(
    *,
    project_id="project-1",
    visibility="private",
    org_role="member",
    project_role=None,
    project_member_org_id=None,
):
    return ProjectAuthorizationFacts(
        project_id=project_id,
        org_id="org-1",
        visibility=visibility,
        org_role=org_role,
        project_role=project_role,
        project_member_org_id=project_member_org_id,
    )


@pytest.mark.parametrize(
    ("facts", "role", "source"),
    [
        (_facts(org_role=None), None, None),
        (_facts(org_role="owner"), ProjectRole.ADMIN, GrantSource.ORG_OWNER),
        (
            _facts(project_role="editor", project_member_org_id="org-1"),
            ProjectRole.EDITOR,
            GrantSource.PROJECT_MEMBER,
        ),
        (
            _facts(visibility="org"),
            ProjectRole.VIEWER,
            GrantSource.ORG_VISIBILITY,
        ),
        (_facts(visibility="private"), None, None),
    ],
)
def test_project_grant_resolution_matrix(facts, role, source):
    grant = AuthorizationService(FactsRepository(facts)).resolve_project_grant(
        "project-1", "user-1"
    )
    assert (grant.role if grant else None) == role
    assert (grant.source if grant else None) == source


def test_explicit_role_overrides_org_visible_viewer_baseline():
    grant = AuthorizationService(
        FactsRepository(
            _facts(
                visibility="org",
                project_role="admin",
                project_member_org_id="org-1",
            )
        )
    ).resolve_project_grant("project-1", "user-1")
    assert grant and grant.role is ProjectRole.ADMIN


@pytest.mark.parametrize("bad_role", ["owner", "member", "reader", "denied", ""])
def test_unknown_or_legacy_project_roles_fail_closed(bad_role):
    grant = AuthorizationService(
        FactsRepository(_facts(project_role=bad_role, project_member_org_id="org-1"))
    ).resolve_project_grant("project-1", "user-1")
    assert grant is None


def test_cross_tenant_project_membership_fails_closed():
    grant = AuthorizationService(
        FactsRepository(_facts(project_role="admin", project_member_org_id="org-other"))
    ).resolve_project_grant("project-1", "user-1")
    assert grant is None


def test_repository_fact_for_another_project_cannot_be_reused():
    grant = AuthorizationService(
        FactsRepository(_facts(project_id="project-other", visibility="org"))
    ).resolve_project_grant("project-1", "user-1")
    assert grant is None


def test_repository_failure_fails_closed_as_retryable_unavailability():
    service = AuthorizationService(FactsRepository(error=RuntimeError("database unavailable")))
    with pytest.raises(ServiceUnavailableException) as resolved:
        service.resolve_project_grant("project-1", "user-1")
    assert resolved.value.status_code == 503
    assert resolved.value.details == {"retryable": True}
    assert resolved.value.headers == {"Retry-After": "1"}

    with pytest.raises(ServiceUnavailableException) as authorized:
        service.authorize("project-1", "user-1", ProjectAction.PROJECT_READ)
    assert authorized.value.status_code == 503


def test_viewer_cannot_mutate_or_manage_credentials():
    service = AuthorizationService(FactsRepository(_facts(visibility="org")))
    assert service.allows("project-1", "user-1", ProjectAction.CONTENT_READ)
    for action in (
        ProjectAction.CONTENT_WRITE,
        ProjectAction.AGENT_RUN,
        ProjectAction.ACCESS_MANAGE,
        ProjectAction.CREDENTIAL_MANAGE,
        ProjectAction.MEMBERS_MANAGE,
    ):
        with pytest.raises(PermissionException):
            service.authorize("project-1", "user-1", action)


def test_editor_can_run_but_cannot_manage_project_runtime_surfaces():
    service = AuthorizationService(
        FactsRepository(_facts(project_role="editor", project_member_org_id="org-1"))
    )
    for action in (
        ProjectAction.CONTENT_WRITE,
        ProjectAction.AGENT_RUN,
        ProjectAction.AUTOMATION_RUN,
    ):
        assert service.allows("project-1", "user-1", action)
    for action in (
        ProjectAction.AGENT_MANAGE,
        ProjectAction.AUTOMATION_MANAGE,
        ProjectAction.MCP_MANAGE,
        ProjectAction.SANDBOX_MANAGE,
        ProjectAction.SYNCHRONIZE_MANAGE,
        ProjectAction.ACCESS_MANAGE,
        ProjectAction.CREDENTIAL_MANAGE,
        ProjectAction.MEMBERS_MANAGE,
    ):
        assert not service.allows("project-1", "user-1", action)


def test_capabilities_are_monotonic_by_role():
    assert ROLE_CAPABILITIES[ProjectRole.VIEWER] < ROLE_CAPABILITIES[ProjectRole.EDITOR]
    assert ROLE_CAPABILITIES[ProjectRole.EDITOR] < ROLE_CAPABILITIES[ProjectRole.ADMIN]
    for lower, higher in itertools.pairwise(
        [ProjectRole.VIEWER, ProjectRole.EDITOR, ProjectRole.ADMIN]
    ):
        assert not (ROLE_CAPABILITIES[lower] - ROLE_CAPABILITIES[higher])


def test_batch_filter_fails_closed_as_retryable_unavailability():
    class Project:
        id = "project-1"

    service = AuthorizationService(FactsRepository(error=RuntimeError("database unavailable")))
    with pytest.raises(ServiceUnavailableException) as filtered:
        service.filter_accessible([Project()], "user-1")
    assert filtered.value.status_code == 503
    with pytest.raises(ServiceUnavailableException) as ids:
        service.accessible_project_ids(["project-1"], "user-1")
    assert ids.value.status_code == 503


def test_decision_telemetry_is_redacted(caplog):
    service = AuthorizationService(
        FactsRepository(_facts(project_id="sensitive-project-id", visibility="org"))
    )
    with caplog.at_level("INFO", logger="puppyone.authorization"):
        service.authorize("sensitive-project-id", "sensitive-user-id", ProjectAction.PROJECT_READ)
    decision = next(
        record.authorization_decision
        for record in caplog.records
        if hasattr(record, "authorization_decision")
    )
    assert decision["outcome"] == "allow"
    assert decision["project_ref"] != "sensitive-project-id"
    assert "sensitive-project-id" not in str(decision)
    assert "sensitive-user-id" not in str(decision)
