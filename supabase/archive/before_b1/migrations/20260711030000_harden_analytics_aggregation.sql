-- ISSUE-001: database-scoped analytics aggregation, index and RLS defence.

BEGIN;

CREATE INDEX IF NOT EXISTS idx_access_logs_project_created_at
    ON public.access_logs (project_id, created_at DESC);

DROP POLICY IF EXISTS access_logs_authenticated_project_member ON public.access_logs;
CREATE POLICY access_logs_authenticated_project_member
    ON public.access_logs FOR SELECT TO authenticated
    USING (
        EXISTS (
            SELECT 1
            FROM public.projects p
            LEFT JOIN public.org_members om
              ON om.org_id = p.org_id AND om.user_id = auth.uid()
            LEFT JOIN public.project_members pm
              ON pm.project_id = p.id AND pm.user_id = auth.uid()
            WHERE p.id = access_logs.project_id
              AND (
                  (COALESCE(p.visibility, 'org') = 'org' AND om.user_id IS NOT NULL)
                  OR om.role = 'owner'
                  OR pm.user_id IS NOT NULL
              )
        )
    );

CREATE OR REPLACE FUNCTION public.analytics_access_timeseries(
    p_project_id TEXT,
    p_start_time TIMESTAMPTZ,
    p_interval TEXT,
    p_agent_id TEXT DEFAULT NULL,
    p_node_name TEXT DEFAULT NULL
) RETURNS TABLE (bucket TIMESTAMPTZ, event_count BIGINT)
LANGUAGE sql STABLE SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
    SELECT
        CASE WHEN p_interval = 'day'
             THEN date_trunc('day', al.created_at)
             ELSE date_trunc('hour', al.created_at) END,
        COUNT(*)::BIGINT
    FROM public.access_logs al
    WHERE al.project_id::TEXT = p_project_id
      AND al.created_at >= p_start_time
      AND (p_agent_id IS NULL OR al.agent_id::TEXT = p_agent_id)
      AND (p_node_name IS NULL OR al.node_name = p_node_name)
    GROUP BY 1
    ORDER BY 1 ASC;
$$;

CREATE OR REPLACE FUNCTION public.analytics_access_summary(
    p_project_id TEXT,
    p_start_time TIMESTAMPTZ
) RETURNS TABLE (total_accesses BIGINT, unique_agents BIGINT, unique_nodes BIGINT)
LANGUAGE sql STABLE SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
    SELECT COUNT(*)::BIGINT,
           COUNT(DISTINCT al.agent_id)::BIGINT,
           COUNT(DISTINCT al.node_name)::BIGINT
    FROM public.access_logs al
    WHERE al.project_id::TEXT = p_project_id
      AND al.created_at >= p_start_time;
$$;

REVOKE ALL ON FUNCTION public.analytics_access_timeseries(
    TEXT, TIMESTAMPTZ, TEXT, TEXT, TEXT
) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.analytics_access_summary(
    TEXT, TIMESTAMPTZ
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.analytics_access_timeseries(
    TEXT, TIMESTAMPTZ, TEXT, TEXT, TEXT
) TO service_role;
GRANT EXECUTE ON FUNCTION public.analytics_access_summary(
    TEXT, TIMESTAMPTZ
) TO service_role;

NOTIFY pgrst, 'reload schema';

COMMIT;
