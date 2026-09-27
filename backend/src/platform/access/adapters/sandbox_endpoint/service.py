from typing import List, Optional

from src.platform.access.adapters.sandbox_endpoint.repository import SandboxEndpointRepository
from src.platform.access.adapters.sandbox_endpoint.schemas import SandboxMountItem, SandboxResourceLimits


class SandboxEndpointService:

    def __init__(self, repository: SandboxEndpointRepository = None):
        self._repo = repository or SandboxEndpointRepository()

    def get_endpoint(self, endpoint_id: str) -> Optional[dict]:
        return self._repo.get_by_id(endpoint_id)

    def get_by_access_key(self, access_key: str) -> Optional[dict]:
        return self._repo.get_by_access_key(access_key)

    def list_endpoints(self, project_id: str) -> List[dict]:
        return self._repo.list_by_project(project_id)

    def get_by_path(self, path: str) -> Optional[dict]:
        return self._repo.get_by_path(path)

    def create_endpoint(
        self,
        project_id: str,
        name: str = "Sandbox",
        path: Optional[str] = None,
        description: Optional[str] = None,
        mounts: Optional[List[SandboxMountItem]] = None,
        runtime: str = "alpine",
        timeout_seconds: int = 30,
        resource_limits: Optional[SandboxResourceLimits] = None,
    ) -> dict:
        return self._repo.create(
            project_id=project_id,
            name=name,
            path=path,
            description=description,
            mounts=[m.model_dump() for m in mounts] if mounts else [],
            runtime=runtime,
            timeout_seconds=timeout_seconds,
            resource_limits=resource_limits.model_dump() if resource_limits else None,
        )

    def update_endpoint(self, endpoint_id: str, **kwargs) -> Optional[dict]:
        mounts = kwargs.pop("mounts", None)
        resource_limits = kwargs.pop("resource_limits", None)
        if mounts is not None:
            kwargs["mounts"] = [m.model_dump() if hasattr(m, "model_dump") else m for m in mounts]
        if resource_limits is not None:
            kwargs["resource_limits"] = resource_limits.model_dump() if hasattr(resource_limits, "model_dump") else resource_limits
        return self._repo.update(endpoint_id, **kwargs)

    def delete_endpoint(self, endpoint_id: str) -> bool:
        return self._repo.delete(endpoint_id)

    def regenerate_key(self, endpoint_id: str) -> Optional[dict]:
        return self._repo.regenerate_access_key(endpoint_id)
