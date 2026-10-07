BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='2min';

CREATE FUNCTION public.agent_run_publication_fence(p_run uuid,p_execution uuid,p_fence bigint,p_project text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE r public.agent_runs%ROWTYPE; surface public.access_surfaces%ROWTYPE; org text;
BEGIN
    r:=public.agent_run_assert(p_run,p_execution,p_fence);
    IF r.project_id<>p_project OR r.state<>'publishing' OR r.stop_requested OR r.deadline<=clock_timestamp() THEN
        RAISE EXCEPTION 'agent_publication_fenced' USING ERRCODE='55000';
    END IF;
    SELECT * INTO surface FROM public.access_surfaces WHERE id=r.agent_id AND project_id=p_project FOR SHARE;
    IF NOT FOUND OR surface.status<>'active'
       OR surface.updated_at IS DISTINCT FROM (r.policy->>'surface_updated_at')::timestamptz THEN
        RAISE EXCEPTION 'agent_configuration_changed' USING ERRCODE='42501';
    END IF;
    IF surface.scope_id IS NOT NULL THEN
        PERFORM 1 FROM public.repository_scopes WHERE id=surface.scope_id AND project_id=p_project
            AND updated_at=(r.policy->>'scope_updated_at')::timestamptz FOR SHARE;
        IF NOT FOUND THEN RAISE EXCEPTION 'agent_scope_changed' USING ERRCODE='42501'; END IF;
    END IF;
    SELECT org_id INTO org FROM public.projects WHERE id=p_project;
    PERFORM public._version_assert_current_repository_actor(p_project,org,'user:'||r.user_id,true);
END $$;

-- Keep the existing Version Engine's final admission point. Agent publication
-- leases additionally bind the run execution/fence, in the same transaction.
CREATE OR REPLACE FUNCTION public._version_assert_write_lease(p_project_id text,p_lease_id uuid,p_holder_id text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE lease public.project_write_leases%ROWTYPE; parts text[];
BEGIN
    SELECT * INTO lease FROM public.project_write_leases WHERE id=p_lease_id FOR SHARE;
    IF NOT FOUND OR lease.project_id IS DISTINCT FROM p_project_id
       OR lease.holder_id IS DISTINCT FROM p_holder_id OR lease.expires_at<=clock_timestamp() THEN
        RAISE EXCEPTION 'repository_write_lease_unavailable' USING ERRCODE='55000';
    END IF;
    IF p_holder_id LIKE 'cloud-agent:%' THEN
        parts:=string_to_array(p_holder_id,':');
        IF array_length(parts,1)<>4 THEN RAISE EXCEPTION 'agent_publication_fenced'; END IF;
        PERFORM public.agent_run_publication_fence(parts[2]::uuid,parts[3]::uuid,parts[4]::bigint,p_project_id);
    END IF;
END $$;

CREATE FUNCTION public.agent_run_legacy_publication_receipt()
RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE parts text[];
BEGIN
    IF NEW.actor LIKE 'cloud-agent:%' THEN
        parts:=string_to_array(NEW.actor,':');
        PERFORM public.agent_run_publication_fence(parts[2]::uuid,parts[3]::uuid,parts[4]::bigint,NEW.project_id);
        IF NEW.status='committed' THEN
            UPDATE public.agent_runs SET publication=jsonb_build_object('status','committed',
                'commit_id',NEW.committed_commit_id,'transaction_id',NEW.id,'source','legacy') WHERE id=parts[2]::uuid;
        ELSIF NEW.status IN ('pending_manual_review','pending_agent_resolution','retryable_conflict','rejected') THEN
            UPDATE public.agent_runs SET publication=jsonb_build_object(
                'status',CASE WHEN NEW.status='rejected' THEN 'failed' ELSE 'conflict' END,
                'transaction_id',NEW.id,'source','legacy') WHERE id=parts[2]::uuid;
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER agent_run_legacy_receipt BEFORE INSERT OR UPDATE ON public.version_transactions
    FOR EACH ROW EXECUTE FUNCTION public.agent_run_legacy_publication_receipt();

CREATE FUNCTION public.agent_run_native_publication_receipt()
RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE r public.agent_runs%ROWTYPE;
BEGIN
    SELECT * INTO r FROM public.agent_runs WHERE id=NEW.request_key
        AND project_id=NEW.project_id AND 'user:'||user_id=NEW.actor AND state='publishing';
    IF FOUND AND EXISTS(SELECT 1 FROM public.project_write_leases
        WHERE project_id=NEW.project_id AND expires_at>clock_timestamp()
        AND holder_id='cloud-agent:'||r.id||':'||r.execution_id||':'||r.fence) THEN
        PERFORM public.agent_run_publication_fence(r.id,r.execution_id,r.fence,r.project_id);
        UPDATE public.agent_runs SET publication=NEW.result||jsonb_build_object('source','native','transaction_id',NEW.id)
            WHERE id=r.id;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER agent_run_native_receipt AFTER INSERT ON public.version_ref_transactions
    FOR EACH ROW EXECUTE FUNCTION public.agent_run_native_publication_receipt();

REVOKE ALL ON FUNCTION public.agent_run_publication_fence(uuid,uuid,bigint,text),
    public.agent_run_legacy_publication_receipt(),public.agent_run_native_publication_receipt() FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.agent_run_publication_fence(uuid,uuid,bigint,text),
    public.agent_run_legacy_publication_receipt(),public.agent_run_native_publication_receipt() TO service_role;
COMMIT;
