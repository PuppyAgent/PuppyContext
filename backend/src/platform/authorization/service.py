"""Single policy decision point for human Project authorization."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from src.exceptions import (
    ErrorCode,
    NotFoundException,
    PermissionException,
    ServiceUnavailableException,
)
from src.platform.authorization.models import (
    ROLE_CAPABILITIES,
    GrantSource,
    ProjectAction,
    ProjectGrant,
    ProjectRole,
)
from src.platform.authorization.repository import (
    AuthorizationRepository,
    ProjectAuthorizationFacts,
)
from src.utils.request_context import project_access_cache_var

_CACHE_PREFIX = "project-grant:v2"
_logger = logging.getLogger("puppyone.authorization")


def redacted_project_ref(project_id: str) -> str:
    """Stable, non-reversible Project reference for decision telemetry."""

    return hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:12]


def _record_decision(
    *,
    project_id: str,
    action: ProjectAction,
    outcome: str,
    reason: str,
    grant: ProjectGrant | None = None,
) -> None:
    # Deliberately omit user id, organization id, project name, and raw Project
    # id.  Operations can aggregate outcome/action/reason while support may
    # correlate a Project only with an independently computed redacted ref.
    _logger.info(
        "project_authorization_decision",
        extra={
            "authorization_decision": {
                "project_ref": redacted_project_ref(project_id),
                "action": action.value,
                "outcome": outcome,
                "reason": reason,
                "role": grant.role.value if grant else None,
                "grant_source": grant.source.value if grant else None,
            }
        },
    )


class AuthorizationService:
    def __init__(self, repository: AuthorizationRepository):
        self._repository = repository

    @classmethod
    def authorize_facts(cls, facts, project_id, user_id, action):
        """Evaluate a caller-supplied consistent snapshot without database I/O.

        Composition queries return facts from authorization_project_facts; they
        never interpret roles or bypass this canonical policy.
        """
        if isinstance(facts, dict):
            facts = ProjectAuthorizationFacts(**facts)
        grant = (
            cls._grant_from_facts(facts, user_id)
            if facts and facts.project_id == project_id
            else None
        )
        return cls._authorize_grant(grant, project_id, action, True)

    @staticmethod
    def _grant_from_facts(facts: ProjectAuthorizationFacts, user_id: str) -> ProjectGrant | None:
        # Organization membership is the tenant boundary. An explicit Project
        # row can never admit a user who no longer belongs to the tenant.
        if facts.org_role is None:
            return None

        role: ProjectRole | None = None
        source: GrantSource | None = None
        if facts.org_role == "owner":
            role = ProjectRole.ADMIN
            source = GrantSource.ORG_OWNER
        elif facts.project_role is not None:
            # Tenant mismatch or unknown legacy roles fail closed.
            if facts.project_member_org_id != facts.org_id:
                return None
            try:
                role = ProjectRole(facts.project_role)
            except ValueError:
                return None
            source = GrantSource.PROJECT_MEMBER
        elif facts.visibility == "org":
            role = ProjectRole.VIEWER
            source = GrantSource.ORG_VISIBILITY

        if role is None or source is None:
            return None
        return ProjectGrant(
            project_id=facts.project_id,
            org_id=facts.org_id,
            user_id=user_id,
            role=role,
            source=source,
            capabilities=ROLE_CAPABILITIES[role],
        )

    def resolve_project_grant(self, project_id: str, user_id: str) -> ProjectGrant | None:
        cache = project_access_cache_var.get()
        cache_key = f"{_CACHE_PREFIX}:{project_id}:{user_id}"
        if cache is not None and cache_key in cache:
            return cache[cache_key]  # type: ignore[return-value]

        try:
            facts = self._repository.load_project_facts(project_id, user_id)
            grant = (
                self._grant_from_facts(facts, user_id)
                if facts and facts.project_id == project_id
                else None
            )
        except Exception as exc:
            # Authorization storage failure is never an allow signal, but it
            # is also not evidence that the Project or grant is absent. Keep
            # the decision fail-closed while preserving retryable 503
            # semantics for the caller.
            _logger.warning(
                "project_authorization_facts_unavailable",
                extra={
                    "project_ref": redacted_project_ref(project_id),
                    "error_type": type(exc).__name__,
                },
            )
            raise ServiceUnavailableException(
                "Project authorization is temporarily unavailable"
            ) from exc

        if cache is not None:
            cache[cache_key] = grant  # type: ignore[assignment]
        return grant

    def authorize(
        self,
        project_id: str,
        user_id: str,
        action: ProjectAction,
        *,
        conceal_missing_grant: bool = True,
    ) -> ProjectGrant:
        grant = self.resolve_project_grant(project_id, user_id)
        return self._authorize_grant(grant, project_id, action, conceal_missing_grant)

    @staticmethod
    def _authorize_grant(grant, project_id, action, conceal_missing_grant):
        if grant is None:
            _record_decision(
                project_id=project_id,
                action=action,
                outcome="deny",
                reason="missing_grant",
            )
            if conceal_missing_grant:
                raise NotFoundException(
                    f"Project not found: {project_id}", code=ErrorCode.NOT_FOUND
                )
            raise PermissionException(
                f"Action {action.value} is not permitted", code=ErrorCode.FORBIDDEN
            )
        if not grant.allows(action):
            _record_decision(
                project_id=project_id,
                action=action,
                outcome="deny",
                reason="insufficient_capability",
                grant=grant,
            )
            raise PermissionException(
                f"Action {action.value} requires a higher Project role",
                code=ErrorCode.FORBIDDEN,
            )
        _record_decision(
            project_id=project_id,
            action=action,
            outcome="allow",
            reason="capability_granted",
            grant=grant,
        )
        return grant

    def allows(self, project_id: str, user_id: str, action: ProjectAction) -> bool:
        grant = self.resolve_project_grant(project_id, user_id)
        allowed = bool(grant and grant.allows(action))
        _record_decision(
            project_id=project_id,
            action=action,
            outcome="allow" if allowed else "deny",
            reason=(
                "capability_granted"
                if allowed
                else "missing_grant"
                if grant is None
                else "insufficient_capability"
            ),
            grant=grant,
        )
        return allowed

    def filter_accessible(
        self, projects: list[Any], user_id: str
    ) -> list[tuple[Any, ProjectGrant]]:
        if not projects:
            return []
        try:
            facts_by_id = self._repository.load_project_facts_batch(
                [str(project.id) for project in projects], user_id
            )
        except Exception as exc:
            # A partial list is an authorization metadata leak. Fail the whole
            # decision set closed when its facts cannot be read consistently,
            # while distinguishing dependency outage from an empty result.
            _logger.warning(
                "project_authorization_batch_facts_unavailable",
                extra={"error_type": type(exc).__name__, "candidate_count": len(projects)},
            )
            raise ServiceUnavailableException(
                "Project authorization is temporarily unavailable"
            ) from exc
        accessible: list[tuple[Any, ProjectGrant]] = []
        for project in projects:
            project_id = str(project.id)
            facts = facts_by_id.get(project_id)
            grant = (
                self._grant_from_facts(facts, user_id)
                if facts and facts.project_id == project_id
                else None
            )
            cache = project_access_cache_var.get()
            if cache is not None:
                cache[f"{_CACHE_PREFIX}:{project_id}:{user_id}"] = grant
            if grant is not None:
                accessible.append((project, grant))
            _record_decision(
                project_id=project_id,
                action=ProjectAction.PROJECT_READ,
                outcome="allow" if grant is not None else "deny",
                reason="list_filter",
                grant=grant,
            )
        return accessible

    def accessible_project_ids(self, project_ids: list[str], user_id: str) -> list[str]:
        """Filter candidate IDs through the canonical policy in one batch.

        Candidate discovery may use tenant indexes for efficiency, but only
        this policy result is an authorization decision. Returning no IDs on a
        storage error avoids partial-list metadata leaks.
        """
        normalized = list(dict.fromkeys(str(value) for value in project_ids if value))
        if not normalized:
            return []
        try:
            facts_by_id = self._repository.load_project_facts_batch(normalized, user_id)
        except Exception as exc:
            _logger.warning(
                "project_authorization_batch_facts_unavailable",
                extra={
                    "error_type": type(exc).__name__,
                    "candidate_count": len(normalized),
                },
            )
            raise ServiceUnavailableException(
                "Project authorization is temporarily unavailable"
            ) from exc
        allowed: list[str] = []
        for project_id in normalized:
            facts = facts_by_id.get(project_id)
            grant = (
                self._grant_from_facts(facts, user_id)
                if facts and facts.project_id == project_id
                else None
            )
            cache = project_access_cache_var.get()
            if cache is not None:
                cache[f"{_CACHE_PREFIX}:{project_id}:{user_id}"] = grant
            if grant is not None:
                allowed.append(project_id)
            _record_decision(
                project_id=project_id,
                action=ProjectAction.PROJECT_READ,
                outcome="allow" if grant is not None else "deny",
                reason="list_filter",
                grant=grant,
            )
        return allowed
