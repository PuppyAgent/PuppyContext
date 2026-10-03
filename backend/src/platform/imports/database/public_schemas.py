"""One-time Database Import source HTTP contracts; never Access credentials."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="before")
    @classmethod
    def no_blank_strings(cls, value):
        if isinstance(value, str) and not value.strip():
            raise ValueError("Explicit values must not be blank")
        return value


class ImportDatabaseSourceCreate(StrictRequest):
    name: str = Field(min_length=1)
    provider: str = "supabase"
    project_url: str = Field(min_length=1)
    api_key: str = Field(min_length=1)
    key_type: Literal["anon", "service_role"] = "anon"


class ImportDatabaseSource(BaseModel):
    id: str
    name: str
    provider: str
    project_id: str
    is_active: bool
    last_used_at: str | None = None
    created_at: str


class ImportDatabaseSourceCreated(BaseModel):
    source: ImportDatabaseSource
    database_info: dict


class ImportDatabaseSave(StrictRequest):
    name: str = Field(min_length=1)
    table: str = Field(min_length=1)
    limit: int = Field(default=1000, ge=1, le=10000)


class ImportDatabaseSaved(BaseModel):
    import_database_source_id: str
    content_path: str
    row_count: int


class ImportDatabaseTable(BaseModel):
    name: str
    type: str
    columns: list[dict] = Field(default_factory=list)


class ImportDatabasePreview(BaseModel):
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    execution_time_ms: float
