"""Human repository management; canonical publication stays in the ref engine."""

from __future__ import annotations

import base64
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from postgrest.exceptions import APIError
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.common_schemas import ApiResponse
from src.platform.authorization.dependencies import AuthorizedProject, require_project_action
from src.platform.authorization.models import ProjectAction
from src.platform.project.schemas import NativeRepositoryCreate
from src.platform.project.write_lease import ProjectWriteLease
from src.version_engine.bootstrap.dependencies import get_repo_manager
from src.version_engine.infrastructure.owned_work import run_owned
from src.version_engine.infrastructure.supabase.repo_manager import VersionRepoManager
from src.version_engine.read.ref_metadata import repository_ref_metadata
from src.version_engine.write_engine.ref_transaction import RefEdit, RefState, admitted_actor

management_router = APIRouter()


class RepositoryHeadState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["symbolic", "oid"]
    target_b64: str | None = None
    oid: str | None = None

    @model_validator(mode="after")
    def validate_state(self):
        if self.kind == "symbolic":
            if not self.target_b64 or self.oid is not None:
                raise ValueError("symbolic HEAD requires only target_b64")
            target = base64.b64decode(self.target_b64, validate=True)
            if base64.b64encode(target).decode() != self.target_b64:
                raise ValueError("non-canonical base64")
            RefState(target=target).wire("sha1")
        elif self.target_b64 is not None or not self.oid:
            raise ValueError("direct HEAD requires only oid")
        return self

    def state(self):
        return (
            RefState(target=base64.b64decode(self.target_b64))
            if self.kind == "symbolic"
            else RefState(oid=self.oid)
        )


class RepositoryHeadUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_key: UUID
    generation: int = Field(ge=1)
    expected: RepositoryHeadState
    target_branch: str

    @model_validator(mode="after")
    def validate_branch(self):
        NativeRepositoryCreate(profile="native", default_branch=self.target_branch)
        return self


def read_native_head(manager, grant):
    service = manager.get_native_service(grant.project_id)
    if service is None:
        return None
    metadata = repository_ref_metadata(service.control, grant.project_id, grant)
    refs = {row["name_b64"]: row["state"] for row in metadata.pop("refs")}
    head = refs[base64.b64encode(b"HEAD").decode()]
    selected = refs.get(head.get("target_b64"), {}) if head["kind"] == "symbolic" else head
    return {**metadata, "head": head, "head_commit_id": selected.get("oid", "")}


def change_head(manager, grant, payload):
    service = manager.get_native_service(grant.project_id)
    if service is None:
        raise HTTPException(409, "Native repository required")
    edit = RefEdit(
        b"HEAD",
        payload.expected.state(),
        RefState(target=("refs/heads/" + payload.target_branch).encode()),
    )
    edit.wire(service.object_format)
    def submit():
        return service.submit(
            grant,
            request_key=str(payload.request_key),
            generation=payload.generation,
            edits=[edit],
            roots={},
            prepare=lambda: None,
            message="change repository HEAD",
        )
    try:
        return submit()
    except Exception as exc:
        if isinstance(exc, APIError) and exc.code == "22023":
            raise
        actor = admitted_actor(grant, grant.project_id, write=False)
        result = service.control.recover_result(grant.project_id, actor, str(payload.request_key))
        if result is None:
            raise
        # A stored result proves an outcome exists, not that this request has
        # the same contents. Replay through the engine so SQL validates the
        # original digest before returning a recovered acknowledgement.
        return submit()


@management_router.put(
    "/{project_id}/head",
    response_model=ApiResponse[dict],
    summary="Change native symbolic HEAD with an explicit expected state",
)
async def update_repository_head(
    payload: RepositoryHeadUpdate,
    authorized: AuthorizedProject = Depends(require_project_action(ProjectAction.PROJECT_MANAGE)),
    manager: VersionRepoManager = Depends(get_repo_manager),
):
    try:
        async with ProjectWriteLease(authorized.grant.project_id, "repository.head"):
            result = await run_owned(change_head, manager, authorized.grant, payload)
    except ValueError as exc:
        raise HTTPException(422, "Invalid HEAD state for this repository") from exc
    except PermissionError as exc:
        raise HTTPException(403, "Repository action denied") from exc
    except APIError as exc:
        if exc.code == "22023":
            raise HTTPException(
                409 if exc.message == "request_key_reused" else 422,
                "Request key already used for different contents"
                if exc.message == "request_key_reused"
                else "Invalid repository management request",
            ) from exc
        raise HTTPException(
            403 if exc.code == "42501" else 503, "Repository management unavailable"
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            503,
            "Repository management outcome unavailable; query the operation before retrying",
            headers={"Cache-Control": "no-store"},
        ) from exc
    if result["status"] != "committed":
        raise HTTPException(409, {"code": "repository_head_conflict", "result": result})
    return ApiResponse.success(data=result)
