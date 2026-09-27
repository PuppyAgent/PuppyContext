-- ============================================================================
-- ALIGN LEGACY SCHEMA DRIFT
-- ============================================================================
-- Purpose
--   Final reconciliation pass that brings the production database into 100%
--   alignment with the canonical schema produced by replaying every migration
--   on a fresh database (= "expected").
--
--   By applying this migration, a freshly bootstrapped database, the qubits
--   branch, and production all end up with identical schemas (modulo column
--   physical ordering, which is pg_dump cosmetic only and has zero effect on
--   query/insert behavior).
--
-- Drift discovered (April 2026 archaeology)
--   1. 26 abandoned projects on prod with NULL org_id, all empty
--      (0 mut_commits, 0 audit_logs). Created during the early "Get Started"
--      onboarding window (Feb 10 - Mar 6 2026) before org_id was tightened.
--   2. 11 orphan tools attached to those projects (cascade-cleaned).
--   3. 7 NOT NULL constraint mismatches:
--        prod stricter on: context_publishes/etl_rules/mcp/tools.created_by
--        prod looser  on: etl_rules/projects/tools.org_id
--   4. 3 stale constraint names on access_points (connections_* -> syncs_*).
--   5. 10 FK constraints completely missing on prod (likely dropped via a
--      historical SQL Editor session). Re-added with their canonical names.
--
-- Safety
--   - Wrapped in a single transaction; aborts on any unexpected condition.
--   - Verifies "no-data NULL projects" precondition before deleting.
--   - Each ADD CONSTRAINT cleans up orphans according to the FK's intended
--     ON DELETE rule (SET NULL or CASCADE), so the operation is reversible
--     in spirit (orphans were always going to be cleared on the next CASCADE
--     anyway).
--
-- Idempotent
--   Re-running this migration is safe: every operation is guarded with
--   IF EXISTS / IF NOT EXISTS / DO blocks. On qubits (already canonical),
--   most steps are no-ops.
-- ============================================================================

BEGIN;

-- ============================================================================
-- 1. Pre-flight safety check
-- ============================================================================
DO $$
DECLARE
    null_proj_with_data INT;
BEGIN
    SELECT count(*) INTO null_proj_with_data
      FROM public.projects p
     WHERE p.org_id IS NULL
       AND (EXISTS (SELECT 1 FROM public.mut_commits  WHERE project_id = p.id)
         OR EXISTS (SELECT 1 FROM public.audit_logs   WHERE project_id = p.id));

    IF null_proj_with_data > 0 THEN
        RAISE EXCEPTION
            'ABORT: % NULL-org projects have mut_commits or audit_logs data. Manual review required before deletion.',
            null_proj_with_data;
    END IF;
END $$;

-- ============================================================================
-- 2. Delete abandoned NULL-org projects (cascades to dependent rows)
-- ============================================================================
DO $$
DECLARE
    deleted_projects INT;
    deleted_tools    INT;
BEGIN
    DELETE FROM public.projects WHERE org_id IS NULL;
    GET DIAGNOSTICS deleted_projects = ROW_COUNT;
    RAISE NOTICE 'Deleted % abandoned NULL-org projects (cascades cleaned dependent tools/access_points)', deleted_projects;

    DELETE FROM public.tools WHERE org_id IS NULL;
    GET DIAGNOSTICS deleted_tools = ROW_COUNT;
    IF deleted_tools > 0 THEN
        RAISE NOTICE 'Defensive cleanup: deleted % residual NULL-org tools that were not covered by cascade', deleted_tools;
    END IF;
END $$;

-- ============================================================================
-- 3. Loosen NOT NULL on created_by columns (matches migration intent)
-- ============================================================================
ALTER TABLE public.context_publishes ALTER COLUMN created_by DROP NOT NULL;
ALTER TABLE public.etl_rules         ALTER COLUMN created_by DROP NOT NULL;
ALTER TABLE public.mcp               ALTER COLUMN created_by DROP NOT NULL;
ALTER TABLE public.tools             ALTER COLUMN created_by DROP NOT NULL;

-- ============================================================================
-- 4. Tighten NOT NULL on org_id where appropriate
--    projects/tools: now safe (NULLs were just cleaned in Phase 2)
--    etl_rules:      stays nullable (the 'global_default_etl_rule' system
--                    row is intentionally org-less)
-- ============================================================================
ALTER TABLE public.projects ALTER COLUMN org_id SET NOT NULL;
ALTER TABLE public.tools    ALTER COLUMN org_id SET NOT NULL;

-- Idempotent: ensure etl_rules.org_id is nullable on every environment.
-- This effectively retroactively amends the original NOT NULL declaration
-- in the qubits_schema baseline (which doesn't match prod reality).
ALTER TABLE public.etl_rules ALTER COLUMN org_id DROP NOT NULL;

-- ============================================================================
-- 5. Rename stale constraint names on access_points
--    (Table was renamed connections -> syncs -> access_points over time;
--    PK/FK names never caught up on prod.)
-- ============================================================================
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'connections_pkey') THEN
        ALTER TABLE public.access_points RENAME CONSTRAINT connections_pkey TO syncs_pkey;
        RAISE NOTICE 'Renamed connections_pkey -> syncs_pkey';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'connections_project_id_fkey') THEN
        ALTER TABLE public.access_points RENAME CONSTRAINT connections_project_id_fkey TO syncs_project_id_fkey;
        RAISE NOTICE 'Renamed connections_project_id_fkey -> syncs_project_id_fkey';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'connections_user_id_fkey') THEN
        ALTER TABLE public.access_points RENAME CONSTRAINT connections_user_id_fkey TO syncs_user_id_fkey;
        RAISE NOTICE 'Renamed connections_user_id_fkey -> syncs_user_id_fkey';
    END IF;
END $$;

-- ============================================================================
-- 6. Add 10 missing FK constraints (with orphan cleanup per FK's ON DELETE)
-- ============================================================================

-- 6.1 access_logs.agent_id -> access_points (ON DELETE SET NULL)
DO $$
DECLARE n INT;
BEGIN
    UPDATE public.access_logs SET agent_id = NULL
     WHERE agent_id IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM public.access_points ap WHERE ap.id = access_logs.agent_id);
    GET DIAGNOSTICS n = ROW_COUNT;
    IF n > 0 THEN RAISE NOTICE 'Cleared % orphan agent_id values in access_logs', n; END IF;
END $$;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'access_logs_agent_id_fkey') THEN
        ALTER TABLE public.access_logs
          ADD CONSTRAINT access_logs_agent_id_fkey
          FOREIGN KEY (agent_id) REFERENCES public.access_points(id) ON DELETE SET NULL;
        RAISE NOTICE 'Added access_logs_agent_id_fkey';
    END IF;
END $$;

-- 6.2 agent_execution_logs.agent_id -> access_points (ON DELETE CASCADE)
DO $$
DECLARE n INT;
BEGIN
    DELETE FROM public.agent_execution_logs ael
     WHERE ael.agent_id IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM public.access_points ap WHERE ap.id = ael.agent_id);
    GET DIAGNOSTICS n = ROW_COUNT;
    IF n > 0 THEN RAISE NOTICE 'Deleted % orphan agent_execution_logs rows', n; END IF;
END $$;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'agent_execution_log_agent_id_fkey') THEN
        ALTER TABLE public.agent_execution_logs
          ADD CONSTRAINT agent_execution_log_agent_id_fkey
          FOREIGN KEY (agent_id) REFERENCES public.access_points(id) ON DELETE CASCADE;
        RAISE NOTICE 'Added agent_execution_log_agent_id_fkey';
    END IF;
END $$;

-- 6.3 agent_logs.agent_id -> access_points (ON DELETE SET NULL)
DO $$
DECLARE n INT;
BEGIN
    UPDATE public.agent_logs SET agent_id = NULL
     WHERE agent_id IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM public.access_points ap WHERE ap.id = agent_logs.agent_id);
    GET DIAGNOSTICS n = ROW_COUNT;
    IF n > 0 THEN RAISE NOTICE 'Cleared % orphan agent_id values in agent_logs', n; END IF;
END $$;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'agent_logs_agent_id_fkey') THEN
        ALTER TABLE public.agent_logs
          ADD CONSTRAINT agent_logs_agent_id_fkey
          FOREIGN KEY (agent_id) REFERENCES public.access_points(id) ON DELETE SET NULL;
        RAISE NOTICE 'Added agent_logs_agent_id_fkey';
    END IF;
END $$;

-- 6.4 access_tools.access_point_id -> access_points (ON DELETE CASCADE)
DO $$
DECLARE n INT;
BEGIN
    DELETE FROM public.access_tools at
     WHERE at.access_point_id IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM public.access_points ap WHERE ap.id = at.access_point_id);
    GET DIAGNOSTICS n = ROW_COUNT;
    IF n > 0 THEN RAISE NOTICE 'Deleted % orphan access_tools rows', n; END IF;
END $$;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'agent_tool_agent_id_fkey') THEN
        ALTER TABLE public.access_tools
          ADD CONSTRAINT agent_tool_agent_id_fkey
          FOREIGN KEY (access_point_id) REFERENCES public.access_points(id) ON DELETE CASCADE;
        RAISE NOTICE 'Added agent_tool_agent_id_fkey';
    END IF;
END $$;

-- 6.5 chat_sessions.agent_id -> access_points (ON DELETE SET NULL)
DO $$
DECLARE n INT;
BEGIN
    UPDATE public.chat_sessions SET agent_id = NULL
     WHERE agent_id IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM public.access_points ap WHERE ap.id = chat_sessions.agent_id);
    GET DIAGNOSTICS n = ROW_COUNT;
    IF n > 0 THEN RAISE NOTICE 'Cleared % orphan agent_id values in chat_sessions', n; END IF;
END $$;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chat_sessions_agent_id_fkey') THEN
        ALTER TABLE public.chat_sessions
          ADD CONSTRAINT chat_sessions_agent_id_fkey
          FOREIGN KEY (agent_id) REFERENCES public.access_points(id) ON DELETE SET NULL;
        RAISE NOTICE 'Added chat_sessions_agent_id_fkey';
    END IF;
END $$;

-- 6.6 etl_rules.org_id -> organizations (ON DELETE CASCADE)
--     NULL org_id is allowed (FK permits NULL).
DO $$
DECLARE n INT;
BEGIN
    DELETE FROM public.etl_rules er
     WHERE er.org_id IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM public.organizations o WHERE o.id = er.org_id);
    GET DIAGNOSTICS n = ROW_COUNT;
    IF n > 0 THEN RAISE NOTICE 'Deleted % orphan etl_rules rows (org_id pointing to deleted org)', n; END IF;
END $$;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'etl_rule_org_id_fkey') THEN
        ALTER TABLE public.etl_rules
          ADD CONSTRAINT etl_rule_org_id_fkey
          FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;
        RAISE NOTICE 'Added etl_rule_org_id_fkey';
    END IF;
END $$;

-- 6.7 organizations.created_by -> auth.users (NO ACTION)
--     Refuses to apply if any organization points to a non-existent user;
--     that case requires manual investigation.
DO $$
DECLARE n INT;
BEGIN
    SELECT count(*) INTO n FROM public.organizations o
     WHERE o.created_by IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM auth.users u WHERE u.id = o.created_by);
    IF n > 0 THEN
        RAISE EXCEPTION
            'ABORT: % organizations have created_by pointing to non-existent users. Manual investigation required (cannot auto-set NULL because the original FK has no ON DELETE rule).',
            n;
    END IF;
END $$;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'organizations_created_by_fkey') THEN
        ALTER TABLE public.organizations
          ADD CONSTRAINT organizations_created_by_fkey
          FOREIGN KEY (created_by) REFERENCES auth.users(id);
        RAISE NOTICE 'Added organizations_created_by_fkey';
    END IF;
END $$;

-- 6.8 projects.org_id -> organizations (ON DELETE CASCADE)
--     NULL rows already cleaned in Phase 2; just sweep up non-null orphans.
DO $$
DECLARE n INT;
BEGIN
    DELETE FROM public.projects p
     WHERE p.org_id IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM public.organizations o WHERE o.id = p.org_id);
    GET DIAGNOSTICS n = ROW_COUNT;
    IF n > 0 THEN RAISE NOTICE 'Deleted % projects with org_id pointing to deleted org', n; END IF;
END $$;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'project_org_id_fkey') THEN
        ALTER TABLE public.projects
          ADD CONSTRAINT project_org_id_fkey
          FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;
        RAISE NOTICE 'Added project_org_id_fkey';
    END IF;
END $$;

-- 6.9 tools.org_id -> organizations (ON DELETE CASCADE)
DO $$
DECLARE n INT;
BEGIN
    DELETE FROM public.tools t
     WHERE t.org_id IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM public.organizations o WHERE o.id = t.org_id);
    GET DIAGNOSTICS n = ROW_COUNT;
    IF n > 0 THEN RAISE NOTICE 'Deleted % tools with org_id pointing to deleted org', n; END IF;
END $$;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'tool_org_id_fkey') THEN
        ALTER TABLE public.tools
          ADD CONSTRAINT tool_org_id_fkey
          FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;
        RAISE NOTICE 'Added tool_org_id_fkey';
    END IF;
END $$;

-- 6.10 uploads.created_by -> auth.users (ON DELETE CASCADE)
DO $$
DECLARE n INT;
BEGIN
    DELETE FROM public.uploads u
     WHERE u.created_by IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM auth.users au WHERE au.id = u.created_by);
    GET DIAGNOSTICS n = ROW_COUNT;
    IF n > 0 THEN RAISE NOTICE 'Deleted % uploads pointing to deleted user', n; END IF;
END $$;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uploads_user_id_fkey') THEN
        ALTER TABLE public.uploads
          ADD CONSTRAINT uploads_user_id_fkey
          FOREIGN KEY (created_by) REFERENCES auth.users(id) ON DELETE CASCADE;
        RAISE NOTICE 'Added uploads_user_id_fkey';
    END IF;
END $$;

-- ============================================================================
-- 7. Final sanity check
-- ============================================================================
DO $$
DECLARE
    missing_fks INT;
    stale_names INT;
    null_org_projects INT;
    null_org_tools INT;
BEGIN
    SELECT count(*) INTO missing_fks
      FROM (VALUES
        ('access_logs_agent_id_fkey'),
        ('agent_execution_log_agent_id_fkey'),
        ('agent_logs_agent_id_fkey'),
        ('agent_tool_agent_id_fkey'),
        ('chat_sessions_agent_id_fkey'),
        ('etl_rule_org_id_fkey'),
        ('organizations_created_by_fkey'),
        ('project_org_id_fkey'),
        ('tool_org_id_fkey'),
        ('uploads_user_id_fkey')
      ) v(name)
     WHERE NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = v.name);

    SELECT count(*) INTO stale_names
      FROM pg_constraint
     WHERE conname IN ('connections_pkey', 'connections_project_id_fkey', 'connections_user_id_fkey');

    SELECT count(*) INTO null_org_projects FROM public.projects WHERE org_id IS NULL;
    SELECT count(*) INTO null_org_tools    FROM public.tools    WHERE org_id IS NULL;

    IF missing_fks > 0 OR stale_names > 0 OR null_org_projects > 0 OR null_org_tools > 0 THEN
        RAISE EXCEPTION
            'ABORT post-check: missing_fks=%, stale_names=%, null_projects=%, null_tools=%',
            missing_fks, stale_names, null_org_projects, null_org_tools;
    END IF;

    RAISE NOTICE 'Reconciliation complete: 0 missing FKs, 0 stale names, 0 NULL-org projects/tools';
END $$;

COMMIT;
