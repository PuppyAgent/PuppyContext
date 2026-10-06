-- Reuse the durable Project deletion job. Retain physical capacity until the
-- existing worker has completed its purge and quiet verification window.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='2min';

CREATE OR REPLACE FUNCTION public.settle_version_object_capacity_io(
    p_project_id text,p_actor text,p_pin_id uuid,p_io_id uuid
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE settled bigint;
BEGIN
    -- Completing a specific admitted I/O is legal after deletion closes new
    -- admission. A ready-only lock here would strand every late completion.
    PERFORM 1 FROM public.organizations WHERE id=(SELECT org_id FROM public.projects WHERE id=p_project_id) FOR UPDATE;
    PERFORM 1 FROM public.projects WHERE id=p_project_id FOR UPDATE;
    PERFORM 1 FROM public.version_repositories WHERE project_id=p_project_id FOR UPDATE;
    -- This only settles a specific completed invocation, never authorizes new
    -- I/O. Expiry/release of its pin must not prevent known-complete cleanup.
    PERFORM 1 FROM public.version_object_pins WHERE id=p_pin_id AND project_id=p_project_id
        AND actor=p_actor AND purpose='publication' FOR SHARE;
    IF NOT FOUND OR p_io_id IS NULL THEN
        RAISE EXCEPTION 'publication_pin_unavailable' USING ERRCODE='55000';
    END IF;
    DELETE FROM public.version_repository_capacity_inflight
        WHERE project_id=p_project_id AND pin_id=p_pin_id AND io_id=p_io_id;
    GET DIAGNOSTICS settled = ROW_COUNT;
    RETURN jsonb_build_object('settled_objects',settled);
END $$;

CREATE FUNCTION public._version_track_project_logical_usage()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
    IF NEW.payload ? 'logical_storage' AND NOT OLD.payload ? 'logical_storage' THEN
        UPDATE public.version_repository_billing
        SET accounted_bytes=accounted_bytes+(NEW.payload->'logical_storage'->>'delta_bytes')::bigint
        WHERE project_id=NEW.project_id AND accounted_bytes IS NOT NULL;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER version_project_logical_usage AFTER UPDATE OF payload ON public.version_ref_events
    FOR EACH ROW EXECUTE FUNCTION public._version_track_project_logical_usage();

ALTER FUNCTION public.drain_project_deletion_job(text,text) RENAME TO _drain_project_deletion_before_native;
CREATE FUNCTION public.drain_project_deletion_job(p_job_id text,p_worker_id text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE job public.project_deletion_jobs%ROWTYPE; org text; result jsonb;
    accounted bigint; usage bigint; tracked boolean;
BEGIN
    SELECT * INTO job FROM public.project_deletion_jobs WHERE id=p_job_id;
    IF NOT FOUND THEN RETURN jsonb_build_object('outcome','claim_lost'); END IF;
    SELECT org_id INTO org FROM public.projects WHERE id=job.project_id;
    -- Same lock order as publication: organization billing, Project, repository.
    IF org IS NOT NULL THEN
        PERFORM pg_advisory_xact_lock(hashtextextended(org,0));
        PERFORM pg_advisory_xact_lock(hashtextextended(org || ':storage.logical_bytes',0));
        PERFORM 1 FROM public.organizations WHERE id=org FOR UPDATE;
        PERFORM 1 FROM public.projects WHERE id=job.project_id FOR UPDATE;
        PERFORM 1 FROM public.version_repositories WHERE project_id=job.project_id FOR UPDATE;
    END IF;
    SELECT * INTO job FROM public.project_deletion_jobs WHERE id=p_job_id FOR UPDATE;
    IF job.status <> 'running' OR job.phase <> 'drain' OR job.claimed_by IS DISTINCT FROM p_worker_id THEN
        RETURN jsonb_build_object('outcome','claim_lost');
    END IF;
    IF EXISTS(SELECT 1 FROM public.version_repository_capacity_inflight WHERE project_id=job.project_id)
       OR EXISTS(SELECT 1 FROM public.version_repositories WHERE project_id=job.project_id AND gc_token IS NOT NULL) THEN
        UPDATE public.project_deletion_jobs SET status='pending',available_at=now()+interval '60 seconds',
            claimed_at=NULL,claimed_by=NULL,updated_at=now() WHERE id=p_job_id;
        RETURN jsonb_build_object('outcome','waiting','reason','native_storage_io_unsettled');
    END IF;
    SELECT accounted_bytes INTO accounted FROM public.version_repository_billing WHERE project_id=job.project_id;
    tracked:=FOUND;
    IF tracked THEN
        SELECT value INTO usage FROM public.organization_usage_counters
            WHERE org_id=org AND metric='storage.logical_bytes' AND version>0 FOR UPDATE;
        IF accounted IS NULL OR usage IS NULL OR usage<accounted THEN
            RAISE EXCEPTION 'storage_billing_deletion_baseline_unavailable' USING ERRCODE='55000';
        END IF;
    END IF;
    result:=public._drain_project_deletion_before_native(p_job_id,p_worker_id);
    IF result->>'outcome'='drained' AND tracked THEN
        PERFORM public.reconcile_organization_usage_counter(org,'storage.logical_bytes',usage-accounted,NULL,
            'storage-delete:'||p_job_id,'version_engine',jsonb_build_object(
                'project_id',job.project_id,'delta_bytes',-accounted,'deletion_job_id',p_job_id));
    END IF;
    RETURN result;
END $$;

ALTER FUNCTION public.complete_project_deletion_job(text,text) RENAME TO _complete_project_deletion_before_native;
CREATE FUNCTION public.complete_project_deletion_job(p_job_id text,p_worker_id text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE job public.project_deletion_jobs%ROWTYPE; capacity public.version_repository_capacity%ROWTYPE; done boolean;
BEGIN
    SELECT * INTO job FROM public.project_deletion_jobs WHERE id=p_job_id FOR UPDATE;
    IF NOT FOUND OR job.phase <> 'verify' OR job.status <> 'running'
       OR job.claimed_by IS DISTINCT FROM p_worker_id THEN RETURN NULL; END IF;
    IF EXISTS(SELECT 1 FROM public.projects WHERE id=job.project_id)
       OR EXISTS(SELECT 1 FROM public.version_repository_capacity_inflight WHERE project_id=job.project_id) THEN
        RETURN false;
    END IF;
    SELECT * INTO capacity FROM public.version_repository_capacity WHERE project_id=job.project_id;
    IF FOUND THEN
        -- Physical accounting is retained through every failed/uncertain purge.
        PERFORM 1 FROM public.version_organization_capacity WHERE org_id=capacity.org_id FOR UPDATE;
        PERFORM 1 FROM public.version_repository_capacity WHERE project_id=job.project_id FOR UPDATE;
        UPDATE public.version_organization_capacity SET used_body_bytes=used_body_bytes-capacity.used_body_bytes,
            used_objects=used_objects-capacity.used_objects WHERE org_id=capacity.org_id;
        DELETE FROM public.version_repository_capacity WHERE project_id=job.project_id;
    END IF;
    done:=public._complete_project_deletion_before_native(p_job_id,p_worker_id);
    IF NOT coalesce(done,false) THEN RAISE EXCEPTION 'deletion_completion_claim_lost'; END IF;
    RETURN true;
END $$;

REVOKE ALL ON FUNCTION public._version_track_project_logical_usage(),
    public._drain_project_deletion_before_native(text,text),public._complete_project_deletion_before_native(text,text),
    public.drain_project_deletion_job(text,text),public.complete_project_deletion_job(text,text)
    FROM PUBLIC,anon,authenticated,service_role;
GRANT EXECUTE ON FUNCTION public.drain_project_deletion_job(text,text),public.complete_project_deletion_job(text,text) TO service_role;
NOTIFY pgrst,'reload schema';
COMMIT;
