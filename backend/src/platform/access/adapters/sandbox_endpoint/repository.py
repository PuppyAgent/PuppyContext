"""Sandbox endpoint repository over access_surfaces + repository_scopes."""

from src.repo.access_credentials import AccessCredentialRepository
from src.repo.scope_service import ScopeService
from src.utils.id_generator import generate_uuid_v7

PROVIDER = "sandbox"
ACCESS_SURFACES_TABLE = "access_surfaces"
SCOPES_TABLE = "repository_scopes"


def _row_to_endpoint(
    row: dict,
    scope_path: str | None = None,
    *,
    credential: dict | None = None,
    plaintext_access_key: str | None = None,
) -> dict:
    """Reshape an access_surfaces row into the sandbox endpoint API dict."""
    config = row.get("config") or {}
    return {
        "id": row["id"],
        "org_id": row.get("org_id"),
        "project_id": row["project_id"],
        "path": scope_path,
        "name": row.get("name") or config.get("name", "Sandbox"),
        "description": config.get("description"),
        "access_key": plaintext_access_key,
        "has_key": bool(credential or plaintext_access_key),
        "key_last4": (credential or {}).get("key_last4")
        or ((plaintext_access_key or "")[-4:] or None),
        "mounts": config.get("mounts", []),
        "runtime": config.get("runtime", "alpine"),
        "timeout_seconds": config.get("timeout_seconds", 30),
        "resource_limits": config.get("resource_limits", {"memory_mb": 128, "cpu_shares": 0.5}),
        "status": row.get("status", "active"),
        "created_at": row.get("created_at", ""),
        "updated_at": row.get("updated_at", ""),
    }


class SandboxEndpointRepository:
    TABLE = ACCESS_SURFACES_TABLE

    def __init__(self, supabase_client=None):
        if supabase_client is None:
            from src.infra.supabase.dependencies import get_supabase_client

            self._client = get_supabase_client()
        else:
            self._client = supabase_client
        self._credentials = AccessCredentialRepository(self._client)

    def _project_org_id(self, project_id: str) -> str | None:
        resp = (
            self._client.table("projects").select("org_id").eq("id", project_id).limit(1).execute()
        )
        rows = resp.data or []
        return rows[0].get("org_id") if rows else None

    def _query(self):
        return self._client.table(ACCESS_SURFACES_TABLE).select("*").eq("kind", PROVIDER)

    def _scope_path_lookup(self, scope_ids: list[str | None]) -> dict[str, str | None]:
        unique = list({sid for sid in scope_ids if sid})
        if not unique:
            return {}
        resp = self._client.table(SCOPES_TABLE).select("id, path").in_("id", unique).execute()
        return {s["id"]: s.get("path") for s in (resp.data or [])}

    def _hydrate(self, rows: list[dict]) -> list[dict]:
        if not rows:
            return []
        path_by_scope = self._scope_path_lookup([r.get("scope_id") for r in rows])
        credentials = self._credentials.list_active_by_surface([row["id"] for row in rows])
        return [
            _row_to_endpoint(
                row,
                path_by_scope.get(row.get("scope_id")),
                credential=credentials.get(row["id"]),
            )
            for row in rows
        ]

    def _scope_for_path(self, project_id: str, path: str | None) -> dict:
        normalized = (path or "").strip("/")
        scope_svc = ScopeService()
        for scope in scope_svc.list_for_project(project_id):
            if (scope.path or "") == normalized:
                return {"id": scope.id, "path": scope.path}
        scope = scope_svc.create(
            project_id=project_id,
            name=normalized.rsplit("/", 1)[-1] if normalized else "Root",
            path=normalized,
            exclude=[],
            mode="rw",
        )
        return {"id": scope.id, "path": scope.path}

    def get_by_id(self, endpoint_id: str) -> dict | None:
        resp = self._query().eq("id", endpoint_id).execute()
        rows = self._hydrate(resp.data or [])
        return rows[0] if rows else None

    def get_by_access_key(self, access_key: str) -> dict | None:
        credential = self._credentials.get_active_by_token(access_key)
        if credential:
            resp = self._query().eq("id", credential["access_surface_id"]).execute()
            if resp.data:
                row = resp.data[0]
                if row.get("status") != "active":
                    return None
                path_by_scope = self._scope_path_lookup([row.get("scope_id")])
                return _row_to_endpoint(
                    row,
                    path_by_scope.get(row.get("scope_id")),
                    credential=credential,
                    plaintext_access_key=access_key,
                )

        return None

    def list_by_project(self, project_id: str) -> list[dict]:
        resp = self._query().eq("project_id", project_id).order("created_at", desc=True).execute()
        return self._hydrate(resp.data or [])

    def get_by_path(self, path: str) -> dict | None:
        # path lives on the scope, not the connector. Resolve scope first,
        # then fetch the connector attached to it.
        normalized = (path or "").strip("/")
        scope_resp = self._client.table(SCOPES_TABLE).select("id").eq("path", normalized).execute()
        scope_ids = [s["id"] for s in (scope_resp.data or [])]
        if not scope_ids:
            return None
        resp = self._query().in_("scope_id", scope_ids).execute()
        rows = self._hydrate(resp.data or [])
        return rows[0] if rows else None

    def create(
        self,
        project_id: str,
        name: str,
        path: str | None = None,
        description: str | None = None,
        mounts: list | None = None,
        runtime: str = "alpine",
        timeout_seconds: int = 30,
        resource_limits: dict | None = None,
    ) -> dict:
        config = {
            "name": name,
            "description": description,
            "mounts": mounts or [],
            "runtime": runtime,
            "timeout_seconds": timeout_seconds,
            "resource_limits": resource_limits or {"memory_mb": 128, "cpu_shares": 0.5},
        }
        scope = self._scope_for_path(project_id, path)
        row = {
            "id": generate_uuid_v7(),
            "org_id": self._project_org_id(project_id),
            "project_id": project_id,
            "scope_id": scope["id"],
            "kind": PROVIDER,
            "name": name,
            "config": config,
            "status": "active",
        }
        resp = self._client.table(self.TABLE).insert(row).execute()
        inserted = resp.data[0]
        access_key = self._credentials.issue_bearer_token(
            access_surface_id=inserted["id"],
            org_id=inserted.get("org_id"),
            project_id=inserted["project_id"],
            prefix="sbx",
            created_by=inserted.get("created_by"),
        )
        credential = self._credentials.get_active_by_surface(inserted["id"])
        return _row_to_endpoint(
            inserted,
            scope["path"],
            credential=credential,
            plaintext_access_key=access_key,
        )

    def update(self, endpoint_id: str, **kwargs) -> dict | None:
        current = self._query().eq("id", endpoint_id).execute()
        if not current.data:
            return None

        row = current.data[0]
        config = dict(row.get("config") or {})
        update_data = {}

        config_keys = (
            "name",
            "description",
            "mounts",
            "runtime",
            "timeout_seconds",
            "resource_limits",
        )
        for key in config_keys:
            if key in kwargs and kwargs[key] is not None:
                config[key] = kwargs[key]

        config.pop("sandbox_provider", None)
        update_data["config"] = config

        if "path" in kwargs:
            scope = self._scope_for_path(row["project_id"], kwargs["path"])
            update_data["scope_id"] = scope["id"]
        if "access_key" in kwargs:
            raise ValueError("Use regenerate_access_key for credential rotation")
        if "status" in kwargs:
            update_data["status"] = kwargs["status"]

        resp = self._client.table(self.TABLE).update(update_data).eq("id", endpoint_id).execute()
        if not resp.data:
            return None
        return self._hydrate(resp.data)[0]

    def delete(self, endpoint_id: str) -> bool:
        resp = self._client.table(self.TABLE).delete().eq("id", endpoint_id).execute()
        return bool(resp.data)

    def regenerate_access_key(self, endpoint_id: str) -> dict | None:
        current = self._query().eq("id", endpoint_id).execute()
        if not current.data:
            return None
        row = current.data[0]
        new_key = self._credentials.issue_bearer_token(
            access_surface_id=row["id"],
            org_id=row.get("org_id"),
            project_id=row["project_id"],
            prefix="sbx",
            created_by=row.get("created_by"),
            revoke_existing=True,
        )
        credential = self._credentials.get_active_by_surface(row["id"])
        path_by_scope = self._scope_path_lookup([row.get("scope_id")])
        return _row_to_endpoint(
            row,
            path_by_scope.get(row.get("scope_id")),
            credential=credential,
            plaintext_access_key=new_key,
        )
