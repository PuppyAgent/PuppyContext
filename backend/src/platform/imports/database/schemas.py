"""Database Import API Schemas"""

from typing import Any, Optional, List, Literal
from pydantic import BaseModel, Field


# === Request ===

class CreateConnectionRequest(BaseModel):
    """Create access request."""
    name: str = Field(..., description="Access point name", examples=["My Supabase"])
    provider: str = Field("supabase", description="Database type", examples=["supabase"])
    project_url: str = Field(..., description="Supabase Project URL", examples=["https://abcdefg.supabase.co"])
    api_key: str = Field(..., description="Supabase API Key (anon or service_role)")
    key_type: Literal["anon", "service_role"] = Field(
        default="anon",
        description="Key type for security tracking (anon recommended, service_role has full permissions)"
    )

    def to_config(self) -> dict:
        return {
            "project_url": self.project_url,
            "api_key": self.api_key,
            "key_type": self.key_type,
        }


class SaveTableRequest(BaseModel):
    """Save an entire table to the project."""
    name: str = Field(..., description="Save name", examples=["users"])
    table: str = Field(..., description="Table name")
    limit: int = Field(1000, ge=1, le=10000, description="Maximum number of rows")


# === Response ===

class ConnectionResponse(BaseModel):
    id: str
    name: str
    provider: str
    project_id: str
    is_active: bool
    last_used_at: Optional[str] = None
    created_at: str


class ConnectionCreatedResponse(BaseModel):
    connection: ConnectionResponse
    database_info: dict


class TableInfoResponse(BaseModel):
    name: str
    type: str
    columns: List[dict] = Field(default_factory=list)


class TablePreviewResponse(BaseModel):
    """Table data preview."""
    columns: List[str]
    rows: List[dict[str, Any]]
    row_count: int
    execution_time_ms: float


class SaveResultResponse(BaseModel):
    content_path: str
    row_count: int


class ConnectionErrorDetail(BaseModel):
    """Structured error detail for frontend display"""
    error_code: str | None = Field(None, description="Error code for programmatic handling")
    message: str = Field(..., description="User-friendly error message")
    suggested_actions: List[str] = Field(default_factory=list, description="Suggested fixes user can try")


class ConnectionErrorResponse(BaseModel):
    """Error response with structured guidance"""
    success: bool = False
    error: ConnectionErrorDetail
