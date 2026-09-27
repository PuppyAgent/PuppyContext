"""OAuth data models."""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class OAuthConnection(BaseModel):
    """OAuth connection model representing a user's connection to a platform."""

    id: int
    user_id: str
    provider: str
    access_token: str
    refresh_token: Optional[str] = None
    token_type: Optional[str] = None
    expires_at: Optional[datetime] = None
    workspace_id: Optional[str] = None
    workspace_name: Optional[str] = None
    bot_id: Optional[str] = None
    metadata: dict = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class OAuthConnectionCreate(BaseModel):
    """Model for creating a new OAuth connection."""

    user_id: str
    provider: str
    access_token: str
    refresh_token: Optional[str] = None
    token_type: Optional[str] = None
    expires_at: Optional[datetime] = None
    workspace_id: Optional[str] = None
    workspace_name: Optional[str] = None
    bot_id: Optional[str] = None
    metadata: dict = Field(default_factory=dict)


class OAuthConnectionUpdate(BaseModel):
    """Model for updating an existing OAuth connection."""

    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    token_type: Optional[str] = None
    expires_at: Optional[datetime] = None
    workspace_id: Optional[str] = None
    workspace_name: Optional[str] = None
    bot_id: Optional[str] = None
    metadata: Optional[dict] = None
