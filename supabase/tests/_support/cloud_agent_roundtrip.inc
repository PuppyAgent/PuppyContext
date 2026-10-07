-- Synthetic local/CI exercise only. Never invoke against a shared worker queue.
BEGIN;
DO $$
DECLARE
    uid uuid:=gen_random_uuid(); project text:=gen_random_uuid()::text;
    org text:=gen_random_uuid()::text; agent text; request uuid:=gen_random_uuid();
    policy jsonb; submitted jsonb; repeated jsonb; claimed jsonb; completed jsonb;
    run_id uuid; execution uuid; fence bigint;
BEGIN
    IF EXISTS(SELECT FROM public.agent_runs) THEN
        RAISE EXCEPTION 'Cloud Agent roundtrip requires an empty owned test database';
    END IF;
    INSERT INTO auth.users(id) VALUES(uid);
    INSERT INTO public.organizations(id,name,slug,created_by) VALUES(org,'Agent test','agent-'||org,uid);
    INSERT INTO public.org_members(org_id,user_id,role) VALUES(org,uid,'owner');
    INSERT INTO public.projects(id,name,org_id,created_by,lifecycle_status) VALUES(project,'Agent test',org,uid,'ready');
    INSERT INTO public.project_members(project_id,org_id,user_id,role) VALUES(project,org,uid,'admin');
    agent:=public.agent_run_builtin(project,NULL,uid,'{"name":"Agent test"}');
    SELECT jsonb_build_object('surface_updated_at',updated_at,'model','fixture',
        'revision',public.authorization_revision_token(project)) INTO policy
        FROM public.access_surfaces WHERE id=agent;
    SET LOCAL ROLE service_role;
    submitted:=public.agent_run_submit_context(uid,project,agent,NULL,request,repeat('a',64),'List files',policy,600);
    repeated:=public.agent_run_submit_context(uid,project,agent,NULL,request,repeat('a',64),'List files',policy,600);
    IF submitted->>'id' IS DISTINCT FROM repeated->>'id' OR (SELECT count(*) FROM public.agent_runs)<>1 THEN
        RAISE EXCEPTION 'Duplicate submission created another run';
    END IF;
    claimed:=public.agent_run_claim('release-test',45);
    run_id:=(claimed->>'id')::uuid; execution:=(claimed->>'execution_id')::uuid; fence:=(claimed->>'fence')::bigint;
    IF run_id IS DISTINCT FROM (submitted->>'id')::uuid THEN RAISE EXCEPTION 'Worker could not claim submitted run'; END IF;
    PERFORM public.agent_run_write(run_id,execution,fence,'publishing','{}','{"state":"publishing"}');
    -- Executes the actual actor/lease dependency; function existence alone is insufficient.
    PERFORM public.agent_run_publication_fence(run_id,execution,fence,project);
    BEGIN
        PERFORM public.agent_run_write(run_id,gen_random_uuid(),fence,'stale');
        RAISE EXCEPTION 'Stale execution was accepted';
    EXCEPTION WHEN SQLSTATE '55000' THEN NULL;
    END;
    completed:=public.agent_run_write(run_id,execution,fence,'completed','{}',
        '{"state":"succeeded","snapshot":{"text":"File listing complete"},"publication":{"status":"no_changes"}}');
    IF completed->>'state'<>'succeeded' OR NOT EXISTS(SELECT FROM public.chat_messages
        WHERE session_id=submitted->>'session_id' AND role='assistant' AND content='File listing complete') THEN
        RAISE EXCEPTION 'Durable assistant reply missing';
    END IF;
    IF public.agent_run_claim('release-test',45) IS NOT NULL THEN RAISE EXCEPTION 'Completed run was claimed again'; END IF;
END $$;
ROLLBACK;
