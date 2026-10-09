-- Expand: operation-owned Agent transactions. Existing functions remain for
-- deployed readers/writers; new code uses these service-only named commands.
-- No data rewrite. Row locks are bounded to one Run; external I/O is outside SQL.

BEGIN;

CREATE FUNCTION public.agent_run_guard(p_run uuid,p_execution uuid,p_fence bigint)
RETURNS jsonb LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE status jsonb; r public.agent_runs%ROWTYPE;
BEGIN
 status:=public.agent_run_renew(p_run,p_execution,p_fence);
 IF status->>'code' IS NULL THEN
   SELECT * INTO STRICT r FROM public.agent_runs WHERE id=p_run;
   -- Hold the authorization revision guard through the entire command, not
   -- merely a read/check before a second HTTP request.
   PERFORM public.authorization_assert_revision(r.project_id,r.policy->'revision');
 END IF;
 RETURN status;
END $$;

CREATE FUNCTION public.agent_run_start_execution(p_run uuid,p_execution uuid,p_fence bigint,
 p_checkpoint jsonb,p_billing text,p_resource jsonb)
RETURNS jsonb LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE status jsonb; r jsonb;
BEGIN
 status:=public.agent_run_guard(p_run,p_execution,p_fence);
 IF status->>'code' IS NULL THEN
   r:=public.agent_run_append_batch(p_run,p_execution,p_fence,p_execution,
     jsonb_build_array(jsonb_build_object('kind','state','payload','{"state":"running"}'::jsonb,
       'patch',jsonb_build_object('state','running','checkpoint',p_checkpoint,'billing_run_id',p_billing,'resource',p_resource))));
 ELSE SELECT to_jsonb(a) INTO r FROM public.agent_runs a WHERE id=p_run;
 END IF;
 RETURN jsonb_build_object('run',r,'status',status);
END $$;

CREATE FUNCTION public.agent_run_begin_model(p_run uuid,p_execution uuid,p_fence bigint,
 p_request uuid,p_checkpoint jsonb,p_limit integer)
RETURNS jsonb LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE status jsonb; r jsonb; n integer; events jsonb;
BEGIN
 status:=public.agent_run_guard(p_run,p_execution,p_fence);
 SELECT to_jsonb(a) INTO r FROM public.agent_runs a WHERE id=p_run;
 IF status->>'code' IS NULL THEN
   -- Batch digest is independent of the current counter so ACK retries reuse
   -- the same input, even after the counter has already advanced.
   events:=jsonb_build_array(jsonb_build_object('kind','model','payload',jsonb_build_object('request_id',p_request),
     'patch',jsonb_build_object('checkpoint',p_checkpoint)));
   IF NOT EXISTS(SELECT 1 FROM public.agent_run_event_batches
       WHERE run_id=p_run AND execution_id=p_execution AND batch_id=p_request) THEN
     n:=coalesce((r->'snapshot'->>'model_calls')::integer,0)+1;
     IF p_limit<1 OR n>p_limit THEN RAISE EXCEPTION 'agent_model_limit'; END IF;
     UPDATE public.agent_runs SET snapshot=jsonb_set(snapshot,'{model_calls}',to_jsonb(n)) WHERE id=p_run;
   END IF;
   r:=public.agent_run_append_batch(p_run,p_execution,p_fence,p_request,events);
 END IF;
 RETURN jsonb_build_object('run',r,'status',status);
END $$;

CREATE FUNCTION public.agent_run_begin_tool(p_run uuid,p_execution uuid,p_fence bigint,
 p_call text,p_name text,p_input jsonb,p_checkpoint jsonb,p_mutation boolean)
RETURNS jsonb LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE status jsonb; r jsonb; t jsonb; existing public.agent_run_tools%ROWTYPE; next_state text;
BEGIN
 status:=public.agent_run_guard(p_run,p_execution,p_fence);
 SELECT to_jsonb(a) INTO r FROM public.agent_runs a WHERE id=p_run;
 IF status->>'code' IS NULL THEN
   SELECT * INTO existing FROM public.agent_run_tools WHERE run_id=p_run AND call_id=p_call;
   IF FOUND AND (existing.name IS DISTINCT FROM p_name OR existing.input IS DISTINCT FROM p_input) THEN
     RAISE EXCEPTION 'agent_tool_identity_conflict';
   END IF;
   IF existing.state IN ('completed','rejected','executing','waiting') THEN
     t:=to_jsonb(existing);
   ELSE
     t:=public.agent_run_tool(p_run,p_execution,p_fence,p_call,p_name,p_input,
       CASE WHEN existing.state='approved' OR NOT p_mutation THEN 'executing' ELSE 'waiting' END);
     next_state:=CASE WHEN t->>'state'='waiting' THEN 'waiting_approval' ELSE 'running' END;
     r:=public.agent_run_write(p_run,p_execution,p_fence,'checkpoint',jsonb_build_object('reason','before_tool'),
       jsonb_build_object('checkpoint',p_checkpoint,'state',next_state));
   END IF;
 END IF;
 RETURN jsonb_build_object('run',r,'status',status,'tool',t);
END $$;

CREATE FUNCTION public.agent_run_complete_tool(p_run uuid,p_execution uuid,p_fence bigint,
 p_call text,p_name text,p_input jsonb,p_result jsonb,p_checkpoint jsonb)
RETURNS jsonb LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE r jsonb; t public.agent_run_tools%ROWTYPE;
BEGIN
 -- Completion preserves an already executed side effect, even if stopped or
 -- revoked in the meantime. Ownership/fence is always checked.
 r:=to_jsonb(public.agent_run_assert(p_run,p_execution,p_fence));
 SELECT * INTO t FROM public.agent_run_tools WHERE run_id=p_run AND call_id=p_call;
 IF t.state='completed' THEN
   IF t.name IS DISTINCT FROM p_name OR t.input IS DISTINCT FROM p_input OR t.result IS DISTINCT FROM p_result THEN
     RAISE EXCEPTION 'agent_tool_result_conflict';
   END IF;
   RETURN jsonb_build_object('run',r,'tool',to_jsonb(t));
 END IF;
 PERFORM public.agent_run_tool(p_run,p_execution,p_fence,p_call,p_name,p_input,'completed',p_result);
 r:=public.agent_run_write(p_run,p_execution,p_fence,'checkpoint','{"reason":"after_tool"}',
   jsonb_build_object('checkpoint',p_checkpoint));
 SELECT * INTO t FROM public.agent_run_tools WHERE run_id=p_run AND call_id=p_call;
 RETURN jsonb_build_object('run',r,'tool',to_jsonb(t));
END $$;

CREATE FUNCTION public.agent_run_settle_model(p_run uuid,p_execution uuid,p_fence bigint,p_checkpoint jsonb)
RETURNS jsonb LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE status jsonb; r jsonb;
BEGIN
 status:=public.agent_run_guard(p_run,p_execution,p_fence);
 IF status->>'code' IS NULL THEN
   r:=public.agent_run_write(p_run,p_execution,p_fence,'checkpoint','{"reason":"agent_settled"}',
     jsonb_build_object('checkpoint',p_checkpoint));
 ELSE SELECT to_jsonb(a) INTO r FROM public.agent_runs a WHERE id=p_run;
 END IF;
 RETURN jsonb_build_object('run',r,'status',status);
END $$;

CREATE FUNCTION public.agent_run_finish(p_run uuid,p_execution uuid,p_fence bigint,
 p_state text,p_code text,p_snapshot jsonb,p_publication jsonb,p_cleaned boolean)
RETURNS jsonb LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE r public.agent_runs%ROWTYPE;
BEGIN
 r:=public.agent_run_assert(p_run,p_execution,p_fence);
 IF coalesce(p_publication,'null'::jsonb) IS DISTINCT FROM coalesce(r.publication,'null'::jsonb) THEN
   PERFORM public.agent_run_write(p_run,p_execution,p_fence,'publication',p_publication,
     jsonb_build_object('publication',p_publication));
 END IF;
 RETURN public.agent_run_write(p_run,p_execution,p_fence,'terminal',
   jsonb_build_object('state',p_state,'code',p_code,'resource_retained',coalesce((p_snapshot->>'resource_retained')::boolean,false)),
   jsonb_build_object('state',p_state,'snapshot',p_snapshot) ||
     CASE WHEN p_cleaned THEN '{"cleaned":true}'::jsonb ELSE '{}'::jsonb END);
END $$;

DO $$ DECLARE name text; f record; BEGIN
 FOREACH name IN ARRAY ARRAY['agent_run_guard','agent_run_start_execution','agent_run_begin_model',
   'agent_run_begin_tool','agent_run_complete_tool','agent_run_settle_model','agent_run_finish'] LOOP
   FOR f IN SELECT oid::regprocedure AS signature FROM pg_proc
     WHERE pronamespace='public'::regnamespace AND proname=name LOOP
     EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC,anon,authenticated',f.signature);
     EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO service_role',f.signature);
   END LOOP;
 END LOOP;
END $$;

NOTIFY pgrst,'reload schema';

COMMIT;
