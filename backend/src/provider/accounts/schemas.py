"""Gateway Pydantic schemas."""
from __future__ import annotations

from pydantic import BaseModel, Field


class GatewayCreate(BaseModel):
    provider: str = Field(..., description="Provider type: gmail, github, notion, database, ...")
    name: str | None = Field(None, description="Display name (e.g. 'My Gmail', 'Prod DB')")
    credentials: dict = Field(default_factory=dict, description="Credentials (token, connection string, etc)")
    metadata: dict = Field(default_factory=dict, description="Provider-specific metadata")


class GatewayUpdate(BaseModel):
    name: str | None = None
    metadata: dict | None = None
    status: str | None = None


class GatewayOut(BaseModel):
    id: str
    org_id: str
    user_id: str
    provider: str
    name: str | None = None
    status: str = "active"
    metadata: dict = Field(default_factory=dict)
    # credentials intentionally excluded from output (sensitive)
    has_credentials: bool = False
    created_at: str | None = None
    updated_at: str | None = None


class GatewayDetail(GatewayOut):
    """Detailed view including credential status (not the actual credentials)."""
    credential_keys: list[str] = Field(default_factory=list)
    access_point_count: int = 0
