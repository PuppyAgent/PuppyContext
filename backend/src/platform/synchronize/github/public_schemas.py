"""Canonical GitHub Synchronize contracts, independent of storage-era aliases."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="before")
    @classmethod
    def no_blank_strings(cls, value):
        if isinstance(value, str) and not value.strip():
            raise ValueError("Explicit values must not be blank")
        return value


class SynchronizeGithubBindingCreate(StrictRequest):
    oauth_connection_id: int = Field(gt=0)
    github_repo_owner: str = Field(min_length=1)
    github_repo_name: str = Field(min_length=1)
    default_branch: str = Field(default="main", min_length=1)
    auto_pull: bool = False
    webhook_secret: str | None = None


class SynchronizeGithubBindingUpdate(StrictRequest):
    default_branch: str | None = Field(default=None, min_length=1)
    auto_pull: bool | None = None
    webhook_secret: str | None = None

    @field_validator("default_branch", "auto_pull", mode="before")
    @classmethod
    def non_null_updates(cls, value):
        if value is None:
            raise ValueError("Omit an unchanged field instead of setting it to null")
        return value


class SynchronizeGithubBinding(BaseModel):
    id: str
    project_id: str
    oauth_connection_id: int | None
    github_repo_owner: str
    github_repo_name: str
    default_branch: str
    auto_pull: bool
    has_webhook_secret: bool
    last_pulled_sha: str | None
    last_pulled_at: datetime | None
    last_pushed_sha: str | None
    last_pushed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class SynchronizeGithubPull(StrictRequest):
    branch: str | None = None
    force: bool = False


class SynchronizeGithubPush(StrictRequest):
    branch: str | None = None
    message: str | None = None


GithubDirection = Literal["inbound", "outbound"]
GithubResultStatus = Literal["pending", "success", "failed", "conflict"]


class SynchronizeGithubResult(BaseModel):
    synchronize_github_binding_id: str
    status: GithubResultStatus
    direction: GithubDirection
    git_sha: str | None
    version_commit_id: str | None
    files_changed: int | None
    error_message: str | None = None


class SynchronizeGithubLog(BaseModel):
    id: str
    synchronize_github_binding_id: str
    direction: GithubDirection
    git_sha: str | None
    version_commit_id: str | None
    status: GithubResultStatus
    error_message: str | None
    files_changed: int | None
    created_at: datetime


class SynchronizeGithubLogs(BaseModel):
    synchronize_github_binding_id: str
    entries: list[SynchronizeGithubLog]
    total: int


class SynchronizeGithubRepo(BaseModel):
    owner: str
    name: str
    full_name: str
    default_branch: str
    private: bool


class SynchronizeGithubRepos(BaseModel):
    repos: list[SynchronizeGithubRepo]


class SynchronizeGithubBranch(BaseModel):
    name: str
    sha: str
    protected: bool = False
    is_default: bool = False


class SynchronizeGithubBranches(BaseModel):
    repo_owner: str
    repo_name: str
    branches: list[SynchronizeGithubBranch]
