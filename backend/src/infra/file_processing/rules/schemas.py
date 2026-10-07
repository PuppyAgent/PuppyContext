"""
ETL Rule Schemas

Pydantic models for ETL transformation rules.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

PostprocessMode = Literal["llm", "skip"]


def parse_rule_payload(
    raw_json_schema: dict[str, Any],
) -> tuple[PostprocessMode, str | None, dict[str, Any]]:
    """
    Backward compatible parsing for rule json_schema stored in DB.

    - Legacy format: raw json schema (has "type")
    - New format: {"schema": <json schema>, "_etl": {"postprocess_mode": "...", "postprocess_strategy": "..."}}
    """
    if not isinstance(raw_json_schema, dict):
        return "llm", None, {"type": "object"}

    if "_etl" in raw_json_schema:
        meta = raw_json_schema.get("_etl") or {}
        mode = meta.get("postprocess_mode") or "llm"
        if mode not in ("llm", "skip"):
            mode = "llm"
        strategy = meta.get("postprocess_strategy")
        schema = raw_json_schema.get("schema") or {"type": "object"}
        if not isinstance(schema, dict) or "type" not in schema:
            schema = {"type": "object"}
        return mode, strategy, schema

    # Legacy schema
    if "type" in raw_json_schema:
        return "llm", None, raw_json_schema

    return "llm", None, {"type": "object"}


def build_rule_payload(
    *,
    json_schema: dict[str, Any] | None,
    postprocess_mode: PostprocessMode,
    postprocess_strategy: str | None,
) -> dict[str, Any]:
    schema = json_schema or {"type": "object"}
    if not isinstance(schema, dict) or "type" not in schema:
        schema = {"type": "object"}
    return {
        "schema": schema,
        "_etl": {
            "postprocess_mode": postprocess_mode,
            "postprocess_strategy": postprocess_strategy,
        },
    }


class ETLRule(BaseModel):
    """ETL transformation rule definition."""

    rule_id: str = Field(..., description="Unique rule identifier")
    name: str = Field(..., description="Rule name")
    description: str = Field(..., description="Rule description")
    json_schema: dict[str, Any] = Field(
        ..., description="JSON Schema for output structure (effective)"
    )
    postprocess_mode: PostprocessMode = Field(
        default="llm", description="Postprocess mode: llm|skip"
    )
    postprocess_strategy: str | None = Field(
        default=None, description="Postprocess strategy (optional)"
    )
    system_prompt: str | None = Field(
        None, description="System prompt to guide LLM transformation"
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("json_schema")
    @classmethod
    def validate_json_schema(cls, v: dict[str, Any]) -> dict[str, Any]:
        """Validate that json_schema is a valid JSON Schema object."""
        if not isinstance(v, dict):
            raise ValueError("json_schema must be a dictionary")
        if "type" not in v:
            raise ValueError("json_schema must have a 'type' field")
        return v


class RuleCreateRequest(BaseModel):
    """Request to create a new ETL rule."""

    name: str = Field(..., description="Rule name")
    description: str = Field(..., description="Rule description")
    json_schema: dict[str, Any] | None = Field(
        default=None, description="JSON Schema for output (required for llm)"
    )
    postprocess_mode: PostprocessMode = Field(default="llm", description="llm|skip")
    postprocess_strategy: str | None = Field(
        default=None, description="Postprocess strategy (optional)"
    )
    system_prompt: str | None = Field(None, description="Optional system prompt")

    @field_validator("json_schema")
    @classmethod
    def validate_create_schema(
        cls, v: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if v is None:
            return None
        if not isinstance(v, dict):
            raise ValueError("json_schema must be a dictionary")
        return v


class RuleUpdateRequest(BaseModel):
    """Request to update an existing ETL rule."""

    name: str | None = Field(None, description="Rule name")
    description: str | None = Field(None, description="Rule description")
    json_schema: dict[str, Any] | None = Field(None, description="JSON Schema")
    system_prompt: str | None = Field(None, description="System prompt")
    postprocess_mode: PostprocessMode | None = Field(
        default=None, description="llm|skip"
    )
    postprocess_strategy: str | None = Field(
        default=None, description="Postprocess strategy"
    )


class TransformationResult(BaseModel):
    """Result of applying an ETL rule."""

    success: bool = Field(..., description="Whether transformation succeeded")
    output: dict[str, Any] | list[Any] | None = Field(
        None, description="Transformed JSON output (dict or list)"
    )
    error: str | None = Field(None, description="Error message if failed")
    llm_usage: dict[str, Any] | None = Field(
        None, description="LLM usage statistics"
    )
