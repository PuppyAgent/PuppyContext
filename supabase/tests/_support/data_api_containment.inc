-- ISSUE-053 scoped, read-only post-deploy check. No credentials or row contents.
-- This is not the full application exposure catalog (ISSUE-054).
BEGIN READ ONLY;
SET LOCAL statement_timeout = '30s';
DO $$
DECLARE
    object record;
    client text;
    role_oid oid;
    signature regprocedure;
    protected_relations oid[] := ARRAY[
        'public.audit_logs'::regclass, 'public.bookmarks'::regclass,
        'public.connector_runs'::regclass, 'public.scope_sync_events'::regclass,
        'public.scope_sync_settings'::regclass, 'public.tables'::regclass,
        'public.version_activity_feed'::regclass
    ];
BEGIN
    FOREACH client IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        SELECT oid INTO STRICT role_oid FROM pg_roles WHERE rolname = client;
        IF EXISTS (SELECT FROM pg_roles WHERE oid = role_oid AND (rolsuper OR rolbypassrls))
           OR pg_has_role(client, 'service_role', 'MEMBER')
           OR pg_has_role(client, 'postgres', 'MEMBER') THEN
            RAISE EXCEPTION 'ISSUE-053: privileged client role %', client;
        END IF;
        FOR object IN
            -- Include views that could otherwise execute as an owner over a
            -- protected relation. pg_rewrite captures view dependencies.
            WITH RECURSIVE relations(oid) AS (
                SELECT unnest(protected_relations)
                UNION
                SELECT r.ev_class FROM relations base
                JOIN pg_depend d ON d.refclassid = 'pg_class'::regclass AND d.refobjid = base.oid
                JOIN pg_rewrite r ON d.classid = 'pg_rewrite'::regclass AND r.oid = d.objid
            )
            SELECT c.* FROM relations JOIN pg_class c USING (oid)
        LOOP
            IF pg_has_role(client, object.relowner, 'MEMBER')
               OR has_table_privilege(client, object.oid, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER,MAINTAIN')
               OR EXISTS (SELECT FROM pg_attribute a WHERE a.attrelid = object.oid
                   AND a.attnum > 0 AND NOT a.attisdropped
                   AND has_column_privilege(client, object.oid, a.attnum, 'SELECT,INSERT,UPDATE,REFERENCES')) THEN
                RAISE EXCEPTION 'ISSUE-053: client % can access %', client, object.oid::regclass;
            END IF;
            IF object.relkind IN ('r', 'p') AND NOT object.relrowsecurity THEN
                RAISE EXCEPTION 'ISSUE-053: RLS missing on %', object.oid::regclass;
            END IF;
            IF EXISTS (SELECT FROM pg_policy p WHERE p.polrelid = object.oid
                AND (0 = ANY(p.polroles) OR EXISTS (
                    SELECT FROM unnest(p.polroles) r WHERE r <> 0 AND pg_has_role(role_oid, r, 'MEMBER')
                ))) THEN
                RAISE EXCEPTION 'ISSUE-053: client policy on %', object.oid::regclass;
            END IF;
        END LOOP;
        FOR object IN
            SELECT c.oid FROM pg_class c JOIN pg_depend d ON d.objid = c.oid
            WHERE c.relkind = 'S' AND d.classid = 'pg_class'::regclass
              AND d.refclassid = 'pg_class'::regclass AND d.refobjid = ANY(protected_relations)
              AND d.deptype IN ('a', 'i')
        LOOP
            IF has_sequence_privilege(client, object.oid, 'USAGE,SELECT,UPDATE') THEN
                RAISE EXCEPTION 'ISSUE-053: client sequence access %', object.oid::regclass;
            END IF;
        END LOOP;
    END LOOP;
    IF NOT (SELECT coalesce(reloptions, '{}') @> ARRAY['security_invoker=true']
        FROM pg_class WHERE oid = 'public.version_activity_feed'::regclass) THEN
        RAISE EXCEPTION 'ISSUE-053: activity feed must use invoker permissions';
    END IF;

    -- Seed reviewed dynamic/cascade paths explicitly. Supplement pg_depend
    -- (which misses PL/pgSQL/string bodies) with a conservative body call scan.
    -- A new reachable wrapper cannot silently retain PUBLIC EXECUTE.
    FOR object IN
        WITH RECURSIVE functions AS (
            SELECT p.*, replace(p.prosrc, '"', '') AS body FROM pg_proc p
            WHERE p.pronamespace = 'public'::regnamespace AND p.prokind = 'f'
        ), reachable(oid) AS (
            SELECT oid FROM functions WHERE proname = ANY(ARRAY[
                'add_project_member_authorized', 'remove_project_member_authorized',
                'update_project_member_role_authorized', 'join_project_via_share_token',
                'issue_user_git_http_credential', 'issue_user_git_http_credential_idempotent',
                'revoke_user_git_http_credential', 'publish_version_project_update',
                'publish_mut_project_update', 'publish_version_project_update_with_usage',
                'repository_target_integrity_report', '_project_initialization_has_cascade_dependents',
                'delete_project_control_plane', 'abandon_project_initialization'
            ]) OR body ~ '\m(audit_logs|bookmarks|connector_runs|scope_sync_events|scope_sync_settings|version_activity_feed)\M|\mpublic\.tables\M'
            UNION
            SELECT caller.oid FROM reachable r JOIN functions callee ON callee.oid = r.oid
            JOIN functions caller ON caller.body ~ ('\m' || callee.proname || '\s*\(')
                OR EXISTS (SELECT FROM pg_depend d WHERE d.classid = 'pg_proc'::regclass
                    AND d.objid = caller.oid AND d.refclassid = 'pg_proc'::regclass AND d.refobjid = callee.oid)
        )
        SELECT f.* FROM reachable JOIN functions f USING (oid)
    LOOP
        signature := object.oid::regprocedure;
        IF has_function_privilege('anon', object.oid, 'EXECUTE')
           OR has_function_privilege('authenticated', object.oid, 'EXECUTE')
           OR EXISTS (SELECT FROM aclexplode(coalesce(object.proacl, acldefault('f', object.proowner)))
                      WHERE grantee = 0 AND privilege_type = 'EXECUTE') THEN
            RAISE EXCEPTION 'ISSUE-053: client can invoke protected function %', signature;
        END IF;
        IF object.proname = 'issue_user_git_http_credential'
           AND has_function_privilege('service_role', object.oid, 'EXECUTE') THEN
            RAISE EXCEPTION 'ISSUE-053: backend can bypass idempotent credential wrapper';
        END IF;
        IF object.prosecdef AND NOT (
            coalesce(object.proconfig, '{}') @> ARRAY['search_path=pg_catalog, public, pg_temp']
        ) THEN
            RAISE EXCEPTION 'ISSUE-053: unsafe definer search_path on %', signature;
        END IF;
    END LOOP;
    RAISE NOTICE 'ISSUE-053: protected relations, columns, sequences, views, RPCs and client roles are confined';
END $$;
ROLLBACK;
