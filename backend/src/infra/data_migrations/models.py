"""Manifest and operator-facing plan models."""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MIGRATION_ID_RE = re.compile(r"^[0-9]{8,14}_[a-z0-9]+(?:_[a-z0-9]+)*$")
SCHEMA_VERSION_RE = re.compile(r"^[0-9]{14}$")
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
CONTRACT_KEY_RE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
ARTIFACT_FILE_RE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*\.(?:sql|py)$")


class MigrationKind(StrEnum):
    SQL = "sql"
    PYTHON = "python"


def _validate_relative_file(value: str, *, suffix: str, field_name: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or len(path.parts) != 1:
        raise ValueError(f"{field_name} must be one file inside the migration directory")
    if path.suffix != suffix:
        raise ValueError(f"{field_name} must end with {suffix}")
    if not ARTIFACT_FILE_RE.fullmatch(value):
        raise ValueError(f"{field_name} must use a lowercase snake_case filename")
    return value


class DataMigrationManifest(BaseModel):
    """Versioned, deliberately small migration contract."""

    model_config = ConfigDict(extra="forbid")

    api_version: Literal[1] = 1
    id: str
    description: str = Field(min_length=8, max_length=240)
    kind: MigrationKind
    entrypoint: str
    verify: str = "verify.sql"
    requires_schema: list[str] = Field(min_length=1)
    required_env: list[str] = Field(default_factory=list)
    timeout_seconds: int = Field(default=3600, ge=1, le=86400)
    batch_size: int | None = Field(default=None, ge=1, le=100000)
    contract_key: str | None = None
    legacy: bool = False

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not MIGRATION_ID_RE.fullmatch(value):
            raise ValueError("id must be a timestamp prefix followed by snake_case words")
        return value

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("description cannot be blank")
        return value

    @field_validator("verify")
    @classmethod
    def validate_verify(cls, value: str) -> str:
        return _validate_relative_file(value, suffix=".sql", field_name="verify")

    @field_validator("requires_schema")
    @classmethod
    def validate_schema_versions(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("requires_schema cannot contain duplicates")
        for value in values:
            if not SCHEMA_VERSION_RE.fullmatch(value):
                raise ValueError(f"invalid Supabase schema version: {value}")
        return values

    @field_validator("required_env")
    @classmethod
    def validate_environment_names(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("required_env cannot contain duplicates")
        for value in values:
            if not ENV_NAME_RE.fullmatch(value):
                raise ValueError(f"invalid environment variable name: {value}")
        return values

    @field_validator("contract_key")
    @classmethod
    def validate_contract_key(cls, value: str | None) -> str | None:
        if value is not None and not CONTRACT_KEY_RE.fullmatch(value):
            raise ValueError("contract_key must use snake_case")
        return value

    @model_validator(mode="after")
    def validate_entrypoint(self) -> DataMigrationManifest:
        suffix = ".sql" if self.kind is MigrationKind.SQL else ".py"
        self.entrypoint = _validate_relative_file(
            self.entrypoint,
            suffix=suffix,
            field_name="entrypoint",
        )
        if self.kind is MigrationKind.SQL and self.batch_size is not None:
            raise ValueError("batch_size is supported only by Python migrations")
        return self


class MigrationState(StrEnum):
    READY = "ready"
    COMPLETED = "completed"
    BLOCKED = "blocked"


class MigrationPlan(BaseModel):
    """Stable plan output used by humans and CI."""

    id: str
    kind: MigrationKind
    state: MigrationState
    checksum: str
    legacy: bool
    missing_schema: list[str] = Field(default_factory=list)
    missing_environment: list[str] = Field(default_factory=list)
    completed_source_sha: str | None = None
    retired: bool = False
