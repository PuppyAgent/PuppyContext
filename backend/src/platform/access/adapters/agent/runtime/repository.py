"""The SQL transaction is the authority for receipts, leases and event order."""

from fastapi import HTTPException
from postgrest.exceptions import APIError

from src.infra.supabase.dependencies import get_supabase_client


class RunRepository:
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
            if exc.message in {
                "agent_request_conflict",
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

    def cleaned(self, execution_id):
        self.client.table("agent_run_executions").update({"cleaned": True}).eq(
            "id", execution_id
        ).execute()

    def write(self, run, kind, payload=None, **patch):
        return self.rpc(
            "write",
            run=run["id"],
            execution=run["execution_id"],
            fence=run["fence"],
            kind=kind,
            payload=payload or {},
            patch=patch,
        )

    def tool(self, run, frame, state, result=None):
        return self.rpc(
            "tool",
            run=run["id"],
            execution=run["execution_id"],
            fence=run["fence"],
            call=frame["call_id"],
            name=frame["name"],
            input=frame["input"],
            state=state,
            result=result,
        )
