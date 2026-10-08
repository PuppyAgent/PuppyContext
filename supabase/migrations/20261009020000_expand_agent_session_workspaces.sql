-- Session-owned compute. Run fences remain the sole execution authority.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '2min';

CREATE TABLE public.agent_session_workspaces (
 -- Provider cleanup ownership must survive deletion of its product owner.
 -- Acquisition binds these identities through the fenced authoritative Run.
 session_id text PRIMARY KEY,
 project_id text NOT NULL,
 user_id uuid NOT NULL,
 generation uuid NOT NULL DEFAULT gen_random_uuid(),
 version bigint NOT NULL DEFAULT 1,
 binding jsonb NOT NULL,
 state text NOT NULL CHECK(state IN ('allocating','resuming','running','pausing','paused','retained','cleanup_pending','retired')),
 run_id uuid REFERENCES public.agent_runs(id) ON DELETE SET NULL,
 execution_id uuid,
 fence bigint,
 resource jsonb,
 operation_id uuid NOT NULL DEFAULT gen_random_uuid(),
 operation_until timestamptz,
 last_user_activity_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 idle_since timestamptz,
 retire_after timestamptz,
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX agent_session_workspaces_due ON public.agent_session_workspaces(retire_after)
 WHERE state='paused';
CREATE INDEX agent_session_workspaces_cleanup ON public.agent_session_workspaces(operation_until)
 WHERE state='cleanup_pending';
CREATE INDEX agent_session_workspaces_project ON public.agent_session_workspaces(project_id);
ALTER TABLE public.agent_session_workspaces ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.agent_session_workspaces FROM PUBLIC,anon,authenticated;
GRANT ALL ON public.agent_session_workspaces TO service_role;

CREATE FUNCTION public.agent_workspace_delete_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF OLD.state<>'retired' THEN RAISE EXCEPTION 'agent_workspace_cleanup_required'; END IF;
 RETURN OLD;
END $$;
CREATE TRIGGER agent_workspace_delete_guard BEFORE DELETE ON public.agent_session_workspaces
 FOR EACH ROW EXECUTE FUNCTION public.agent_workspace_delete_guard();

CREATE FUNCTION public.agent_workspace_owner_deleted() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 -- Explicit owner deletion transfers exact resource identity to the durable
 -- cleanup queue. A cascading FK must never erase its only cleanup receipt.
 IF TG_TABLE_NAME='chat_sessions' THEN
   UPDATE public.agent_session_workspaces SET state='cleanup_pending',version=version+1,
     operation_id=gen_random_uuid(),operation_until=clock_timestamp(),retire_after=NULL
   WHERE session_id=OLD.id AND state<>'retired';
 ELSE
   UPDATE public.agent_session_workspaces SET state='cleanup_pending',version=version+1,
     operation_id=gen_random_uuid(),operation_until=clock_timestamp(),retire_after=NULL
   WHERE project_id=OLD.id AND state<>'retired';
 END IF;
 RETURN OLD;
END $$;
CREATE TRIGGER agent_workspace_session_deleted BEFORE DELETE ON public.chat_sessions
 FOR EACH ROW EXECUTE FUNCTION public.agent_workspace_owner_deleted();
CREATE TRIGGER agent_workspace_project_deleted BEFORE DELETE ON public.projects
 FOR EACH ROW EXECUTE FUNCTION public.agent_workspace_owner_deleted();

CREATE FUNCTION public.agent_run_workspace_acquire(p_run uuid,p_execution uuid,p_fence bigint,p_binding jsonb)
RETURNS jsonb LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE r public.agent_runs%ROWTYPE; w public.agent_session_workspaces%ROWTYPE; status jsonb;
BEGIN
 status:=public.agent_run_guard(p_run,p_execution,p_fence);
 IF status->>'code' IS NOT NULL THEN RAISE EXCEPTION 'agent_workspace_admission_denied'; END IF;
 SELECT * INTO STRICT r FROM public.agent_runs WHERE id=p_run;
 IF jsonb_typeof(p_binding)<>'object' OR octet_length(p_binding::text)>8192 THEN
   RAISE EXCEPTION 'agent_workspace_binding_invalid'; END IF;
 INSERT INTO public.agent_session_workspaces(session_id,project_id,user_id,binding,state,run_id,execution_id,fence)
 VALUES(r.session_id,r.project_id,r.user_id,p_binding,'allocating',r.id,p_execution,p_fence)
 ON CONFLICT(session_id) DO NOTHING;
 SELECT * INTO STRICT w FROM public.agent_session_workspaces WHERE session_id=r.session_id FOR UPDATE;
 IF w.project_id<>r.project_id OR w.user_id<>r.user_id THEN RAISE EXCEPTION 'agent_workspace_identity_mismatch'; END IF;
 -- Takeover has already quiesced/deleted the old run resource and confirmed
 -- that cleanup on its execution receipt before preparing a replacement.
 IF w.run_id=p_run AND w.fence<p_fence AND EXISTS(SELECT 1 FROM public.agent_run_executions
     WHERE id=w.execution_id AND cleaned) THEN
   UPDATE public.agent_session_workspaces SET state='retired',resource=NULL WHERE session_id=r.session_id RETURNING * INTO w;
 END IF;
 IF w.state='cleanup_pending' THEN
   IF w.execution_id=p_execution AND w.fence=p_fence THEN RETURN to_jsonb(w); END IF;
   RAISE EXCEPTION 'agent_workspace_busy';
 END IF;
 IF w.state='retained' THEN RAISE EXCEPTION 'agent_workspace_requires_resolution'; END IF;
 IF w.execution_id=p_execution AND w.fence=p_fence AND w.state IN ('allocating','resuming','running') THEN
   IF w.binding<>p_binding THEN RAISE EXCEPTION 'agent_workspace_binding_mismatch'; END IF;
   RETURN to_jsonb(w);
 END IF;
 IF w.state NOT IN ('paused','retired') THEN RAISE EXCEPTION 'agent_workspace_requires_recovery'; END IF;
 IF EXISTS(SELECT 1 FROM public.agent_runs WHERE id=w.run_id AND id<>p_run
   AND state IN ('queued','running','waiting_approval','publishing')) THEN RAISE EXCEPTION 'agent_workspace_busy'; END IF;
 -- An admitted new target/artifact first retires the old clean resource.
 IF w.state='paused' AND w.binding<>p_binding THEN
   UPDATE public.agent_session_workspaces SET state='cleanup_pending',run_id=p_run,
     execution_id=p_execution,fence=p_fence,version=version+1,operation_id=gen_random_uuid(),
     operation_until=clock_timestamp()+interval '2 minutes',idle_since=NULL,retire_after=NULL
   WHERE session_id=r.session_id RETURNING * INTO w;
   RETURN to_jsonb(w);
 END IF;
 UPDATE public.agent_session_workspaces SET
   generation=CASE WHEN state='retired' THEN gen_random_uuid() ELSE generation END,
   resource=CASE WHEN state='retired' THEN NULL ELSE resource END,
   state=CASE WHEN state='retired' THEN 'allocating' ELSE 'resuming' END,
   binding=p_binding,run_id=p_run,execution_id=p_execution,fence=p_fence,
   version=version+1,operation_id=gen_random_uuid(),operation_until=NULL,
   idle_since=NULL,retire_after=NULL,last_user_activity_at=clock_timestamp(),updated_at=clock_timestamp()
 WHERE session_id=r.session_id RETURNING * INTO w;
 RETURN to_jsonb(w);
END $$;

CREATE FUNCTION public.agent_run_workspace_transition(p_run uuid,p_execution uuid,p_fence bigint,
 p_generation uuid,p_version bigint,p_state text,p_resource jsonb DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE r public.agent_runs%ROWTYPE; w public.agent_session_workspaces%ROWTYPE;
BEGIN
 r:=public.agent_run_assert(p_run,p_execution,p_fence);
 SELECT * INTO STRICT w FROM public.agent_session_workspaces WHERE session_id=r.session_id FOR UPDATE;
 IF w.generation<>p_generation OR w.execution_id<>p_execution OR w.fence<>p_fence THEN
   RAISE EXCEPTION 'agent_workspace_fenced'; END IF;
 IF w.version=p_version+1 AND w.state=p_state AND (p_resource IS NULL OR w.resource=p_resource) THEN RETURN to_jsonb(w); END IF;
 IF w.version<>p_version OR NOT (
   (w.state IN ('allocating','resuming') AND p_state='running') OR
   (w.state='running' AND p_state='pausing') OR (w.state='pausing' AND p_state='paused') OR
   (w.state IN ('allocating','resuming','running','pausing','paused','retained') AND p_state IN ('retained','cleanup_pending')) OR
   (w.state='cleanup_pending' AND p_state='retired') OR
   (w.state='allocating' AND p_state='allocating' AND w.resource IS NULL)
 ) THEN RAISE EXCEPTION 'agent_workspace_transition_invalid'; END IF;
 IF p_state IN ('pausing','paused') AND coalesce(r.publication->>'status','') NOT IN ('committed','no_changes') THEN
   RAISE EXCEPTION 'agent_workspace_publication_unconfirmed'; END IF;
 IF p_state='running' AND coalesce(p_resource,w.resource) IS NULL THEN RAISE EXCEPTION 'agent_workspace_resource_missing'; END IF;
 UPDATE public.agent_session_workspaces SET state=p_state,resource=coalesce(p_resource,resource),
   version=version+1,operation_id=gen_random_uuid(),
   operation_until=CASE WHEN p_state='cleanup_pending' THEN clock_timestamp()+interval '2 minutes' ELSE NULL END,
   updated_at=clock_timestamp()
 WHERE session_id=r.session_id RETURNING * INTO w;
 RETURN to_jsonb(w);
END $$;

CREATE FUNCTION public.agent_run_workspace_due(p_limit integer DEFAULT 20)
RETURNS jsonb LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE result jsonb;
BEGIN
 IF p_limit NOT BETWEEN 1 AND 100 THEN RAISE EXCEPTION 'agent_workspace_batch_invalid'; END IF;
 WITH due AS (
   SELECT w.session_id FROM public.agent_session_workspaces w
   WHERE (w.state='paused' AND w.retire_after<=clock_timestamp()
     AND NOT EXISTS(SELECT 1 FROM public.agent_runs r WHERE r.session_id=w.session_id
      AND r.state IN ('queued','running','waiting_approval','publishing')))
     OR (w.state='cleanup_pending' AND w.operation_until<=clock_timestamp())
   ORDER BY coalesce(w.retire_after,w.operation_until) LIMIT p_limit FOR UPDATE OF w SKIP LOCKED
 ), claimed AS (
   UPDATE public.agent_session_workspaces w SET state='cleanup_pending',version=version+1,
      operation_id=gen_random_uuid(),operation_until=clock_timestamp()+interval '2 minutes',updated_at=clock_timestamp()
   FROM due WHERE w.session_id=due.session_id RETURNING w.*
 ) SELECT coalesce(jsonb_agg(to_jsonb(claimed)),'[]'::jsonb) INTO result FROM claimed;
 RETURN result;
END $$;

CREATE FUNCTION public.agent_run_workspace_retired(p_session text,p_generation uuid,p_operation uuid)
RETURNS boolean LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 UPDATE public.agent_session_workspaces SET state='retired',version=version+1,operation_until=NULL,
   retire_after=NULL,updated_at=clock_timestamp()
 WHERE session_id=p_session AND generation=p_generation AND operation_id=p_operation AND state='cleanup_pending';
 RETURN FOUND;
END $$;

CREATE FUNCTION public.agent_run_workspace_recovered(p_run uuid,p_execution uuid,p_fence bigint)
RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE r public.agent_runs%ROWTYPE;
BEGIN
 r:=public.agent_run_assert(p_run,p_execution,p_fence);
 IF EXISTS(SELECT 1 FROM public.agent_run_executions WHERE run_id=p_run AND resource IS NOT NULL AND NOT cleaned) THEN
   RAISE EXCEPTION 'agent_workspace_cleanup_unconfirmed'; END IF;
 UPDATE public.agent_session_workspaces SET state='retired',version=version+1,retire_after=NULL,
   operation_until=NULL,updated_at=clock_timestamp()
 WHERE session_id=r.session_id AND run_id=p_run AND fence<=p_fence;
END $$;

CREATE OR REPLACE FUNCTION public.agent_run_finish(p_run uuid,p_execution uuid,p_fence bigint,
 p_state text,p_code text,p_snapshot jsonb,p_publication jsonb,p_cleaned boolean)
RETURNS jsonb LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE r public.agent_runs%ROWTYPE; result jsonb;
BEGIN
 r:=public.agent_run_assert(p_run,p_execution,p_fence);
 IF coalesce(p_publication,'null'::jsonb) IS DISTINCT FROM coalesce(r.publication,'null'::jsonb) THEN
   PERFORM public.agent_run_write(p_run,p_execution,p_fence,'publication',p_publication,
     jsonb_build_object('publication',p_publication));
 END IF;
 result:=public.agent_run_write(p_run,p_execution,p_fence,'terminal',
   jsonb_build_object('state',p_state,'code',p_code,'resource_retained',coalesce((p_snapshot->>'resource_retained')::boolean,false)),
   jsonb_build_object('state',p_state,'snapshot',p_snapshot) ||
     CASE WHEN p_cleaned THEN '{"cleaned":true}'::jsonb ELSE '{}'::jsonb END);
 UPDATE public.agent_session_workspaces SET idle_since=coalesce(idle_since,clock_timestamp()),
   retire_after=coalesce(retire_after,clock_timestamp()+interval '6 hours'),updated_at=clock_timestamp()
 WHERE session_id=r.session_id AND execution_id=p_execution AND fence=p_fence AND state='paused';
 RETURN result;
END $$;

DO $$ DECLARE f record; BEGIN
 FOR f IN SELECT oid::regprocedure AS signature FROM pg_proc WHERE pronamespace='public'::regnamespace
 AND proname IN ('agent_workspace_delete_guard','agent_workspace_owner_deleted','agent_run_workspace_acquire','agent_run_workspace_transition',
   'agent_run_workspace_due','agent_run_workspace_retired','agent_run_workspace_recovered') LOOP
   EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC,anon,authenticated',f.signature);
   EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO service_role',f.signature);
 END LOOP;
END $$;
NOTIFY pgrst,'reload schema';
COMMIT;
