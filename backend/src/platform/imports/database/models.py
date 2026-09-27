"""Database Import Models"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class DBConnection(BaseModel):
    """External database access."""

    id: str = Field(..., description="Connector ID")
    created_by: Optional[str] = Field(None, description="Creator user ID (nullable)")
    project_id: str = Field(..., description="Owning project ID")
    name: str = Field(..., description="Connector name")
    provider: str = Field("supabase", description="Database type: supabase | postgres | mysql")
    config: dict = Field(default_factory=dict, description="Access configuration")
    is_active: bool = Field(True, description="Whether active")
    last_used_at: Optional[datetime] = None
    created_at: datetime = Field(...)
    updated_at: datetime = Field(...)

    class Config:
        from_attributes = True
