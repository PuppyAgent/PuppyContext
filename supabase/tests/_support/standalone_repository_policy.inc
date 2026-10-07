-- Owned installation/CI fixture only; configuration rolls back with the data.
BEGIN;
DO $$
DECLARE uid uuid:=gen_random_uuid(); org text:=gen_random_uuid()::text;
    project text:=gen_random_uuid()::text; result jsonb; policy jsonb;
BEGIN
    IF (SELECT mode FROM public.repository_entitlement_source)<>'db' THEN
        RAISE EXCEPTION 'hosted entitlement enforcement is not the default';
    END IF;
    BEGIN
        PERFORM public._version_billing_entitlement('missing-entitlement-org');
        RAISE EXCEPTION 'missing hosted entitlement was accepted';
    EXCEPTION WHEN SQLSTATE '55000' THEN
        IF SQLERRM<>'storage_billing_entitlement_unavailable' THEN RAISE; END IF;
    END;
    IF has_function_privilege('service_role','public.configure_repository_entitlement_source(text)','EXECUTE')
       OR has_table_privilege('service_role','public.repository_entitlement_source','UPDATE') THEN
        RAISE EXCEPTION 'runtime credentials can change deployment policy';
    END IF;
    PERFORM public.configure_repository_entitlement_source('disabled');
    INSERT INTO auth.users(id) VALUES(uid);
    INSERT INTO public.organizations(id,name,slug,created_by) VALUES(org,'Standalone test','standalone-'||org,uid);
    INSERT INTO public.org_members(org_id,user_id,role) VALUES(org,uid,'owner');
    SET LOCAL ROLE service_role;
    result:=public.create_native_project_repository(gen_random_uuid()::text,repeat('a',64),
        project,'Standalone project','',org,uid,gen_random_uuid()::text,'empty');
    IF result->>'outcome'<>'native_created' THEN RAISE EXCEPTION 'standalone project creation failed'; END IF;
    policy:=public.check_version_repository_file_policy(project);
    IF policy->'file_limit' IS DISTINCT FROM 'null'::jsonb OR (policy->>'source_revision')::bigint<=0 THEN
        RAISE EXCEPTION 'standalone file policy is invalid';
    END IF;
    IF EXISTS(SELECT FROM public.organization_entitlements WHERE org_id=org) THEN
        RAISE EXCEPTION 'standalone installation fabricated a hosted entitlement';
    END IF;
    RESET ROLE;
    PERFORM public.configure_repository_entitlement_source('disabled');
    BEGIN
        PERFORM public.configure_repository_entitlement_source('db');
        RAISE EXCEPTION 'deployment policy changed under existing native repositories';
    EXCEPTION WHEN SQLSTATE 'P0001' THEN
        IF SQLERRM<>'repository_entitlement_source_in_use' THEN RAISE; END IF;
    END;
END $$;
ROLLBACK;
