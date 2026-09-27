from typing import Optional, List, Literal
from pydantic import BaseModel, Field


class SandboxMountPermissions(BaseModel):
    read: bool = True
    write: bool = False
    exec: bool = False


class SandboxMountItem(BaseModel):
    path: str
    mount_path: str = "/workspace"
    permissions: SandboxMountPermissions = Field(default_factory=SandboxMountPermissions)


class SandboxResourceLimits(BaseModel):
    memory_mb: int = 128
    cpu_shares: float = 0.5


class SandboxEndpointCreate(BaseModel):
    project_id: str = Field(..., description="Associated project ID")
    path: Optional[str] = Field(None, description="Associated version path")
    name: str = Field(default="Sandbox", min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=500)
    mounts: List[SandboxMountItem] = Field(default_factory=list)
    runtime: Literal["alpine", "python", "node"] = "alpine"
    timeout_seconds: int = Field(default=30, ge=5, le=300)
    resource_limits: SandboxResourceLimits = Field(default_factory=SandboxResourceLimits)


class SandboxEndpointUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = None
    path: Optional[str] = None
    status: Optional[str] = None
    mounts: Optional[List[SandboxMountItem]] = None
    runtime: Optional[Literal["alpine", "python", "node"]] = None
    timeout_seconds: Optional[int] = Field(None, ge=5, le=300)
    resource_limits: Optional[SandboxResourceLimits] = None


class SandboxEndpointOut(BaseModel):
    id: str
    project_id: str
    path: Optional[str] = None
    name: str
    description: Optional[str] = None
    access_key: Optional[str] = None
    has_key: bool = False
    key_last4: Optional[str] = None
    mounts: list = Field(default_factory=list)
    runtime: str
    timeout_seconds: int
    resource_limits: dict = Field(default_factory=dict)
    status: str
    created_at: str
    updated_at: str
