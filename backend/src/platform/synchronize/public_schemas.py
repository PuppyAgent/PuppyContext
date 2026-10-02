"""Canonical public Synchronize resources. No persistence or legacy wire aliases."""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class SynchronizeBindingCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str
    provider: str
    config: dict[str, Any]
    target_path: str | None = None
    credentials_ref: str | None = None
    direction: str = "inbound"
    conflict_strategy: str = "three_way_merge"
    sync_mode: Literal["manual", "scheduled", "realtime"] = "manual"
    trigger: dict[str, Any] | None = None


class SynchronizeBindingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: dict[str, Any] | None = None
    target_path: str | None = None
    direction: str | None = None
    conflict_strategy: str | None = None


class SynchronizeTriggerUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sync_mode: Literal["manual", "scheduled", "realtime"]
    trigger: dict[str, Any] | None = None


class SynchronizeBinding(BaseModel):
    id: str
    project_id: str
    path: str | None = None
    direction: str
    provider: str
    config: dict[str, Any]
    status: str
    last_synchronize_commit_id: str = ""
    error_message: str | None = None
    trigger: dict[str, Any] = Field(default_factory=dict)
    last_synced_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class SynchronizeExecutionResult(BaseModel):
    synchronize_binding_id: str
    synchronize_run_id: str | None = None
    worker_job_id: str | None = None
    path: str | None = None
    provider: str
    status: str
    commit_id: str | None = None
    direction: str | None = None
    summary: str | None = None
    deduped: bool = False


class SynchronizeBindingCreated(BaseModel):
    binding: SynchronizeBinding
    execution_result: SynchronizeExecutionResult | None = None


class SynchronizeRun(BaseModel):
    id: str
    synchronize_binding_id: str
    status: str
    worker_job_id: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None
    exit_code: int | None = None
    stdout: str | None = None
    error: str | None = None
    trigger_type: str | None = None
    result_summary: str | None = None


class SynchronizeFailedRun(BaseModel):
    id: str
    synchronize_binding_id: str
    synchronize_binding_name: str | None = None
    target_path: str | None = None
    provider: str = ""
    direction: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None
    error: str | None = None
    result_summary: str | None = None
    trigger_type: str | None = None


class SynchronizeStatusItem(BaseModel):
    id: str
    path: str | None = None
    node_name: str | None = None
    node_type: str | None = None
    provider: str
    direction: str
    status: str
    name: str | None = None
    trigger: dict[str, Any] | None = None
    last_synced_at: str | None = None
    error_message: str | None = None


class SynchronizeStatus(BaseModel):
    bindings: list[SynchronizeStatusItem]


class SynchronizeBootstrapResult(BaseModel):
    bindings_created: int


class SynchronizePullResult(BaseModel):
    synced: int
    results: list[SynchronizeExecutionResult]


class SynchronizePushResult(BaseModel):
    pushed: int
    results: list[SynchronizeExecutionResult]
