-- Backend-only execution control plane. Existing Agent configuration and chat
-- history remain in place. No data backfill or external I/O.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '2min';

CREATE TABLE public.agent_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id text NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,
    agent_id text NOT NULL REFERENCES public.access_surfaces(id) ON DELETE CASCADE,
    session_id text NOT NULL REFERENCES public.chat_sessions(id) ON DELETE CASCADE,
    user_id uuid NOT NULL,
    request_id uuid NOT NULL,
    input_sha256 text NOT NULL CHECK (input_sha256 ~ '^[0-9a-f]{64}$'),
    prompt text NOT NULL CHECK (length(prompt) BETWEEN 1 AND 100000),
    policy jsonb NOT NULL,
    state text NOT NULL DEFAULT 'queued' CHECK (state IN
        ('queued','running','waiting_approval','publishing','succeeded','stopped',
         'failed','conflict','outcome_unknown')),
    execution_id uuid,
    fence bigint NOT NULL DEFAULT 0,
    lease_until timestamptz,
    stop_requested boolean NOT NULL DEFAULT false,
    sequence bigint NOT NULL DEFAULT 0,
    snapshot jsonb NOT NULL DEFAULT '{}',
    checkpoint jsonb,
    publication jsonb,
    billing_run_id text,
    deadline timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(user_id, project_id, request_id)
);
CREATE UNIQUE INDEX agent_runs_session_busy ON public.agent_runs(session_id)
    WHERE state IN ('queued','running','waiting_approval','publishing');
CREATE INDEX agent_runs_dispatch ON public.agent_runs(lease_until,created_at)
    WHERE state IN ('queued','running','waiting_approval','publishing');
CREATE TABLE public.agent_run_executions (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES public.agent_runs(id) ON DELETE CASCADE,
    fence bigint NOT NULL,
    worker_id text NOT NULL,
    resource jsonb,
    cleaned boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(run_id,fence)
);
CREATE TABLE public.agent_run_events (
    run_id uuid NOT NULL REFERENCES public.agent_runs(id) ON DELETE CASCADE,
    sequence bigint NOT NULL,
    kind text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(run_id,sequence)
);
CREATE TABLE public.agent_run_tools (
    run_id uuid NOT NULL REFERENCES public.agent_runs(id) ON DELETE CASCADE,
    call_id text NOT NULL CHECK (length(call_id) BETWEEN 1 AND 256),
    name text NOT NULL,
    input jsonb NOT NULL,
    state text NOT NULL CHECK (state IN ('waiting','approved','rejected','executing','completed')),
    decision_id uuid,
    result jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(run_id,call_id)
);

-- Ordinary deletion is allowed after execution and recovery cleanup finish.
-- Cascading parent deletes must not erase an active run or its only recovery copy.
CREATE FUNCTION public.agent_run_delete_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
    IF OLD.state IN ('queued','running','waiting_approval','publishing')
        OR coalesce(OLD.snapshot->>'resource_retained','false')='true' THEN
        RAISE EXCEPTION 'agent_run_cleanup_required' USING ERRCODE='55000';
    END IF;
    RETURN OLD;
END $$;
CREATE TRIGGER agent_run_delete_guard BEFORE DELETE ON public.agent_runs
    FOR EACH ROW EXECUTE FUNCTION public.agent_run_delete_guard();

CREATE FUNCTION public.agent_run_event(p_run uuid,p_kind text,p_payload jsonb)
RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE seq bigint;
BEGIN
    IF octet_length(p_payload::text)>262144 THEN RAISE EXCEPTION 'agent_event_too_large'; END IF;
    UPDATE public.agent_runs SET sequence=sequence+1,updated_at=clock_timestamp()
        WHERE id=p_run RETURNING sequence INTO seq;
    INSERT INTO public.agent_run_events(run_id,sequence,kind,payload) VALUES(p_run,seq,p_kind,p_payload);
    DELETE FROM public.agent_run_events WHERE run_id=p_run AND sequence<=seq-512;
END $$;

CREATE FUNCTION public.agent_run_submit(p_user uuid,p_project text,p_agent text,p_session text,
    p_request uuid,p_digest text,p_prompt text,p_policy jsonb,p_timeout integer)
RETURNS jsonb LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE r public.agent_runs%ROWTYPE; s public.chat_sessions%ROWTYPE; sid text;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended('agent-submit:'||p_user||':'||p_project||':'||p_request,0));
    SELECT * INTO r FROM public.agent_runs WHERE user_id=p_user AND project_id=p_project AND request_id=p_request;
    IF FOUND THEN
        IF r.input_sha256<>p_digest THEN RAISE EXCEPTION 'agent_request_conflict' USING ERRCODE='22023'; END IF;
        RETURN to_jsonb(r);
    END IF;
    IF p_timeout NOT BETWEEN 1 AND 86400 THEN RAISE EXCEPTION 'agent_timeout_invalid'; END IF;
    PERFORM 1 FROM public.access_surfaces WHERE id=p_agent AND project_id=p_project AND kind='agent' AND status='active';
    IF NOT FOUND THEN RAISE EXCEPTION 'agent_unavailable'; END IF;
    IF p_session IS NULL THEN
        INSERT INTO public.chat_sessions(user_id,agent_id,title,mode)
            VALUES(p_user,p_agent,left(p_prompt,80),'cloud_pi') RETURNING id INTO sid;
    ELSE
        SELECT * INTO s FROM public.chat_sessions WHERE id=p_session FOR UPDATE;
        IF NOT FOUND OR s.user_id<>p_user OR s.agent_id IS DISTINCT FROM p_agent OR s.mode<>'cloud_pi' THEN
            RAISE EXCEPTION 'agent_session_mismatch' USING ERRCODE='42501';
        END IF;
        sid := s.id;
    END IF;
    IF EXISTS(SELECT 1 FROM public.agent_runs WHERE session_id=sid AND state IN ('queued','running','waiting_approval','publishing')) THEN
        RAISE EXCEPTION 'agent_session_busy' USING ERRCODE='55000';
    END IF;
    INSERT INTO public.agent_runs(project_id,agent_id,session_id,user_id,request_id,input_sha256,prompt,policy,deadline)
        VALUES(p_project,p_agent,sid,p_user,p_request,p_digest,p_prompt,p_policy,clock_timestamp()+make_interval(secs=>p_timeout))
        RETURNING * INTO r;
    INSERT INTO public.chat_messages(session_id,role,content) VALUES(sid,'user',p_prompt);
    PERFORM public.agent_run_event(r.id,'accepted',jsonb_build_object('state','queued'));
    SELECT * INTO r FROM public.agent_runs WHERE id=r.id;
    RETURN to_jsonb(r);
END $$;

CREATE FUNCTION public.agent_run_claim(p_worker text,p_ttl integer DEFAULT 45)
RETURNS jsonb LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE r public.agent_runs%ROWTYPE; execution uuid:=gen_random_uuid();
BEGIN
    IF p_ttl NOT BETWEEN 10 AND 120 THEN RAISE EXCEPTION 'agent_lease_invalid'; END IF;
    SELECT * INTO r FROM public.agent_runs WHERE (state IN ('queued','running','waiting_approval','publishing')
        OR snapshot->>'resource_retained'='true')
        AND (lease_until IS NULL OR lease_until<clock_timestamp()) ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1;
    IF NOT FOUND THEN RETURN NULL; END IF;
    UPDATE public.agent_runs SET execution_id=execution,fence=fence+1,
        lease_until=clock_timestamp()+make_interval(secs=>p_ttl)
        WHERE id=r.id RETURNING * INTO r;
    INSERT INTO public.agent_run_executions(id,run_id,fence,worker_id) VALUES(execution,r.id,r.fence,p_worker);
    PERFORM public.agent_run_event(r.id,'execution',jsonb_build_object('execution_id',execution,'fence',r.fence));
    RETURN to_jsonb(r);
END $$;

CREATE FUNCTION public.agent_run_assert(p_run uuid,p_execution uuid,p_fence bigint)
RETURNS public.agent_runs LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE r public.agent_runs%ROWTYPE;
BEGIN
    SELECT * INTO r FROM public.agent_runs WHERE id=p_run FOR UPDATE;
    IF NOT FOUND OR r.execution_id IS DISTINCT FROM p_execution OR r.fence<>p_fence OR r.lease_until<=clock_timestamp()
        OR (r.state NOT IN ('queued','running','waiting_approval','publishing')
            AND coalesce(r.snapshot->>'resource_retained','false')<>'true') THEN
        RAISE EXCEPTION 'agent_execution_fenced' USING ERRCODE='55000';
    END IF;
    RETURN r;
END $$;

CREATE FUNCTION public.agent_run_write(p_run uuid,p_execution uuid,p_fence bigint,
    p_kind text,p_payload jsonb DEFAULT '{}',p_patch jsonb DEFAULT '{}')
RETURNS jsonb LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE r public.agent_runs%ROWTYPE; new_state text;
BEGIN
    r:=public.agent_run_assert(p_run,p_execution,p_fence);
    IF p_patch - ARRAY['state','snapshot','checkpoint','publication','billing_run_id','resource','cleaned'] <> '{}'::jsonb THEN
        RAISE EXCEPTION 'agent_patch_invalid';
    END IF;
    new_state:=coalesce(p_patch->>'state',r.state);
    IF r.state NOT IN ('queued','running','waiting_approval','publishing') AND new_state<>r.state THEN
        RAISE EXCEPTION 'agent_terminal_state_immutable';
    END IF;
    IF new_state='succeeded' AND coalesce((coalesce(p_patch->'publication',r.publication))->>'status','') NOT IN ('committed','no_changes') THEN
        RAISE EXCEPTION 'agent_publication_unconfirmed';
    END IF;
    UPDATE public.agent_runs SET state=new_state,
        snapshot=CASE WHEN p_patch ? 'snapshot' THEN p_patch->'snapshot' ELSE snapshot END,
        checkpoint=CASE WHEN p_patch ? 'checkpoint' THEN p_patch->'checkpoint' ELSE checkpoint END,
        publication=CASE WHEN p_patch ? 'publication' THEN p_patch->'publication' ELSE publication END,
        billing_run_id=CASE WHEN p_patch ? 'billing_run_id' THEN p_patch->>'billing_run_id' ELSE billing_run_id END,
        lease_until=clock_timestamp()+interval '45 seconds'
        WHERE id=p_run;
    IF p_patch ? 'resource' THEN
        UPDATE public.agent_run_executions SET resource=p_patch->'resource' WHERE id=p_execution;
    END IF;
    IF p_patch ? 'cleaned' THEN
        UPDATE public.agent_run_executions SET cleaned=(p_patch->>'cleaned')::boolean WHERE id=p_execution;
    END IF;
    IF new_state NOT IN ('queued','running','waiting_approval','publishing') THEN
        INSERT INTO public.chat_messages(id,session_id,role,content,parts)
            SELECT 'agent-run:'||id,session_id,'assistant',coalesce(snapshot->>'text',''),
                jsonb_build_array(jsonb_build_object('type','agent_run','run_id',id,'state',state))
            FROM public.agent_runs WHERE id=p_run
            ON CONFLICT(id) DO NOTHING;
        UPDATE public.chat_sessions SET updated_at=clock_timestamp() WHERE id=r.session_id;
    END IF;
    IF p_kind<>'heartbeat' THEN PERFORM public.agent_run_event(p_run,p_kind,p_payload); END IF;
    SELECT * INTO r FROM public.agent_runs WHERE id=p_run;
    RETURN to_jsonb(r);
END $$;

CREATE FUNCTION public.agent_run_tool(p_run uuid,p_execution uuid,p_fence bigint,
    p_call text,p_name text,p_input jsonb,p_state text,p_result jsonb DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE r public.agent_runs%ROWTYPE; t public.agent_run_tools%ROWTYPE;
BEGIN
    r:=public.agent_run_assert(p_run,p_execution,p_fence);
    IF r.stop_requested AND p_state<>'completed' THEN RAISE EXCEPTION 'agent_stop_requested'; END IF;
    IF octet_length(p_input::text)>131072 OR octet_length(p_result::text)>262144 THEN
        RAISE EXCEPTION 'agent_tool_payload_too_large';
    END IF;
    SELECT * INTO t FROM public.agent_run_tools WHERE run_id=p_run AND call_id=p_call FOR UPDATE;
    IF NOT FOUND THEN
        IF p_state NOT IN ('waiting','executing') THEN RAISE EXCEPTION 'agent_tool_transition'; END IF;
        INSERT INTO public.agent_run_tools(run_id,call_id,name,input,state) VALUES(p_run,p_call,p_name,p_input,p_state) RETURNING * INTO t;
    ELSE
        IF t.name<>p_name OR t.input<>p_input THEN RAISE EXCEPTION 'agent_tool_identity_conflict'; END IF;
        IF p_state='completed' AND t.state='executing' THEN
            UPDATE public.agent_run_tools SET state='completed',result=p_result,updated_at=clock_timestamp()
                WHERE run_id=p_run AND call_id=p_call RETURNING * INTO t;
        ELSIF p_state='executing' AND t.state='approved' THEN
            UPDATE public.agent_run_tools SET state='executing',updated_at=clock_timestamp()
                WHERE run_id=p_run AND call_id=p_call RETURNING * INTO t;
        ELSIF p_state NOT IN ('waiting',t.state) THEN RAISE EXCEPTION 'agent_tool_transition';
        END IF;
    END IF;
    PERFORM public.agent_run_event(p_run,'tool',jsonb_build_object('call_id',p_call,'name',p_name,'state',t.state));
    RETURN to_jsonb(t);
END $$;

CREATE FUNCTION public.agent_run_command(p_run uuid,p_user uuid,p_command text,p_call text DEFAULT NULL,
    p_decision uuid DEFAULT NULL,p_allow boolean DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE r public.agent_runs%ROWTYPE; t public.agent_run_tools%ROWTYPE; decision text;
BEGIN
    SELECT * INTO r FROM public.agent_runs WHERE id=p_run AND user_id=p_user FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'agent_run_not_found' USING ERRCODE='42501'; END IF;
    IF p_command='stop' THEN
        IF r.state IN ('queued','running','waiting_approval','publishing') AND NOT r.stop_requested THEN
            UPDATE public.agent_runs SET stop_requested=true WHERE id=p_run;
            PERFORM public.agent_run_event(p_run,'stop_requested','{}');
        END IF;
    ELSIF p_command='approve' THEN
        IF p_decision IS NULL OR p_allow IS NULL THEN
            RAISE EXCEPTION 'agent_approval_unavailable';
        END IF;
        SELECT * INTO t FROM public.agent_run_tools WHERE run_id=p_run AND call_id=p_call FOR UPDATE;
        IF NOT FOUND THEN RAISE EXCEPTION 'agent_tool_not_found'; END IF;
        decision:=CASE WHEN p_allow THEN 'approved' ELSE 'rejected' END;
        IF t.decision_id IS NOT NULL THEN
            IF t.decision_id<>p_decision OR (t.state='rejected')<>NOT p_allow THEN RAISE EXCEPTION 'agent_approval_conflict'; END IF;
        ELSE
            IF t.state<>'waiting' OR r.stop_requested OR r.state NOT IN ('running','waiting_approval') THEN RAISE EXCEPTION 'agent_approval_unavailable'; END IF;
            UPDATE public.agent_run_tools SET state=decision,decision_id=p_decision,updated_at=clock_timestamp()
                WHERE run_id=p_run AND call_id=p_call;
            PERFORM public.agent_run_event(p_run,'approval',jsonb_build_object('call_id',p_call,'state',decision));
        END IF;
    ELSE RAISE EXCEPTION 'agent_command_invalid'; END IF;
    SELECT * INTO r FROM public.agent_runs WHERE id=p_run;
    RETURN to_jsonb(r);
END $$;

-- Admission of a first built-in Agent is explicit and service-authorized. It
-- serializes only this target, and never overwrites a configured Agent.
CREATE FUNCTION public.agent_run_builtin(p_project text,p_scope text,p_user uuid,p_config jsonb)
RETURNS text LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE aid text; oid text;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended('agent-builtin:'||p_project||':'||coalesce(p_scope,''),0));
    SELECT id INTO aid FROM public.access_surfaces WHERE project_id=p_project AND scope_id IS NOT DISTINCT FROM p_scope
        AND kind='agent' AND coalesce(config->>'visibility','org')<>'private' ORDER BY created_at LIMIT 1;
    IF FOUND THEN RETURN aid; END IF;
    SELECT org_id INTO oid FROM public.projects WHERE id=p_project;
    IF oid IS NULL THEN RAISE EXCEPTION 'agent_project_not_found'; END IF;
    IF p_scope IS NOT NULL AND NOT EXISTS(SELECT 1 FROM public.repository_scopes WHERE id=p_scope AND project_id=p_project) THEN
        RAISE EXCEPTION 'agent_scope_not_found';
    END IF;
    INSERT INTO public.access_surfaces(project_id,scope_id,org_id,kind,name,config,created_by)
        VALUES(p_project,p_scope,oid,'agent','PuppyOne Agent',p_config,p_user) RETURNING id INTO aid;
    RETURN aid;
END $$;

ALTER TABLE public.agent_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.agent_run_executions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.agent_run_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.agent_run_tools ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.agent_runs,public.agent_run_executions,public.agent_run_events,public.agent_run_tools FROM PUBLIC,anon,authenticated;
GRANT ALL ON public.agent_runs,public.agent_run_executions,public.agent_run_events,public.agent_run_tools TO service_role;
DO $$ DECLARE f record; BEGIN
    FOR f IN SELECT oid::regprocedure AS signature FROM pg_proc WHERE pronamespace='public'::regnamespace AND proname LIKE 'agent_run_%' LOOP
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC, anon, authenticated',f.signature);
        EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO service_role',f.signature);
    END LOOP;
END $$;
COMMIT;
