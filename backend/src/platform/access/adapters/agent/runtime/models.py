from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

TERMINAL = frozenset({"succeeded", "stopped", "failed", "conflict", "outcome_unknown"})


class SubmitRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=128)
    agent_id: str | None = Field(default=None, min_length=1, max_length=128)
    scope_id: str | None = Field(default=None, min_length=1, max_length=128)
    session_id: str | None = Field(default=None, min_length=1, max_length=128)
    request_id: UUID
    prompt: str = Field(min_length=1, max_length=100000)


class Approval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision_id: UUID
    allow: bool


def public_run(run: dict, tools=()):
    return {
        key: run[key]
        for key in (
            "id",
            "project_id",
            "agent_id",
            "session_id",
            "request_id",
            "prompt",
            "state",
            "execution_id",
            "sequence",
            "stop_requested",
            "snapshot",
            "publication",
            "created_at",
            "updated_at",
            "deadline",
        )
    } | {
        "tools": [
            {key: tool.get(key) for key in ("call_id", "name", "input", "state")}
            | {"result": (tool.get("result") or {}).get("pi_result")}
            for tool in tools
        ]
    }
