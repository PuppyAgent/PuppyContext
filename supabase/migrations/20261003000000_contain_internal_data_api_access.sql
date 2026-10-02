-- ISSUE-053: backend-only access to the six known exposed relations and feed.
-- Forward-only security repair; no data rewrite, rename, or compatibility removal.
-- Default privileges and the full application catalog remain ISSUE-054's scope.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '2min';

REVOKE ALL ON TABLE public.audit_logs, public.bookmarks, public.connector_runs,
    public.scope_sync_events, public.scope_sync_settings, public.tables,
    public.version_activity_feed FROM PUBLIC, anon, authenticated, service_role;

-- Table-level REVOKE does not remove separate column grants. Cover upgraded
-- installations too, without changing any column or accepting a missing table.
DO $$
DECLARE
    item record;
BEGIN
    FOR item IN
        SELECT c.oid::regclass AS relation, string_agg(quote_ident(a.attname), ', ') AS columns
        FROM pg_class c
        JOIN pg_attribute a ON a.attrelid = c.oid
        WHERE c.oid = ANY (ARRAY[
            'public.audit_logs'::regclass, 'public.bookmarks'::regclass,
            'public.connector_runs'::regclass, 'public.scope_sync_events'::regclass,
            'public.scope_sync_settings'::regclass, 'public.tables'::regclass,
            'public.version_activity_feed'::regclass
        ]) AND a.attnum > 0 AND NOT a.attisdropped
        GROUP BY c.oid
    LOOP
        EXECUTE format(
            'REVOKE ALL (%s) ON TABLE %s FROM PUBLIC, anon, authenticated, service_role',
            item.columns, item.relation
        );
    END LOOP;
END $$;

ALTER TABLE public.audit_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.bookmarks ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.connector_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.scope_sync_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.scope_sync_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tables ENABLE ROW LEVEL SECURITY;
ALTER VIEW public.version_activity_feed SET (security_invoker = true);

-- The backend authenticates/authorizes before using service_role (BYPASSRLS).
-- audit_repository/audit_backend/history_repository and scope_sync/events are
-- append/read paths. settings_store upserts; content/table/supabase_repo is CRUD.
GRANT SELECT, INSERT ON public.audit_logs, public.scope_sync_events TO service_role;
GRANT SELECT, INSERT, UPDATE ON public.scope_sync_settings TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.tables TO service_role;
GRANT SELECT ON public.version_activity_feed TO service_role;
-- No active application writer remains for these legacy relations. Retain only
-- read access for inventory; owner-run lifecycle RPCs retain their deletion path.
GRANT SELECT ON public.bookmarks, public.connector_runs TO service_role;

REVOKE ALL ON SEQUENCE public.audit_logs_id_seq, public.scope_sync_events_id_seq
    FROM PUBLIC, anon, authenticated, service_role;
GRANT USAGE ON SEQUENCE public.audit_logs_id_seq, public.scope_sync_events_id_seq
    TO service_role;

-- Reviewed direct writers/readers, their wrappers, and owner-executed lifecycle
-- deletion paths. Never accept client-supplied actor/project arguments as auth.
-- Preserve bodies, signatures, default arguments and the existing RPC contracts.
DO $$
DECLARE
    name text;
    signature regprocedure;
BEGIN
    FOREACH name IN ARRAY ARRAY[
        'add_project_member_authorized', 'remove_project_member_authorized',
        'update_project_member_role_authorized', 'join_project_via_share_token',
        'issue_user_git_http_credential', 'issue_user_git_http_credential_idempotent',
        'revoke_user_git_http_credential',
        'publish_version_project_update', 'publish_mut_project_update',
        'publish_version_project_update_with_usage', 'repository_target_integrity_report',
        '_project_initialization_has_cascade_dependents', 'delete_project_control_plane',
        'abandon_project_initialization'
    ] LOOP
        IF NOT EXISTS (
            SELECT FROM pg_proc WHERE pronamespace = 'public'::regnamespace AND proname = name
        ) THEN
            RAISE EXCEPTION 'ISSUE-053: expected backend function % is missing', name;
        END IF;
        FOR signature IN
            SELECT oid::regprocedure FROM pg_proc
            WHERE pronamespace = 'public'::regnamespace AND proname = name
        LOOP
            EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC, anon, authenticated', signature);
            EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO service_role', signature);
        END LOOP;
    END LOOP;
END $$;

NOTIFY pgrst, 'reload schema';
COMMIT;
