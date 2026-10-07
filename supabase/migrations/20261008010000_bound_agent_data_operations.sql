-- Named backend-only Agent operations. No historical migrations are modified.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '2min';

-- Facts, not a second role policy. Canonical Python AuthorizationService owns
-- interpretation. STABLE functions share the caller statement's MVCC snapshot.
CREATE FUNCTION public.authorization_project_facts(p_project text,p_user uuid)
RETURNS jsonb LANGUAGE sql STABLE SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT jsonb_build_object('project_id',p.id,'org_id',p.org_id,'visibility',p.visibility,
   'org_role',o.role,'project_role',m.role,'project_member_org_id',m.org_id)
 FROM public.projects p
 LEFT JOIN public.org_members o ON o.org_id=p.org_id AND o.user_id=p_user
 LEFT JOIN public.project_members m ON m.project_id=p.id AND m.user_id=p_user
 WHERE p.id=p_project AND p.lifecycle_status='ready'
$$;

-- Revision locks serialize command admission with changes, including insertion
-- of previously absent membership rows. No cache TTL or timestamp precision gap.
CREATE TABLE public.authorization_revisions (
    key text PRIMARY KEY, revision bigint NOT NULL DEFAULT 0
);
INSERT INTO public.authorization_revisions(key)
 SELECT 'project:'||id FROM public.projects UNION ALL SELECT 'org:'||id FROM public.organizations;
ALTER TABLE public.authorization_revisions ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.authorization_revisions FROM PUBLIC,anon,authenticated;
GRANT ALL ON public.authorization_revisions TO service_role;

CREATE FUNCTION public.authorization_touch_revision() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE before_row jsonb; after_row jsonb; targets text[] := '{}'; item text;
BEGIN
 IF TG_OP<>'INSERT' THEN before_row:=to_jsonb(OLD); END IF;
 IF TG_OP<>'DELETE' THEN after_row:=to_jsonb(NEW); END IF;
 IF TG_OP='UPDATE' AND before_row=after_row THEN RETURN NEW; END IF;
 IF TG_TABLE_NAME='projects' THEN
   IF TG_OP='UPDATE' AND (before_row->'visibility',before_row->'lifecycle_status',before_row->'org_id')
       IS NOT DISTINCT FROM (after_row->'visibility',after_row->'lifecycle_status',after_row->'org_id') THEN RETURN NEW; END IF;
   targets:=ARRAY['project:'||(coalesce(after_row,before_row)->>'id')];
 ELSIF TG_TABLE_NAME='organizations' THEN
   targets:=ARRAY['org:'||(coalesce(after_row,before_row)->>'id')];
 ELSIF TG_TABLE_NAME='org_members' THEN
   targets:=ARRAY['org:'||(before_row->>'org_id'),'org:'||(after_row->>'org_id')];
 ELSIF TG_TABLE_NAME='access_tools' THEN
   SELECT array_agg('project:'||project_id) INTO targets FROM public.access_surfaces
     WHERE id IN (before_row->>'access_surface_id',after_row->>'access_surface_id');
 ELSE
   targets:=ARRAY['project:'||(before_row->>'project_id'),'project:'||(after_row->>'project_id')];
 END IF;
 FOR item IN SELECT DISTINCT value FROM unnest(targets) value WHERE value IS NOT NULL ORDER BY value LOOP
   INSERT INTO public.authorization_revisions(key,revision) VALUES(item,1)
     ON CONFLICT(key) DO UPDATE SET revision=authorization_revisions.revision+1;
 END LOOP;
 RETURN coalesce(NEW,OLD);
END $$;
CREATE TRIGGER authorization_project_revision AFTER INSERT OR UPDATE OF visibility,lifecycle_status,org_id ON public.projects
 FOR EACH ROW EXECUTE FUNCTION public.authorization_touch_revision();
CREATE TRIGGER authorization_org_revision AFTER INSERT ON public.organizations
 FOR EACH ROW EXECUTE FUNCTION public.authorization_touch_revision();
CREATE TRIGGER authorization_org_member_revision AFTER INSERT OR UPDATE OR DELETE ON public.org_members
 FOR EACH ROW EXECUTE FUNCTION public.authorization_touch_revision();
CREATE TRIGGER authorization_project_member_revision AFTER INSERT OR UPDATE OR DELETE ON public.project_members
 FOR EACH ROW EXECUTE FUNCTION public.authorization_touch_revision();
CREATE TRIGGER authorization_surface_revision AFTER INSERT OR UPDATE OR DELETE ON public.access_surfaces
 FOR EACH ROW EXECUTE FUNCTION public.authorization_touch_revision();
CREATE TRIGGER authorization_binding_revision AFTER INSERT OR UPDATE OR DELETE ON public.access_tools
 FOR EACH ROW EXECUTE FUNCTION public.authorization_touch_revision();
CREATE TRIGGER authorization_tool_revision AFTER INSERT OR UPDATE OR DELETE ON public.tools
 FOR EACH ROW EXECUTE FUNCTION public.authorization_touch_revision();

CREATE FUNCTION public.authorization_revision_token(p_project text)
RETURNS jsonb LANGUAGE sql STABLE SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT jsonb_build_object('project',p.id,'org',p.org_id,'project_revision',r.revision,'org_revision',o.revision)
 FROM public.projects p JOIN public.authorization_revisions r ON r.key='project:'||p.id
 JOIN public.authorization_revisions o ON o.key='org:'||p.org_id WHERE p.id=p_project
$$;
CREATE FUNCTION public.authorization_assert_revision(p_project text,p_token jsonb)
RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF p_token IS NULL OR p_token->>'project' IS DISTINCT FROM p_project THEN
   RAISE EXCEPTION 'agent_context_changed' USING ERRCODE='P0001';
 END IF;
 PERFORM 1 FROM public.authorization_revisions
   WHERE key IN ('project:'||p_project,'org:'||(p_token->>'org')) ORDER BY key FOR SHARE;
 IF public.authorization_revision_token(p_project) IS DISTINCT FROM p_token THEN
   RAISE EXCEPTION 'agent_context_changed' USING ERRCODE='P0001';
 END IF;
END $$;

CREATE FUNCTION public.agent_run_context(p_user uuid,p_project text,p_agent text DEFAULT NULL,p_scope text DEFAULT NULL,p_request uuid DEFAULT NULL)
RETURNS jsonb LANGUAGE sql STABLE SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT jsonb_build_object('facts',public.authorization_project_facts(p_project,p_user),
   'revision',public.authorization_revision_token(p_project),'surface',to_jsonb(a),
   'tools',coalesce((SELECT jsonb_agg(jsonb_build_object('tool_id',b.tool_id,'enabled',b.enabled,
      'definition',to_jsonb(t)) ORDER BY b.tool_id) FROM public.access_tools b
      JOIN public.tools t ON t.id=b.tool_id WHERE b.access_surface_id=a.id),'[]'::jsonb),
   'readiness',public.get_native_project_readiness(p_project),
   'receipt',(SELECT to_jsonb(r) FROM public.agent_runs r WHERE r.user_id=p_user AND r.project_id=p_project AND r.request_id=p_request))
 FROM (SELECT 1) seed LEFT JOIN LATERAL (
   SELECT * FROM public.access_surfaces s WHERE s.project_id=p_project AND s.kind='agent'
     AND (s.id=coalesce(p_agent,(SELECT r.agent_id FROM public.agent_runs r WHERE r.user_id=p_user AND r.project_id=p_project AND r.request_id=p_request),s.id))
     AND (p_agent IS NOT NULL OR s.scope_id IS NOT DISTINCT FROM p_scope)
     AND (coalesce(s.config->>'visibility','org')<>'private' OR s.created_by=p_user)
   ORDER BY s.created_at,s.id LIMIT 1
 ) a ON true
$$;

CREATE FUNCTION public.agent_run_submit_context(p_user uuid,p_project text,p_agent text,p_session text,
 p_request uuid,p_digest text,p_prompt text,p_policy jsonb,p_timeout integer)
RETURNS jsonb LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 PERFORM public.authorization_assert_revision(p_project,p_policy->'revision');
 RETURN public.agent_run_submit(p_user,p_project,p_agent,p_session,p_request,p_digest,p_prompt,p_policy,p_timeout);
END $$;


CREATE FUNCTION public.agent_run_builtin_context(p_project text,p_scope text,p_user uuid,p_config jsonb,p_revision jsonb)
RETURNS text LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended('agent-builtin:'||p_project||':'||coalesce(p_scope,''),0));
 PERFORM public.authorization_assert_revision(p_project,p_revision);
 RETURN public.agent_run_builtin(p_project,p_scope,p_user,p_config);
END $$;

CREATE FUNCTION public.agent_run_execution_view(p_run uuid)
RETURNS jsonb LANGUAGE sql STABLE SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT jsonb_build_object('run',to_jsonb(r),
   'context',public.agent_run_context(r.user_id,r.project_id,r.agent_id),
   'tools',coalesce((SELECT jsonb_agg(t ORDER BY t.updated_at) FROM public.agent_run_tools t WHERE t.run_id=r.id),'[]'::jsonb),
   'executions',coalesce((SELECT jsonb_agg(e ORDER BY e.fence) FROM public.agent_run_executions e WHERE e.run_id=r.id AND NOT e.cleaned),'[]'::jsonb),
   'previous',(SELECT to_jsonb(p) FROM public.agent_runs p WHERE p.session_id=r.session_id AND p.created_at<r.created_at ORDER BY p.created_at DESC LIMIT 1))
 FROM public.agent_runs r WHERE r.id=p_run
$$;
CREATE FUNCTION public.agent_run_claim_execution(p_worker text)
RETURNS jsonb LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE r jsonb;
BEGIN
 r:=public.agent_run_claim(p_worker);
 IF r IS NULL THEN RETURN NULL; END IF;
 RETURN public.agent_run_execution_view((r->>'id')::uuid);
END $$;

CREATE FUNCTION public.agent_run_renew(p_run uuid,p_execution uuid,p_fence bigint)
RETURNS jsonb LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE r public.agent_runs%ROWTYPE; code text;
BEGIN
 r:=public.agent_run_assert(p_run,p_execution,p_fence);
 IF r.stop_requested THEN code:='stop_requested';
 ELSIF r.deadline<=clock_timestamp() THEN code:='runtime_timeout';
 ELSIF public.authorization_revision_token(r.project_id) IS DISTINCT FROM r.policy->'revision' THEN code:='authorization_revoked';
 END IF;
 -- Keep cleanup ownership alive on a rejection. This never authorizes more work.
 UPDATE public.agent_runs SET lease_until=clock_timestamp()+interval '45 seconds' WHERE id=p_run;
 RETURN jsonb_build_object('code',code,'billing_run_id',r.billing_run_id,'state',r.state);
END $$;

CREATE FUNCTION public.agent_run_view(p_user uuid,p_run uuid,p_after bigint DEFAULT 0)
RETURNS jsonb LANGUAGE sql STABLE SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT jsonb_build_object('run',to_jsonb(r),'facts',public.authorization_project_facts(r.project_id,p_user),
   'surface',to_jsonb(a),'revision',public.authorization_revision_token(r.project_id),
   'tools',coalesce((SELECT jsonb_agg(t ORDER BY t.updated_at) FROM public.agent_run_tools t WHERE t.run_id=r.id),'[]'::jsonb),
   'events',coalesce((SELECT jsonb_agg(e ORDER BY e.sequence) FROM
      (SELECT * FROM public.agent_run_events WHERE run_id=r.id AND sequence>p_after AND sequence<=r.sequence ORDER BY sequence LIMIT 64) e),'[]'::jsonb))
 FROM public.agent_runs r JOIN public.access_surfaces a ON a.id=r.agent_id
 WHERE r.id=p_run AND r.user_id=p_user
$$;



CREATE FUNCTION public.agent_run_receipt_view(p_user uuid,p_project text,p_request uuid)
RETURNS jsonb LANGUAGE sql STABLE SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT public.agent_run_view(p_user,r.id) FROM public.agent_runs r
 WHERE r.user_id=p_user AND r.project_id=p_project AND r.request_id=p_request
$$;
CREATE FUNCTION public.agent_run_session_view(p_user uuid,p_session text,p_limit integer DEFAULT 50,p_before timestamptz DEFAULT NULL)
RETURNS jsonb LANGUAGE sql STABLE SET search_path=pg_catalog,public,pg_temp AS $$
 WITH rows AS (SELECT * FROM public.agent_runs r WHERE r.user_id=p_user AND r.session_id=p_session
   AND (p_before IS NULL OR r.created_at<p_before) ORDER BY r.created_at DESC LIMIT least(greatest(p_limit,1),100)),
 first AS (SELECT * FROM rows ORDER BY created_at DESC LIMIT 1)
 SELECT jsonb_build_object('runs',(SELECT jsonb_agg(r ORDER BY r.created_at DESC) FROM rows r),
   'facts',public.authorization_project_facts(f.project_id,p_user),'surface',to_jsonb(a))
 FROM first f JOIN public.access_surfaces a ON a.id=f.agent_id
$$;

CREATE FUNCTION public.agent_run_command_context(p_run uuid,p_user uuid,p_command text,p_revision jsonb,
 p_call text DEFAULT NULL,p_decision uuid DEFAULT NULL,p_allow boolean DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE r public.agent_runs%ROWTYPE;
BEGIN
 SELECT * INTO r FROM public.agent_runs WHERE id=p_run AND user_id=p_user FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'agent_run_not_found' USING ERRCODE='42501'; END IF;
 PERFORM public.authorization_assert_revision(r.project_id,p_revision);
 IF p_command='approve' AND r.policy->'revision' IS DISTINCT FROM p_revision THEN
   RAISE EXCEPTION 'agent_context_changed' USING ERRCODE='P0001';
 END IF;
 RETURN public.agent_run_command(p_run,p_user,p_command,p_call,p_decision,p_allow);
END $$;

CREATE FUNCTION public.agent_run_history_view(p_user uuid,p_project text,p_agent text,p_limit integer DEFAULT 50)
RETURNS jsonb LANGUAGE sql STABLE SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT jsonb_build_object('facts',public.authorization_project_facts(p_project,p_user),
   'surface',to_jsonb(a),'sessions',coalesce((SELECT jsonb_agg(s ORDER BY s.updated_at DESC,s.id) FROM
     (SELECT id,agent_id,title,mode,created_at,updated_at FROM public.chat_sessions
      WHERE user_id=p_user AND agent_id=p_agent AND mode='cloud_pi'
      ORDER BY updated_at DESC,id LIMIT least(greatest(p_limit,1),200)) s),'[]'::jsonb))
 FROM public.access_surfaces a WHERE a.id=p_agent AND a.project_id=p_project AND a.kind='agent'
$$;

CREATE TABLE public.agent_run_event_batches (
 run_id uuid NOT NULL REFERENCES public.agent_runs(id) ON DELETE CASCADE,
 execution_id uuid NOT NULL, batch_id uuid NOT NULL, input_digest text NOT NULL,
 sequence bigint NOT NULL, PRIMARY KEY(run_id,execution_id,batch_id)
);
ALTER TABLE public.agent_run_event_batches ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.agent_run_event_batches FROM PUBLIC,anon,authenticated;
GRANT ALL ON public.agent_run_event_batches TO service_role;

CREATE FUNCTION public.agent_run_append_batch(p_run uuid,p_execution uuid,p_fence bigint,p_batch uuid,p_events jsonb)
RETURNS jsonb LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE r jsonb; e jsonb; old_digest text;
BEGIN
 IF jsonb_typeof(p_events)<>'array' OR jsonb_array_length(p_events) NOT BETWEEN 1 AND 64
   OR octet_length(p_events::text)>2097152 THEN RAISE EXCEPTION 'agent_batch_invalid'; END IF;
 r:=to_jsonb(public.agent_run_assert(p_run,p_execution,p_fence));
 SELECT input_digest INTO old_digest FROM public.agent_run_event_batches
   WHERE run_id=p_run AND execution_id=p_execution AND batch_id=p_batch;
 IF FOUND THEN
   IF old_digest<>md5(p_events::text) THEN RAISE EXCEPTION 'agent_batch_identity_conflict'; END IF;
   RETURN r;
 END IF;
 FOR e IN SELECT value FROM jsonb_array_elements(p_events) LOOP
   r:=public.agent_run_write(p_run,p_execution,p_fence,e->>'kind',coalesce(e->'payload','{}'),coalesce(e->'patch','{}'));
 END LOOP;
 INSERT INTO public.agent_run_event_batches VALUES(p_run,p_execution,p_batch,md5(p_events::text),(r->>'sequence')::bigint);
 -- Receipt lifetime is the execution lifetime. Recovery gets a new execution
 -- and never retries the previous owner's unconfirmed batch IDs.
 DELETE FROM public.agent_run_event_batches WHERE run_id=p_run AND execution_id<>p_execution;
 RETURN r;
END $$;

-- Reuse the existing final Version Engine fence, adding configuration/membership
-- revision serialization; the original canonical actor policy remains in force.
ALTER FUNCTION public.agent_run_publication_fence(uuid,uuid,bigint,text) RENAME TO agent_run_publication_fence_base;
CREATE FUNCTION public.agent_run_publication_fence(p_run uuid,p_execution uuid,p_fence bigint,p_project text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE r public.agent_runs%ROWTYPE;
BEGIN
 r:=public.agent_run_assert(p_run,p_execution,p_fence);
 -- Source rows precede revision rows, matching UPDATE/DELETE triggers. Taking
 -- the revision lock first would invert the surface/member lock order during
 -- a concurrent revocation and invite a deadlock.
 PERFORM public.agent_run_publication_fence_base(p_run,p_execution,p_fence,p_project);
 PERFORM public.authorization_assert_revision(p_project,r.policy->'revision');
END $$;

DO $$ DECLARE f record; BEGIN
 FOR f IN SELECT oid::regprocedure AS signature FROM pg_proc WHERE pronamespace='public'::regnamespace
   AND (proname LIKE 'agent_run_%' OR proname LIKE 'authorization_%') LOOP
   EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC,anon,authenticated',f.signature);
   EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO service_role',f.signature);
 END LOOP;
END $$;

-- One bounded metadata snapshot under an admitted native read pin. The pin
-- prevents the next GC sweep while immutable physical objects are being read.
CREATE FUNCTION public.get_version_pinned_object_locations(p_project_id text,p_actor text,p_pin_id uuid)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE result jsonb;
BEGIN
 IF NOT EXISTS(SELECT 1 FROM public.version_object_pins pin
   JOIN public.version_repositories repo USING(project_id)
   WHERE pin.id=p_pin_id AND pin.project_id=p_project_id AND pin.actor=p_actor
     AND pin.purpose='read' AND pin.state<>'released' AND pin.expires_at>statement_timestamp()
     AND pin.generation=repo.generation AND pin.gc_epoch=repo.gc_epoch) THEN
   RAISE EXCEPTION 'repository_read_pin_unavailable';
 END IF;
 SELECT coalesce(jsonb_agg(row),'[]'::jsonb) INTO result FROM (
   SELECT object_id,pack_key,offset_bytes,size_bytes FROM public.version_object_locations
    WHERE project_id=p_project_id ORDER BY object_id LIMIT 50001
 ) row;
 IF jsonb_array_length(result)>50000 THEN RAISE EXCEPTION 'repository_location_snapshot_limit'; END IF;
 RETURN result;
END $$;
REVOKE ALL ON FUNCTION public.get_version_pinned_object_locations(text,text,uuid) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.get_version_pinned_object_locations(text,text,uuid) TO service_role;

NOTIFY pgrst,'reload schema';
COMMIT;
