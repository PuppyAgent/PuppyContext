"""Human control-plane diagnostics of the canonical native repository."""

from src.version_engine.entrypoints.git.native import NativeGitEndpoint


class ProjectGitViewService:
    def __init__(self, repo_manager):
        self._repo_manager = repo_manager

    def _endpoint(self, project_id, grant):
        service = self._repo_manager.get_native_service(project_id)
        if service is None:
            raise RuntimeError("repository migration required")
        return NativeGitEndpoint(service, grant, self._repo_manager.get_audit(project_id))

    def health(self, project_id, *, grant, content_write_allowed, cache_rebuild_allowed):
        result = self._endpoint(project_id, grant).health()
        result["can_rebuild"] = cache_rebuild_allowed
        return result

    def rebuild(self, project_id, *, grant):
        # Health verifies durable objects; no local bare repository is required.
        return self._endpoint(project_id, grant).rebuild()
