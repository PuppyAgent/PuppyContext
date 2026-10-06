from __future__ import annotations

import math
import secrets
from dataclasses import dataclass
from typing import Any, Literal

from src.exceptions import AppException, ErrorCode, NotFoundException, PermissionException
from src.infra.supabase.client import SupabaseClient
from src.platform.idempotency import canonical_payload_hash, raise_idempotency_outcome
from src.platform.project.models import Project
from src.platform.project.supabase_schemas import ProjectResponse
from src.utils.id_generator import generate_uuid_v7

ProjectPublicationMode = Literal["empty", "deferred"]


def _rpc_object(data: Any, *, operation: str) -> dict[str, Any]:
    if isinstance(data, list) and len(data) == 1:
        data = data[0]
    if not isinstance(data, dict):
        raise RuntimeError(f"{operation} returned an invalid response")
    return data


def _raise_inventory_gate(error: Exception) -> None:
    details = " ".join(str(part) for part in getattr(error, "args", (error,)))
    details = f"{getattr(error, 'message', '')} {details}"
    if "Project storage inventory is incomplete" not in details:
        return
    raise AppException(
        code=ErrorCode.REPOSITORY_STORAGE_UNAVAILABLE,
        status_code=503,
        message="Project deletion is temporarily unavailable during storage inventory",
        details={
            "code": "project_storage_inventory_incomplete",
            "retryable": True,
        },
    ) from error


@dataclass(frozen=True, slots=True)
class IdempotentProjectResult:
    project: Project
    replayed: bool
    ready: bool = True


@dataclass(frozen=True, slots=True)
class ProjectCreationReplay:
    project: Project
    result_metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ProjectDeletionResult:
    project_id: str
    deletion_job_id: str
    status: str
    replayed: bool = False

    def as_dict(self) -> dict[str, str]:
        return {
            "project_id": self.project_id,
            "deletion_job_id": self.deletion_job_id,
            "status": self.status,
        }


class ProjectControlPlaneRepository:
    """Persistence boundary for externally retried Project lifecycle writes."""

    def __init__(self, client=None):
        self._client = client or SupabaseClient().get_client()

    def create_project(self, params: dict[str, Any]) -> dict[str, Any]:
        operation = "create_native_project_repository" if "p_object_format" in params else "create_project_idempotent"
        response = self._client.rpc(operation, params).execute()
        return _rpc_object(response.data, operation=operation)

    def get_project_create_replay(self, params: dict[str, Any]) -> dict[str, Any]:
        response = self._client.rpc(
            "get_project_create_operation_replay",
            params,
        ).execute()
        return _rpc_object(
            response.data,
            operation="get_project_create_operation_replay",
        )

    def abandon_initialization(
        self,
        *,
        project_id: str,
        operation_key: str,
        actor_user_id: str,
        quiescence_seconds: int,
        worker_id: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "p_project_id": project_id,
            "p_operation_key": operation_key,
            "p_actor_user_id": actor_user_id,
            "p_quiescence_seconds": quiescence_seconds,
        }
        if worker_id is not None:
            params["p_worker_id"] = worker_id
        try:
            response = self._client.rpc(
                "abandon_project_initialization",
                params,
            ).execute()
        except Exception as error:
            _raise_inventory_gate(error)
            raise
        return _rpc_object(response.data, operation="abandon_project_initialization")

    def complete_initialization(
        self,
        *,
        project_id: str,
        operation_key: str,
        actor_user_id: str,
    ) -> dict[str, Any]:
        response = self._client.rpc(
            "complete_project_initialization",
            {
                "p_project_id": project_id,
                "p_operation_key": operation_key,
                "p_actor_user_id": actor_user_id,
            },
        ).execute()
        return _rpc_object(response.data, operation="complete_project_initialization")

    def abort_deferred_publication(
        self,
        *,
        project_id: str,
        operation_key: str,
        actor_user_id: str,
        quiescence_seconds: int,
        worker_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            response = self._client.rpc(
                "abort_deferred_project_publication",
                {
                    "p_project_id": project_id,
                    "p_operation_key": operation_key,
                    "p_actor_user_id": actor_user_id,
                    "p_quiescence_seconds": quiescence_seconds,
                    "p_worker_id": worker_id,
                },
            ).execute()
        except Exception as error:
            _raise_inventory_gate(error)
            raise
        return _rpc_object(
            response.data,
            operation="abort_deferred_project_publication",
        )

    def claim_initializations(
        self,
        *,
        worker_id: str,
        limit: int,
        lease_seconds: int,
    ) -> list[dict[str, Any]]:
        response = self._client.rpc(
            "claim_project_initialization_operations",
            {
                "p_worker_id": worker_id,
                "p_limit": limit,
                "p_lease_seconds": lease_seconds,
            },
        ).execute()
        return [row for row in (response.data or []) if isinstance(row, dict)]

    def fail_initialization(
        self,
        *,
        operation_key: str,
        actor_user_id: str,
        worker_id: str,
        error: str,
        retry_after_seconds: int,
    ) -> bool:
        response = self._client.rpc(
            "fail_project_initialization_operation",
            {
                "p_operation_key": operation_key,
                "p_actor_user_id": actor_user_id,
                "p_worker_id": worker_id,
                "p_error": error,
                "p_retry_after_seconds": retry_after_seconds,
            },
        ).execute()
        return bool(response.data)

    def dead_letter_initialization(
        self,
        *,
        operation_key: str,
        actor_user_id: str,
        worker_id: str,
        error: str,
    ) -> bool:
        response = self._client.rpc(
            "dead_letter_project_initialization_operation",
            {
                "p_operation_key": operation_key,
                "p_actor_user_id": actor_user_id,
                "p_worker_id": worker_id,
                "p_error": error,
            },
        ).execute()
        return bool(response.data)

    def delete_project(
        self,
        *,
        project_id: str,
        actor_user_id: str,
        quiescence_seconds: int,
    ) -> dict[str, Any]:
        try:
            response = self._client.rpc(
                "delete_project_control_plane",
                {
                    "p_project_id": project_id,
                    "p_actor_user_id": actor_user_id,
                    "p_quiescence_seconds": quiescence_seconds,
                },
            ).execute()
        except Exception as error:
            _raise_inventory_gate(error)
            raise
        return _rpc_object(response.data, operation="delete_project_control_plane")


class ProjectControlPlaneService:
    def __init__(
        self,
        repository: ProjectControlPlaneRepository,
        *,
        deletion_quiescence_seconds: int = 3600,
    ):
        self._repository = repository
        self._deletion_quiescence_seconds = deletion_quiescence_seconds

    def create_project(
        self,
        *,
        operation_key: str,
        name: str,
        description: str | None,
        org_id: str,
        actor_user_id: str,
        publication_mode: ProjectPublicationMode,
        source_fingerprint: dict[str, Any],
        project_limit: int | float | None,
        request_fingerprint: dict[str, Any] | None = None,
        result_metadata: dict[str, Any] | None = None,
        native_repository: dict[str, str] | None = None,
    ) -> IdempotentProjectResult:
        canonical_payload = {
            "description": description,
            "name": name,
            "org_id": org_id,
            "publication_mode": publication_mode,
            "source": source_fingerprint,
        }
        if native_repository is not None:
            canonical_payload["repository"] = native_repository
        maximum = _normalize_project_limit(project_limit)
        params = {
                "p_operation_key": operation_key,
                "p_payload_hash": canonical_payload_hash(canonical_payload),
                "p_project_id": generate_uuid_v7(),
                "p_name": name,
                "p_description": description,
                "p_org_id": org_id,
                "p_created_by": actor_user_id,
                "p_share_token": f"prj_{secrets.token_urlsafe(24)}",
                "p_publication_mode": publication_mode,
                "p_project_limit": maximum,
                "p_request_hash": canonical_payload_hash(
                    request_fingerprint or canonical_payload
                ),
                "p_result_metadata": result_metadata or {},
            }
        if native_repository is not None:
            if publication_mode != "empty":
                raise ValueError("native repositories require empty publication")
            params.update(p_object_format=native_repository["object_format"],
                          p_default_branch=native_repository["default_branch"])
        outcome = self._repository.create_project(params)
        result = str(outcome.get("outcome") or "")
        if result in {"conflict", "gone", "invalid"}:
            raise_idempotency_outcome(result, resource="project_create")
        if result == "dead_lettered":
            _raise_dead_lettered_publication()
        if result == "forbidden":
            raise PermissionException("Not a member of this organization", code=ErrorCode.FORBIDDEN)
        if result == "capacity_exceeded":
            raise AppException(
                code=ErrorCode.FORBIDDEN,
                status_code=403,
                message="Entitlement required",
                details={
                    "code": "entitlement_required",
                    "reason": "limit_exceeded",
                    "limit": "projects.max",
                    "org_id": org_id,
                    "current": outcome.get("current"),
                    "maximum": outcome.get("maximum"),
                },
            )
        if result not in {
            "native_created",
            "initializing_created",
            "initializing_replayed",
            "replayed",
        }:
            raise RuntimeError(f"Unexpected Project create outcome: {result or 'missing'}")
        project_data = outcome.get("project")
        if not isinstance(project_data, dict):
            raise RuntimeError("Project create outcome did not include a Project")
        return IdempotentProjectResult(
            project=_project_from_row(project_data),
            replayed=result not in {"initializing_created", "native_created"},
            ready=result in {"replayed", "native_created"},
        )

    def preflight_project_creation(
        self,
        *,
        operation_key: str,
        actor_user_id: str,
        request_fingerprint: dict[str, Any],
    ) -> ProjectCreationReplay | None:
        """Return a completed durable response without touching workflow sources."""

        outcome = self._repository.get_project_create_replay(
            {
                "p_operation_key": operation_key,
                "p_actor_user_id": actor_user_id,
                "p_request_hash": canonical_payload_hash(request_fingerprint),
            }
        )
        result = str(outcome.get("outcome") or "")
        if result == "not_found":
            return None
        if result == "initializing":
            raise AppException(
                code=ErrorCode.VERSION_CONFLICT,
                status_code=409,
                message="Project publication is already in progress",
                details={"code": "project_publication_in_progress"},
            )
        if result in {"conflict", "gone", "invalid", "lifecycle_conflict"}:
            raise_idempotency_outcome(
                "gone" if result in {"gone", "lifecycle_conflict"} else result,
                resource="project_create",
            )
        if result == "dead_lettered":
            _raise_dead_lettered_publication()
        if result == "forbidden":
            raise PermissionException(
                "Project publication is no longer accessible to this user"
            )
        if result != "replayed":
            raise RuntimeError(f"Unexpected Project replay outcome: {result or 'missing'}")
        project_data = outcome.get("project")
        result_metadata = outcome.get("result_metadata")
        if not isinstance(project_data, dict) or not isinstance(result_metadata, dict):
            raise RuntimeError("Project replay outcome omitted durable response metadata")
        return ProjectCreationReplay(
            project=_project_from_row(project_data),
            result_metadata=result_metadata,
        )

    def abort_deferred_publication(
        self,
        *,
        project_id: str,
        operation_key: str,
        actor_user_id: str,
        worker_id: str | None = None,
    ) -> ProjectDeletionResult:
        outcome = self._repository.abort_deferred_publication(
            project_id=project_id,
            operation_key=operation_key,
            actor_user_id=actor_user_id,
            quiescence_seconds=self._deletion_quiescence_seconds,
            worker_id=worker_id,
        )
        result = str(outcome.get("outcome") or "")
        if result == "conflict":
            raise_idempotency_outcome("conflict", resource="project_publication")
        if result == "gone":
            raise_idempotency_outcome("gone", resource="project_publication")
        if result == "not_found":
            raise NotFoundException("Project publication operation not found")
        if result == "not_abortable":
            raise AppException(
                code=ErrorCode.VERSION_CONFLICT,
                status_code=409,
                message="Project publication can no longer be aborted",
                details={"code": "publication_not_abortable"},
            )
        if result not in {"accepted", "replayed"}:
            raise RuntimeError(
                f"Unexpected Project publication abort outcome: {result or 'missing'}"
            )
        return _deletion_result(project_id, outcome, replayed=result == "replayed")

    def complete_project_initialization(
        self,
        *,
        project_id: str,
        operation_key: str,
        actor_user_id: str,
        replayed: bool,
    ) -> IdempotentProjectResult:
        outcome = self._repository.complete_initialization(
            project_id=project_id,
            operation_key=operation_key,
            actor_user_id=actor_user_id,
        )
        result = str(outcome.get("outcome") or "")
        if result == "conflict":
            raise_idempotency_outcome("conflict", resource="project_create")
        if result == "gone":
            raise_idempotency_outcome("gone", resource="project_create")
        if result == "not_found":
            raise NotFoundException("Project initialization operation not found")
        if result == "root_not_initialized":
            raise RuntimeError("Version Engine did not initialize the canonical Project root")
        if result not in {"completed", "replayed"}:
            raise RuntimeError(
                f"Unexpected Project initialization completion outcome: {result or 'missing'}"
            )
        project_data = outcome.get("project")
        if not isinstance(project_data, dict):
            raise RuntimeError("Project initialization completion omitted its Project")
        return IdempotentProjectResult(
            project=_project_from_row(project_data),
            replayed=replayed,
            ready=True,
        )

    def abandon_initialization(
        self,
        *,
        project_id: str,
        operation_key: str,
        actor_user_id: str,
    ) -> ProjectDeletionResult:
        outcome = self._repository.abandon_initialization(
            project_id=project_id,
            operation_key=operation_key,
            actor_user_id=actor_user_id,
            quiescence_seconds=self._deletion_quiescence_seconds,
        )
        result = str(outcome.get("outcome") or "")
        if result == "conflict":
            raise_idempotency_outcome("conflict", resource="project_initialization")
        if result == "gone":
            raise_idempotency_outcome("gone", resource="project_initialization")
        if result == "not_found":
            raise NotFoundException("Project initialization operation not found")
        if result == "forbidden":
            raise PermissionException("Project initialization cannot be abandoned by this user")
        if result == "not_abandonable":
            raise AppException(
                code=ErrorCode.VERSION_CONFLICT,
                status_code=409,
                message="Project initialization can no longer be abandoned",
                details={"code": "initialization_not_abandonable"},
            )
        if result not in {"accepted", "replayed"}:
            raise RuntimeError(f"Unexpected Project abandon outcome: {result or 'missing'}")
        return _deletion_result(project_id, outcome, replayed=result == "replayed")

    def delete_project(self, *, project_id: str, actor_user_id: str) -> ProjectDeletionResult:
        outcome = self._repository.delete_project(
            project_id=project_id,
            actor_user_id=actor_user_id,
            quiescence_seconds=self._deletion_quiescence_seconds,
        )
        result = str(outcome.get("outcome") or "")
        if result == "not_found":
            raise NotFoundException(f"Project not found: {project_id}")
        if result == "forbidden":
            raise PermissionException("Project Admin access is required to delete this Project")
        if result != "deleted":
            raise RuntimeError(f"Unexpected Project delete outcome: {result or 'missing'}")
        return _deletion_result(project_id, outcome)


def _normalize_project_limit(value: int | float | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise RuntimeError("projects.max entitlement must be a finite integer or null")
    normalized = int(value)
    if normalized != value or normalized < 0:
        raise RuntimeError("projects.max entitlement must be a non-negative integer or null")
    return normalized


def _raise_dead_lettered_publication() -> None:
    raise AppException(
        code=ErrorCode.INTERNAL_SERVER_ERROR,
        status_code=503,
        message="Project publication could not be completed",
        details={
            "code": "project_publication_dead_lettered",
            "retryable": False,
        },
    )


def _project_from_row(row: dict[str, Any]) -> Project:
    response = ProjectResponse.model_validate(row)
    if not response.org_id:
        raise RuntimeError("Project create outcome is missing org_id")
    return Project(
        id=response.id,
        name=response.name,
        description=response.description,
        org_id=response.org_id,
        visibility=response.visibility,
        bound_git_branch=response.bound_git_branch,
        created_by=response.created_by,
        created_at=response.created_at,
        updated_at=response.updated_at,
        share_token=response.share_token,
    )


def _deletion_result(
    project_id: str,
    outcome: dict[str, Any],
    *,
    replayed: bool = False,
) -> ProjectDeletionResult:
    job = outcome.get("job")
    if not isinstance(job, dict) or not str(job.get("id") or ""):
        raise RuntimeError("Project deletion outcome did not include a deletion job")
    return ProjectDeletionResult(
        project_id=project_id,
        deletion_job_id=str(job["id"]),
        status=str(job.get("status") or "pending"),
        replayed=replayed,
    )
