"""The SQL transaction is the authority for receipts, leases and event order."""

from fastapi import HTTPException
from postgrest.exceptions import APIError

from src.exceptions import DatabaseSchemaOutdatedException
from src.infra.supabase.dependencies import get_supabase_client


class RunQueries:
    def __init__(self, client=None):
        self.client = client if client is not None else get_supabase_client()

    def surface(self, agent_id):
        rows = (
            self.client.table("access_surfaces")
            .select("*")
            .eq("id", agent_id)
            .eq("kind", "agent")
            .limit(1)
            .execute()
            .data
        )
        return rows[0] if rows else None

    def target_agents(self, project_id, scope_id):
        query = (
            self.client.table("access_surfaces")
            .select("*")
            .eq("project_id", project_id)
            .eq("kind", "agent")
        )
        query = (
            query.is_("scope_id", "null") if scope_id is None else query.eq("scope_id", scope_id)
        )
        return query.order("created_at").execute().data

    def rpc(self, function, **values):
        try:
            return (
                self.client.rpc(
                    "agent_run_" + function, {"p_" + key: value for key, value in values.items()}
                )
                .execute()
                .data
            )
        except APIError as exc:
            if exc.code == "PGRST202":
                raise DatabaseSchemaOutdatedException() from exc
            if exc.message in {
                "agent_request_conflict",
                "agent_context_changed",
                "agent_session_busy",
                "agent_approval_unavailable",
                "agent_approval_conflict",
                "agent_session_mismatch",
                "agent_unavailable",
            }:
                raise HTTPException(409, {"code": exc.message}) from exc
            raise

    def get(self, run_id):
        rows = self.client.table("agent_runs").select("*").eq("id", run_id).limit(1).execute().data
        return rows[0] if rows else None

    def receipt(self, user, project, request):
        rows = (
            self.client.table("agent_runs")
            .select("*")
            .eq("user_id", user)
            .eq("project_id", project)
            .eq("request_id", request)
            .limit(1)
            .execute()
            .data
        )
        return rows[0] if rows else None

    def tools(self, run_id):
        return (
            self.client.table("agent_run_tools")
            .select("*")
            .eq("run_id", run_id)
            .order("updated_at")
            .limit(1000)
            .execute()
            .data
        )

    def events(self, run_id, after, through):
        return (
            self.client.table("agent_run_events")
            .select("*")
            .eq("run_id", run_id)
            .gt("sequence", after)
            .lte("sequence", through)
            .order("sequence")
            .limit(64)
            .execute()
            .data
        )

    def session_runs(self, user_id, session_id, limit, before=None):
        query = (
            self.client.table("agent_runs")
            .select("*")
            .eq("user_id", user_id)
            .eq("session_id", session_id)
        )
        if before is not None:
            query = query.lt("created_at", before.isoformat())
        return query.order("created_at", desc=True).limit(limit).execute().data

    def previous(self, run):
        rows = (
            self.client.table("agent_runs")
            .select("*")
            .eq("session_id", run["session_id"])
            .neq("id", run["id"])
            .lt("created_at", run["created_at"])
            .order("created_at", desc=True)
            .limit(1)
            .execute()
            .data
        )
        return rows[0] if rows else None

    def executions(self, run_id):
        return (
            self.client.table("agent_run_executions")
            .select("*")
            .eq("run_id", run_id)
            .eq("cleaned", False)
            .execute()
            .data
        )

    def load_run_context(self, user, project, agent=None, scope=None, request=None):
        return self.rpc(
            "context", user=user, project=project, agent=agent, scope=scope, request=request
        )

    def load_run_view(self, user, run, after=0):
        return self.rpc("view", user=user, run=run, after=after)

    def load_execution(self, run):
        return self.rpc("execution_view", run=run)

    def load_receipt_view(self, user, project, request):
        return self.rpc("receipt_view", user=user, project=project, request=request)

    def load_history_view(self, user, project, agent, limit=50):
        return self.rpc("history_view", user=user, project=project, agent=agent, limit=limit)

    def load_session_view(self, user, session, limit=50, before=None):
        return self.rpc(
            "session_view",
            user=user,
            session=session,
            limit=limit,
            before=before.isoformat() if before else None,
        )
