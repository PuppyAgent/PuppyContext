\ir _support/data_api_containment.inc
BEGIN;
SELECT no_plan();
\ir _support/data_api_containment_fixture.inc

SELECT is((SELECT count(*) FROM public.version_activity_feed WHERE project_id LIKE 'issue053-project-%'),
    2::bigint, 'nonempty activity feed includes both tenants');
SELECT is((SELECT count(*) FROM public.version_activity_feed WHERE project_id LIKE 'issue053-project-%'
    AND transaction_id IS NOT NULL AND pending_conflict_id IS NOT NULL),
    2::bigint, 'fixture exercises joined transaction and conflict information');

-- Each SQL denial must be 42501, not an empty relation, missing column, bad
-- request or a service-role client accidentally used for a negative test.
CREATE FUNCTION pg_temp.expect_client_denials(client text) RETURNS SETOF text
LANGUAGE plpgsql AS $$
DECLARE
    relation text;
    column_name text;
    operation text;
    seq text;
    fn record;
BEGIN
    EXECUTE format('SET LOCAL ROLE %I', client);
    FOREACH relation IN ARRAY ARRAY[
        'audit_logs', 'bookmarks', 'connector_runs', 'scope_sync_events', 'scope_sync_settings', 'tables'
    ] LOOP
        column_name := CASE relation WHEN 'connector_runs' THEN 'connector_id' ELSE 'project_id' END;
        RETURN NEXT throws_ok(format('SELECT * FROM public.%I', relation), '42501',
            format('permission denied for table %s', relation), client || ' cannot read ' || relation);
        RETURN NEXT throws_ok(format('INSERT INTO public.%I DEFAULT VALUES', relation), '42501',
            format('permission denied for table %s', relation), client || ' cannot insert ' || relation);
        RETURN NEXT throws_ok(format('UPDATE public.%I SET %I = %I', relation, column_name, column_name), '42501',
            format('permission denied for table %s', relation), client || ' cannot update ' || relation);
        RETURN NEXT throws_ok(format('DELETE FROM public.%I', relation), '42501',
            format('permission denied for table %s', relation), client || ' cannot delete ' || relation);
        RETURN NEXT ok(NOT has_table_privilege(client, 'public.' || relation, 'TRUNCATE,REFERENCES,TRIGGER,MAINTAIN'),
            client || ' has no whole-table or DDL privileges on ' || relation);
    END LOOP;
    RETURN NEXT throws_ok('SELECT * FROM public.version_activity_feed', '42501',
        'permission denied for view version_activity_feed', client || ' cannot read joined feed');
    FOREACH seq IN ARRAY ARRAY['audit_logs_id_seq', 'scope_sync_events_id_seq'] LOOP
        RETURN NEXT throws_ok(format('SELECT nextval(%L)', 'public.' || seq), '42501',
            'permission denied for sequence ' || seq, client || ' cannot advance ' || seq);
        RETURN NEXT throws_ok(format('SELECT setval(%L, 900)', 'public.' || seq), '42501',
            'permission denied for sequence ' || seq, client || ' cannot reset ' || seq);
        RETURN NEXT ok(NOT has_sequence_privilege(client, 'public.' || seq, 'SELECT'), client || ' cannot inspect ' || seq);
    END LOOP;
    FOR fn IN
        SELECT p.proname, string_agg('NULL::' || t.typ::regtype, ', ' ORDER BY t.position) AS arguments
        FROM pg_proc p
        CROSS JOIN LATERAL unnest(p.proargtypes) WITH ORDINALITY t(typ, position)
        WHERE p.pronamespace = 'public'::regnamespace AND p.proname = ANY(ARRAY[
            'add_project_member_authorized', 'remove_project_member_authorized',
            'update_project_member_role_authorized', 'join_project_via_share_token',
            'issue_user_git_http_credential', 'issue_user_git_http_credential_idempotent',
            'revoke_user_git_http_credential', 'publish_version_project_update',
            'publish_mut_project_update', 'publish_version_project_update_with_usage',
            '_project_initialization_has_cascade_dependents', 'delete_project_control_plane',
            'abandon_project_initialization'
        ]) GROUP BY p.oid, p.proname
    LOOP
        RETURN NEXT throws_ok(format('SELECT public.%I(%s)', fn.proname, fn.arguments), '42501',
            'permission denied for function ' || fn.proname, client || ' cannot call ' || fn.proname);
    END LOOP;
    RETURN NEXT throws_ok('SELECT public.repository_target_integrity_report()', '42501',
        'permission denied for function repository_target_integrity_report', client || ' cannot call diagnostics');
    RESET ROLE;
END $$;
SELECT * FROM pg_temp.expect_client_denials('anon');
SELECT * FROM pg_temp.expect_client_denials('authenticated');

-- A mistaken future SELECT grant still cannot turn the view into an owner/RLS
-- bypass. Roll back these intentional negative mutations with the fixture.
GRANT SELECT ON public.version_activity_feed TO authenticated;
SET LOCAL ROLE authenticated;
SELECT throws_ok('SELECT * FROM public.version_activity_feed', '42501',
    'permission denied for table audit_logs', 'invoker view cannot borrow owner access');
RESET ROLE;
GRANT SELECT, INSERT ON public.tables TO anon, authenticated;
SET LOCAL ROLE anon;
SELECT is((SELECT count(*) FROM public.tables), 0::bigint, 'RLS denies anon even after accidental table grant');
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claims', '{"sub":"00000000-0000-4000-8000-000000000053","role":"authenticated"}', true);
SELECT is((SELECT count(*) FROM public.tables), 0::bigint, 'RLS denies user even on their own tenant');
SELECT throws_ok($sql$INSERT INTO public.tables(id,project_id) VALUES ('rls-probe','issue053-project-a')$sql$,
    '42501', 'new row violates row-level security policy for table "tables"', 'RLS denies client insert');
RESET ROLE;

SET LOCAL ROLE service_role;
SELECT is((SELECT count(*) FROM public.version_activity_feed WHERE project_id LIKE 'issue053-project-%'),
    2::bigint, 'backend can still read both tenant records after application authorization');
SELECT lives_ok($sql$INSERT INTO public.audit_logs(project_id,action) VALUES ('issue053-project-a','backend-probe')$sql$,
    'backend appends audit with sequence USAGE');
SELECT lives_ok($sql$INSERT INTO public.scope_sync_events(project_id,scope_id,head_version)
    VALUES ('issue053-project-a','issue053-scope-a','backend-head')$sql$, 'backend appends scope event');
SELECT lives_ok($sql$INSERT INTO public.scope_sync_settings(project_id,scope_id,auto_sync)
    VALUES ('issue053-project-a','issue053-scope-a',false)
    ON CONFLICT (project_id,scope_id) DO UPDATE SET auto_sync=EXCLUDED.auto_sync$sql$, 'backend settings upsert');
SELECT lives_ok($sql$INSERT INTO public.tables(id,project_id,data) VALUES ('backend-table','issue053-project-a','{}');
    UPDATE public.tables SET data='{"saved":true}' WHERE id='backend-table';
    DELETE FROM public.tables WHERE id='backend-table'$sql$, 'backend structured table CRUD');
SELECT ok(NOT has_table_privilege('service_role','public.audit_logs','UPDATE,DELETE,TRUNCATE,TRIGGER,REFERENCES,MAINTAIN'),
    'audit backend retains only append/read permissions');
SELECT ok(NOT has_sequence_privilege('service_role','public.audit_logs_id_seq','UPDATE'), 'backend cannot reset audit sequence');
RESET ROLE;
SELECT * FROM finish();
ROLLBACK;
