"""Agent access-surface repository."""

from typing import List, Optional
from datetime import datetime, timezone

from src.platform.access.adapters.agent.config.models import Agent, AgentBash, AgentTool
from src.repo.scope_service import ScopeService
from src.repo.access_credentials import AccessCredentialRepository
from src.utils.id_generator import generate_uuid_v7


AGENT_PROVIDER = "agent"
ACCESS_SURFACES_TABLE = "access_surfaces"


def _now_iso() -> str:
    """Return a real timestamp; PostgREST does not evaluate SQL expressions in JSON."""
    return datetime.now(timezone.utc).isoformat()


def _scope_to_bash(agent_id: str, config: dict) -> list[AgentBash]:
    """Derive AgentBash list from the operational view, not target identity."""
    view = config.get("bash_view")
    if not view:
        return []
    path = view.get("path_prefix", "")
    mode = view.get("max_mode", "r")
    return [AgentBash(
        id=f"{agent_id}:scope",
        agent_id=agent_id,
        path=path,
        readonly=(mode == "r"),
        created_at=datetime.now(timezone.utc),
    )]


def _row_to_tool(row: dict) -> AgentTool:
    """Map access_tools DB row to AgentTool model."""
    return AgentTool(
        id=row["id"],
        agent_id=row["access_surface_id"],
        tool_id=row["tool_id"],
        enabled=row.get("enabled", True),
        mcp_exposed=row.get("mcp_exposed", False),
        created_at=row["created_at"],
    )


def _row_to_agent(
    row: dict,
    *,
    credential: dict | None = None,
    plaintext_mcp_api_key: str | None = None,
) -> Agent:
    """Convert an access_surfaces row (kind='agent') to an Agent model."""
    config = row.get("config") or {}
    trigger = config.get("trigger") or {}
    active_key = plaintext_mcp_api_key
    return Agent(
        id=row["id"],
        project_id=row["project_id"],
        name=config.get("name") or row.get("name") or "",
        icon=config.get("icon", "✨"),
        type=config.get("type", "chat"),
        description=config.get("description"),
        is_default=config.get("is_default", False),
        # Plaintext is populated only for one-time issuance or an authenticated
        # runtime lookup. Ordinary reads expose metadata below, never the token.
        mcp_api_key=plaintext_mcp_api_key,
        mcp_enabled=bool(credential or active_key),
        mcp_key_last4=(credential or {}).get("key_last4") or (active_key[-4:] if active_key else None),
        trigger_type=trigger.get("type", "manual"),
        trigger_config=trigger.get("config"),
        task_content=config.get("task_content"),
        task_path=config.get("task_path"),
        external_config=config.get("external_config"),
        llm_model=config.get("llm_model"),
        system_prompt=config.get("system_prompt"),
        status=row.get("status") or "active",
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _merge_agent_updates(
    config: dict, trigger: dict, **kwargs,
) -> tuple[dict, dict]:
    """Merge non-None update fields into config and trigger dicts."""
    # Simple config fields (set directly if not None)
    _simple_keys = (
        "name", "icon", "type", "description", "is_default",
        "task_content", "task_path", "external_config",
    )
    for key in _simple_keys:
        val = kwargs.get(key)
        if val is not None:
            config[key] = val

    # Fields that clear to None on empty string
    for key in ("llm_model", "system_prompt"):
        val = kwargs.get(key)
        if val is not None:
            config[key] = val if val != "" else None

    # Trigger fields
    if kwargs.get("trigger_type") is not None:
        trigger["type"] = kwargs["trigger_type"]
    if kwargs.get("trigger_config") is not None:
        trigger["config"] = kwargs["trigger_config"]

    return config, trigger


class AgentRepository:
    """Agent repository over ``access_surfaces``."""

    TABLE = ACCESS_SURFACES_TABLE

    def __init__(self, supabase_client=None):
        if supabase_client is None:
            from src.infra.supabase.dependencies import get_supabase_client
            self._client = get_supabase_client()
        else:
            self._client = supabase_client
        self._credentials = AccessCredentialRepository(self._client)

    def _credential_map(self, rows: list[dict]) -> dict[str, dict]:
        # A few narrow unit tests construct the repository via ``__new__`` to
        # isolate visibility filtering. Production instances always run
        # ``__init__`` and therefore have the credential boundary.
        if not hasattr(self, "_credentials"):
            return {}
        return self._credentials.list_active_by_surface([row["id"] for row in rows])

    def _agent_from_row(
        self,
        row: dict,
        *,
        plaintext_mcp_api_key: str | None = None,
    ) -> Agent:
        credential = self._credentials.get_active_by_surface(row["id"])
        return _row_to_agent(
            row,
            credential=credential,
            plaintext_mcp_api_key=plaintext_mcp_api_key,
        )

    def _project_org_id(self, project_id: str) -> str | None:
        resp = (
            self._client.table("projects")
            .select("org_id")
            .eq("id", project_id)
            .limit(1)
            .execute()
        )
        rows = resp.data or []
        return rows[0].get("org_id") if rows else None

    def get_project_org_id(self, project_id: str) -> str | None:
        """Return the Agent Project tenant for child-resource validation."""

        return self._project_org_id(project_id)

    def _query(self):
        return (
            self._client.table(ACCESS_SURFACES_TABLE)
            .select("*")
            .eq("kind", AGENT_PROVIDER)
        )

    def _scope_for_path(
        self, project_id: str, path: str = "", *, readonly: bool = False,
    ) -> dict:
        normalized = (path or "").strip("/")
        scope_service = ScopeService()
        if not normalized:
            return {
                "target": {"kind": "project_root", "project_id": project_id},
                "id": None,
                "path": "",
                "exclude": [],
                "mode": "r" if readonly else "rw",
            }
        else:
            scope = None
            for candidate in scope_service.list_for_project(project_id):
                if (candidate.path or "") == normalized:
                    scope = candidate
                    break
            if scope is None:
                scope = scope_service.create(
                    project_id=project_id,
                    name=normalized.rsplit("/", 1)[-1] or "Agent Scope",
                    path=normalized,
                    exclude=[],
                    max_mode="r" if readonly else "rw",
                )
        return {
            "id": scope.id,
            "path": scope.path,
            "exclude": scope.exclude,
            "mode": scope.max_mode,
            "target": {
                "kind": "scope",
                "project_id": project_id,
                "scope_id": scope.id,
            },
        }

    @staticmethod
    def _resolved_view(scope: dict) -> dict:
        """Convert a target selection into the canonical resolved-view shape."""

        return {
            "target": scope["target"],
            "path_prefix": scope.get("path", ""),
            "excludes": list(scope.get("exclude") or []),
            "max_mode": scope.get("mode", "r"),
        }

    def _agent_surface_for_scope(
        self,
        *,
        project_id: str,
        scope: dict,
        name: str,
        created_by: Optional[str],
    ) -> dict:
        query = (
            self._client.table(self.TABLE)
            .select("*")
            .eq("project_id", project_id)
            .eq("kind", AGENT_PROVIDER)
        )
        query = (
            query.is_("scope_id", "null")
            if scope["id"] is None
            else query.eq("scope_id", scope["id"])
        )
        resp = query.limit(1).execute()
        rows = resp.data or []
        if rows:
            surface = rows[0]
            if surface.get("org_id") is None:
                org_id = self._project_org_id(project_id)
                if org_id is not None:
                    self._client.table(self.TABLE).update(
                        {"org_id": org_id}
                    ).eq("id", surface["id"]).execute()
                    surface["org_id"] = org_id
            return surface

        response = (
            self._client.table(self.TABLE)
            .insert({
                "id": generate_uuid_v7(),
                "org_id": self._project_org_id(project_id),
                "project_id": project_id,
                "scope_id": scope["id"],
                "kind": AGENT_PROVIDER,
                "name": name,
                "status": "active",
                "config": {
                    "name": name,
                    "repository_view": self._resolved_view(scope),
                    "bash_view": {
                        "path_prefix": scope.get("path", ""),
                        "excludes": list(scope.get("exclude") or []),
                        "max_mode": scope.get("mode", "r"),
                    },
                    "activated": False,
                },
                "created_by": created_by,
            })
            .execute()
        )
        return response.data[0]

    # ============================================
    # Agent CRUD
    # ============================================

    def get_by_id(self, agent_id: str) -> Optional[Agent]:
        response = (
            self._query()
            .eq("id", agent_id)
            .execute()
        )
        if response.data:
            return self._agent_from_row(response.data[0])
        return None

    def get_by_id_with_accesses(self, agent_id: str) -> Optional[Agent]:
        response = (
            self._query()
            .eq("id", agent_id)
            .execute()
        )
        if not response.data:
            return None
        row = response.data[0]
        agent = self._agent_from_row(row)
        agent.bash_accesses = _scope_to_bash(agent_id, row.get("config") or {})
        agent.tools = self.get_tools_by_agent_id(agent_id)
        return agent

    def get_project_id(self, agent_id: str) -> str | None:
        """Return only the parent identity needed for pre-read authorization."""
        rows = (
            self._client.table(self.TABLE)
            .select("project_id")
            .eq("id", agent_id)
            .eq("kind", AGENT_PROVIDER)
            .limit(1)
            .execute()
        ).data or []
        return str(rows[0]["project_id"]) if rows else None

    def get_by_project_id(self, project_id: str) -> List[Agent]:
        response = (
            self._query()
            .eq("project_id", project_id)
            .order("created_at", desc=True)
            .execute()
        )
        rows = response.data or []
        credentials = self._credential_map(rows)
        return [_row_to_agent(row, credential=credentials.get(row["id"])) for row in rows]

    def get_by_project_id_with_accesses(
        self, project_id: str, viewer_user_id: Optional[str] = None,
    ) -> List[Agent]:
        """Load agents with view-derived bash_accesses and tool bindings.

        Visibility filter (security: M-1):
        Agents whose config.visibility == 'private' are only returned if
        viewer_user_id matches the agent surface owner.
        Pass viewer_user_id=None for internal callers that already gated
        access; pass the JWT user id from request handlers.
        """
        response = (
            self._query()
            .eq("project_id", project_id)
            .order("created_at", desc=True)
            .execute()
        )
        rows = response.data or []

        if viewer_user_id is not None:
            rows = [
                r for r in rows
                if (r.get("config") or {}).get("visibility", "org").lower() != "private"
                or r.get("created_by") == viewer_user_id
            ]

        credentials = self._credential_map(rows)
        agents = [_row_to_agent(row, credential=credentials.get(row["id"])) for row in rows]
        if not agents:
            return agents

        agent_ids = [a.id for a in agents]

        # Derive bash_accesses from the operational view in Surface config.
        config_by_id = {row["id"]: (row.get("config") or {}) for row in rows}
        bash_by_agent: dict[str, list[AgentBash]] = {}
        for aid, cfg in config_by_id.items():
            bash_by_agent[aid] = _scope_to_bash(aid, cfg)

        all_tools = (
            self._client.table("access_tools")
            .select("*")
            .in_("access_surface_id", agent_ids)
            .order("created_at")
            .execute()
        ).data
        tools_by_agent: dict[str, list[AgentTool]] = {}
        for row in all_tools:
            cid = row["access_surface_id"]
            tools_by_agent.setdefault(cid, []).append(_row_to_tool(row))

        for agent in agents:
            agent.bash_accesses = bash_by_agent.get(agent.id, [])
            agent.tools = tools_by_agent.get(agent.id, [])

        return agents

    def get_default_agent(self, project_id: str) -> Optional[Agent]:
        response = (
            self._query()
            .eq("project_id", project_id)
            .execute()
        )
        for row in response.data:
            config = row.get("config") or {}
            if config.get("is_default"):
                return self._agent_from_row(row)
        return None

    def create(
        self,
        project_id: str,
        name: str,
        icon: str = "✨",
        type: str = "chat",
        description: Optional[str] = None,
        is_default: bool = False,
        trigger_type: Optional[str] = "manual",
        trigger_config: Optional[dict] = None,
        task_content: Optional[str] = None,
        task_path: Optional[str] = None,
        external_config: Optional[dict] = None,
        llm_model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        created_by: Optional[str] = None,
        scope_path: Optional[str] = None,
        scope_readonly: bool = False,
    ) -> Agent:
        scope = self._scope_for_path(
            project_id,
            scope_path or "",
            readonly=scope_readonly,
        )
        surface = self._agent_surface_for_scope(
            project_id=project_id,
            scope=scope,
            name=name,
            created_by=created_by,
        )
        trigger = {
            "type": trigger_type or "manual",
            "config": trigger_config,
        }

        config = {
            "name": name,
            "icon": icon,
            "type": type,
            "description": description,
            "is_default": is_default,
            "trigger": trigger,
            "task_content": task_content,
            "task_path": task_path,
            "external_config": external_config,
            "llm_model": llm_model,
            "system_prompt": system_prompt,
            "repository_view": self._resolved_view(scope),
            "bash_view": {
                "path_prefix": scope.get("path", ""),
                "excludes": list(scope.get("exclude") or []),
                "max_mode": scope.get("mode", "r"),
            },
            "activated": True,
        }

        data = {
            "name": name,
            "config": config,
            "status": "active",
            "created_by": created_by,
        }
        response = (
            self._client.table(self.TABLE)
            .update(data)
            .eq("id", surface["id"])
            .eq("kind", AGENT_PROVIDER)
            .execute()
        )
        inserted = response.data[0]
        mcp_api_key = self._credentials.issue_bearer_token(
            access_surface_id=inserted["id"],
            org_id=inserted.get("org_id"),
            project_id=inserted["project_id"],
            prefix="mcp",
            created_by=inserted.get("created_by"),
        )
        credential = self._credentials.get_active_by_surface(inserted["id"])
        return _row_to_agent(
            inserted,
            credential=credential,
            plaintext_mcp_api_key=mcp_api_key,
        )

    def update(
        self,
        agent_id: str,
        name: Optional[str] = None,
        icon: Optional[str] = None,
        type: Optional[str] = None,
        description: Optional[str] = None,
        is_default: Optional[bool] = None,
        mcp_api_key: Optional[str] = None,
        trigger_type: Optional[str] = None,
        trigger_config: Optional[dict] = None,
        task_content: Optional[str] = None,
        task_path: Optional[str] = None,
        external_config: Optional[dict] = None,
        llm_model: Optional[str] = None,
        system_prompt: Optional[str] = None,
    ) -> Optional[Agent]:
        current = self.get_by_id(agent_id)
        if not current:
            return None

        # Rebuild config JSONB by merging updates
        response = (
            self._client.table(self.TABLE)
            .select("config")
            .eq("id", agent_id)
            .eq("kind", AGENT_PROVIDER)
            .execute()
        )
        if not response.data:
            return None

        config = dict(response.data[0].get("config") or {})
        trigger = dict(config.get("trigger") or {})

        config, trigger = _merge_agent_updates(
            config, trigger,
            name=name, icon=icon, type=type, description=description,
            is_default=is_default, task_content=task_content,
            task_path=task_path, external_config=external_config,
            llm_model=llm_model, system_prompt=system_prompt,
            trigger_type=trigger_type, trigger_config=trigger_config,
        )

        update_data: dict = {
            "config": config, "updated_at": _now_iso(),
        }
        config["trigger"] = trigger
        if name is not None:
            update_data["name"] = name
        if mcp_api_key is not None:
            raise ValueError("Use regenerate_mcp_api_key for credential rotation")

        resp = (
            self._client.table(self.TABLE)
            .update(update_data)
            .eq("id", agent_id)
            .eq("kind", AGENT_PROVIDER)
            .execute()
        )
        if resp.data:
            credential = self._credentials.get_active_by_surface(agent_id)
            return _row_to_agent(resp.data[0], credential=credential)
        return None

    def regenerate_mcp_api_key(self, agent_id: str) -> Optional[str]:
        response = self._query().eq("id", agent_id).execute()
        if not response.data:
            return None
        row = response.data[0]
        return self._credentials.issue_bearer_token(
            access_surface_id=row["id"],
            org_id=row.get("org_id"),
            project_id=row["project_id"],
            prefix="mcp",
            created_by=row.get("created_by"),
            revoke_existing=True,
        )

    def delete(self, agent_id: str) -> bool:
        response = (
            self._client.table(self.TABLE)
            .delete()
            .eq("id", agent_id)
            .eq("kind", AGENT_PROVIDER)
            .execute()
        )
        return len(response.data) > 0

    def is_visible_to(self, agent_id: str, user_id: str) -> bool:
        """Apply the child Agent visibility restriction only.

        Canonical Project authorization is outside this method. Child
        visibility may narrow a ProjectGrant but can never create one.
        """
        # Pull both the row and the agent in one go to avoid N queries.
        row_resp = (
            self._client.table(self.TABLE)
            .select("id, project_id, config, created_by")
            .eq("id", agent_id)
            .eq("kind", AGENT_PROVIDER)
            .limit(1)
            .execute()
        )
        if not row_resp.data:
            return False
        row = row_resp.data[0]
        config = row.get("config") or {}
        # Agent visibility can only narrow the already-resolved ProjectGrant.
        visibility = (config.get("visibility") or "org").lower()
        if visibility == "private":
            owner = row.get("created_by")
            if owner and owner != user_id:
                return False
        return True

    # ============================================
    # AgentBash CRUD — narrows an Agent target through config.bash_view.
    # ============================================

    def _get_agent_config(self, agent_id: str) -> Optional[dict]:
        """Read raw config JSONB for an agent."""
        resp = (
            self._client.table(self.TABLE)
            .select("config, project_id, scope_id")
            .eq("id", agent_id)
            .eq("kind", AGENT_PROVIDER)
            .execute()
        )
        if resp.data:
            row = resp.data[0]
            config = dict(row.get("config") or {})
            config["_project_id"] = row.get("project_id")
            config["_target_scope_id"] = row.get("scope_id")
            return config
        return None

    def _update_bash_view(self, agent_id: str, view: dict) -> None:
        """Update an operational view without changing Surface target identity."""
        config = self._get_agent_config(agent_id)
        if config is None:
            return
        config.pop("_target_scope_id", None)
        config.pop("_project_id", None)
        target_view = config.get("repository_view")
        if not isinstance(target_view, dict):
            raise RuntimeError("Agent repository target is missing")
        target_prefix = str(target_view.get("path_prefix") or "").strip("/")
        path_prefix = str(view.get("path_prefix") or "").strip("/")
        if target_prefix and not (
            path_prefix == target_prefix or path_prefix.startswith(f"{target_prefix}/")
        ):
            raise RuntimeError("Agent Bash view cannot escape its repository target")
        target_mode = str(target_view.get("max_mode") or "r")
        max_mode = str(view.get("max_mode") or "r")
        if max_mode == "rw" and target_mode != "rw":
            raise RuntimeError("Agent Bash view cannot exceed its target mode")
        config["bash_view"] = {
            "path_prefix": path_prefix,
            "excludes": list(view.get("excludes") or []),
            "max_mode": max_mode,
        }
        self._client.table(self.TABLE).update(
            {"config": config, "updated_at": _now_iso()}
        ).eq("id", agent_id).eq("kind", AGENT_PROVIDER).execute()

    def get_bash_by_agent_id(self, agent_id: str) -> List[AgentBash]:
        config = self._get_agent_config(agent_id)
        if config is None:
            return []
        return _scope_to_bash(agent_id, config)

    def get_bash_by_id(self, bash_id: str) -> Optional[AgentBash]:
        agent_id = bash_id.split(":")[0] if ":" in bash_id else bash_id
        accesses = self.get_bash_by_agent_id(agent_id)
        for a in accesses:
            if a.id == bash_id:
                return a
        return None

    def create_bash(
        self,
        agent_id: str,
        path: str,
        readonly: bool = True,
    ) -> AgentBash:
        config = self._get_agent_config(agent_id)
        if config is None:
            raise RuntimeError(f"Agent {agent_id} not found")
        project_id = config.get("_project_id")
        if not project_id:
            raise RuntimeError(f"Agent {agent_id} is missing project_id")
        normalized = (path or "").strip("/")
        view = {
            "path_prefix": normalized,
            "excludes": [],
            "max_mode": "r" if readonly else "rw",
        }
        self._update_bash_view(agent_id, view)
        return AgentBash(
            id=f"{agent_id}:scope",
            agent_id=agent_id,
            path=normalized,
            readonly=readonly,
            created_at=datetime.now(timezone.utc),
        )

    def update_bash(
        self,
        bash_id: str,
        readonly: Optional[bool] = None,
    ) -> Optional[AgentBash]:
        agent_id = bash_id.split(":")[0] if ":" in bash_id else bash_id
        config = self._get_agent_config(agent_id)
        if config is None:
            return None
        view = dict(config.get("bash_view") or {})
        if not view:
            return None
        if readonly is not None:
            view["max_mode"] = "r" if readonly else "rw"
        self._update_bash_view(agent_id, view)
        return self.get_bash_by_id(bash_id)

    def delete_bash(self, bash_id: str) -> bool:
        agent_id = bash_id.split(":")[0] if ":" in bash_id else bash_id
        config = self._get_agent_config(agent_id)
        if config is None:
            return False
        if "bash_view" in config:
            del config["bash_view"]
            config.pop("_project_id", None)
            config.pop("_target_scope_id", None)
            self._client.table(self.TABLE).update(
                {"config": config, "updated_at": _now_iso()}
            ).eq("id", agent_id).execute()
        return True

    def delete_bash_by_agent_id(self, agent_id: str) -> int:
        if self.delete_bash(f"{agent_id}:scope"):
            return 1
        return 0

    def upsert_bash(
        self,
        agent_id: str,
        path: str,
        readonly: bool = True,
    ) -> AgentBash:
        return self.create_bash(agent_id, path, readonly)

    # ============================================
    # AgentTool CRUD
    # ============================================

    def get_tools_by_agent_id(self, agent_id: str) -> List[AgentTool]:
        response = (
            self._client.table("access_tools")
            .select("*")
            .eq("access_surface_id", agent_id)
            .order("created_at")
            .execute()
        )
        return [_row_to_tool(row) for row in response.data]

    def list_access_surface_ids_by_tool(self, tool_id: str) -> list[str]:
        response = (
            self._client.table("access_tools")
            .select("access_surface_id")
            .eq("tool_id", tool_id)
            .execute()
        )
        return list(dict.fromkeys(
            row["access_surface_id"]
            for row in (response.data or [])
            if row.get("access_surface_id")
        ))

    def get_tools_by_agent_id_for_mcp(self, agent_id: str) -> List[AgentTool]:
        response = (
            self._client.table("access_tools")
            .select("*")
            .eq("access_surface_id", agent_id)
            .eq("enabled", True)
            .eq("mcp_exposed", True)
            .order("created_at")
            .execute()
        )
        return [_row_to_tool(row) for row in response.data]

    def get_tool_binding_by_id(self, binding_id: str) -> Optional[AgentTool]:
        response = (
            self._client.table("access_tools")
            .select("*")
            .eq("id", binding_id)
            .execute()
        )
        if response.data:
            return _row_to_tool(response.data[0])
        return None

    def create_tool_binding(
        self,
        agent_id: str,
        tool_id: str,
        enabled: bool = True,
        mcp_exposed: bool = False,
    ) -> AgentTool:
        binding_id = generate_uuid_v7()
        data = {
            "id": binding_id,
            "access_surface_id": agent_id,
            "tool_id": tool_id,
            "enabled": enabled,
            "mcp_exposed": mcp_exposed,
        }
        response = self._client.table("access_tools").insert(data).execute()
        return _row_to_tool(response.data[0])

    def update_tool_binding(
        self,
        binding_id: str,
        enabled: Optional[bool] = None,
        mcp_exposed: Optional[bool] = None,
    ) -> Optional[AgentTool]:
        data = {}
        if enabled is not None:
            data["enabled"] = enabled
        if mcp_exposed is not None:
            data["mcp_exposed"] = mcp_exposed
        if not data:
            return self.get_tool_binding_by_id(binding_id)

        response = (
            self._client.table("access_tools")
            .update(data)
            .eq("id", binding_id)
            .execute()
        )
        if response.data:
            return _row_to_tool(response.data[0])
        return None

    def delete_tool_binding(self, binding_id: str) -> bool:
        response = (
            self._client.table("access_tools")
            .delete()
            .eq("id", binding_id)
            .execute()
        )
        return len(response.data) > 0

    def delete_tools_by_agent_id(self, agent_id: str) -> int:
        response = (
            self._client.table("access_tools")
            .delete()
            .eq("access_surface_id", agent_id)
            .execute()
        )
        return len(response.data)

    def get_tool_binding_by_agent_and_tool(
        self, agent_id: str, tool_id: str
    ) -> Optional[AgentTool]:
        response = (
            self._client.table("access_tools")
            .select("*")
            .eq("access_surface_id", agent_id)
            .eq("tool_id", tool_id)
            .execute()
        )
        if response.data:
            return _row_to_tool(response.data[0])
        return None

    def upsert_tool_binding(
        self,
        agent_id: str,
        tool_id: str,
        enabled: bool = True,
        mcp_exposed: bool = False,
    ) -> AgentTool:
        binding_id = generate_uuid_v7()
        data = {
            "id": binding_id,
            "access_surface_id": agent_id,
            "tool_id": tool_id,
            "enabled": enabled,
            "mcp_exposed": mcp_exposed,
        }
        response = (
            self._client.table("access_tools")
            .upsert(data, on_conflict="access_surface_id,tool_id")
            .execute()
        )
        return _row_to_tool(response.data[0])

    # ============================================
    # Execution History
    # ============================================

    def get_execution_history(self, agent_id: str, user_id: str, limit: int = 10) -> list[dict]:
        response = (
            self._client.table("agent_runs")
            .select("*")
            .eq("agent_id", agent_id)
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        from src.platform.access.adapters.agent.runtime.models import public_run
        return [public_run(row) for row in response.data or []]
