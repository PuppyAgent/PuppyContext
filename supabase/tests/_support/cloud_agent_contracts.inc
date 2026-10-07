-- Read-only hosted check: verify the Agent control plane before starting workers.
BEGIN READ ONLY;
SET LOCAL statement_timeout = '30s';
DO $$
DECLARE relation text; signature text; role_name text; object record;
BEGIN
    FOREACH relation IN ARRAY ARRAY['agent_runs','agent_run_executions','agent_run_events','agent_run_tools'] LOOP
        IF to_regclass('public.' || relation) IS NULL THEN
            RAISE EXCEPTION 'CLOUD_AGENT_SCHEMA_MISSING: %', relation;
        END IF;
        IF NOT (SELECT relrowsecurity FROM pg_class WHERE oid=to_regclass('public.' || relation)) THEN
            RAISE EXCEPTION 'CLOUD_AGENT_RLS_MISSING: %', relation;
        END IF;
        FOREACH role_name IN ARRAY ARRAY['anon','authenticated'] LOOP
            IF has_table_privilege(role_name,'public.' || relation,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') THEN
                RAISE EXCEPTION 'CLOUD_AGENT_CLIENT_ACCESS: % %', role_name, relation;
            END IF;
        END LOOP;
        IF NOT has_table_privilege('service_role','public.' || relation,'SELECT,INSERT,UPDATE,DELETE') THEN
            RAISE EXCEPTION 'CLOUD_AGENT_BACKEND_ACCESS_MISSING: %', relation;
        END IF;
    END LOOP;
    FOREACH signature IN ARRAY ARRAY[
        'public.agent_run_submit(uuid,text,text,text,uuid,text,text,jsonb,integer)',
        'public.agent_run_claim(text,integer)',
        'public.agent_run_builtin(text,text,uuid,jsonb)',
        'public.agent_run_publication_fence(uuid,uuid,bigint,text)',
        'public._version_assert_current_repository_actor(text,text,text,boolean)',
        'public._version_assert_write_lease(text,uuid,text)'
    ] LOOP
        IF to_regprocedure(signature) IS NULL THEN
            RAISE EXCEPTION 'CLOUD_AGENT_FUNCTION_MISSING: %', signature;
        END IF;
    END LOOP;
    FOR object IN SELECT oid, proname FROM pg_proc
        WHERE pronamespace='public'::regnamespace AND proname LIKE 'agent_run_%' LOOP
        IF has_function_privilege('anon',object.oid,'EXECUTE')
            OR has_function_privilege('authenticated',object.oid,'EXECUTE')
            OR NOT has_function_privilege('service_role',object.oid,'EXECUTE') THEN
            RAISE EXCEPTION 'CLOUD_AGENT_RPC_ACCESS_INVALID: %', object.proname;
        END IF;
    END LOOP;
    IF (SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND tgenabled<>'D'
        AND (tgrelid,tgname) IN (
            ('public.agent_runs'::regclass,'agent_run_delete_guard'),
            ('public.version_transactions'::regclass,'agent_run_legacy_receipt'),
            ('public.version_ref_transactions'::regclass,'agent_run_native_receipt')
        )) <> 3 THEN
        RAISE EXCEPTION 'CLOUD_AGENT_PUBLICATION_GUARD_MISSING';
    END IF;
END $$;
ROLLBACK;
