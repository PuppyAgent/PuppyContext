-- ISSUE-062: checked, paged Organization inventory and logical reconciliation.
-- Expand only: no enrollment, backfill, external I/O or repository activation.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='2min';

CREATE TABLE public.version_storage_reconciliations (
    id uuid PRIMARY KEY,
    org_id text NOT NULL REFERENCES public.organizations(id) ON DELETE CASCADE,
    project_count bigint NOT NULL DEFAULT 0 CHECK(project_count>=0),
    expires_at timestamptz NOT NULL DEFAULT clock_timestamp()+interval '1 hour',
    transaction_xid xid8,
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX version_storage_reconciliations_org_idx ON public.version_storage_reconciliations(org_id);
CREATE INDEX version_storage_reconciliations_open_idx ON public.version_storage_reconciliations(org_id,expires_at)
    WHERE result IS NULL;
CREATE TABLE public.version_storage_reconciliation_projects (
    reconciliation_id uuid NOT NULL REFERENCES public.version_storage_reconciliations(id) ON DELETE CASCADE,
    project_id text NOT NULL,
    state jsonb NOT NULL,
    logical_bytes bigint CHECK(logical_bytes>=0),
    PRIMARY KEY(reconciliation_id,project_id)
);
ALTER TABLE public.version_storage_reconciliations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.version_storage_reconciliation_projects ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.version_storage_reconciliations,public.version_storage_reconciliation_projects
    FROM PUBLIC,anon,authenticated,service_role;
GRANT SELECT ON public.version_storage_reconciliations,public.version_storage_reconciliation_projects TO service_role;

CREATE FUNCTION public._version_lock_storage_organization(p_org_id text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(p_org_id,0));
    PERFORM pg_advisory_xact_lock(hashtextextended(p_org_id||':storage.logical_bytes',0));
    PERFORM 1 FROM public.organizations WHERE id=p_org_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'storage_organization_unavailable' USING ERRCODE='55000'; END IF;
    -- Organization FK key-share locks serialize newly inserted/moved Projects.
END $$;

CREATE FUNCTION public._version_assert_storage_inventory(p_org_id text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
    IF (SELECT count(*) FROM (SELECT 1 FROM public.projects WHERE org_id=p_org_id LIMIT 1000001) p)>1000000 THEN
        RAISE EXCEPTION 'storage_inventory_project_budget_exceeded';
    END IF;
    PERFORM 1 FROM public.projects WHERE org_id=p_org_id ORDER BY id FOR UPDATE;
    PERFORM 1 FROM public.version_repositories r JOIN public.projects p ON p.id=r.project_id
        WHERE p.org_id=p_org_id ORDER BY r.project_id FOR UPDATE OF r;
    IF EXISTS(SELECT 1 FROM public.projects WHERE org_id=p_org_id AND lifecycle_status<>'ready') THEN
        RAISE EXCEPTION 'storage_project_unavailable' USING ERRCODE='55000';
    END IF;
    IF EXISTS(SELECT 1 FROM public.version_repositories r JOIN public.projects p ON p.id=r.project_id
        LEFT JOIN public.version_repository_billing b ON b.project_id=r.project_id
        WHERE p.org_id=p_org_id AND r.authority='native'
          AND (b.initialized IS DISTINCT FROM true OR b.org_id IS DISTINCT FROM p_org_id)) THEN
        RAISE EXCEPTION 'repository_billing_uninitialized' USING ERRCODE='55000';
    END IF;
    IF EXISTS(SELECT 1 FROM public.projects p JOIN public.version_repositories r ON r.project_id=p.id
        WHERE p.org_id=p_org_id AND r.authority='native') AND NOT EXISTS(
        SELECT 1 FROM public.organization_usage_counters WHERE org_id=p_org_id AND metric='storage.logical_bytes' AND version>0) THEN
        RAISE EXCEPTION 'storage_billing_usage_uninitialized' USING ERRCODE='55000';
    END IF;
END $$;

CREATE FUNCTION public._version_storage_project_states(p_org_id text)
RETURNS TABLE(project_id text,state jsonb) LANGUAGE sql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
    SELECT p.id,jsonb_build_object('authority',coalesce(r.authority,'legacy'),
        'generation',coalesce(r.generation,0),'object_format',CASE WHEN r.authority='native' THEN r.object_format ELSE 'sha1' END,
        'root',CASE WHEN r.authority='native' THEN public._version_default_head_oid(p.id)
                    ELSE nullif(p.version_root_hash,'') END,
        'head',CASE WHEN r.authority='native' THEN (SELECT jsonb_build_object(
            'oid',h.target_oid,'target',replace(encode(h.symbolic_target,'base64'),E'\n',''))
            FROM public.version_repository_refs h WHERE h.project_id=p.id AND h.name=convert_to('HEAD','UTF8')) END)
    FROM public.projects p LEFT JOIN public.version_repositories r ON r.project_id=p.id
    WHERE p.org_id=p_org_id
$$;

CREATE FUNCTION public.begin_version_storage_reconciliation(p_org_id text,p_id uuid)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE job public.version_storage_reconciliations%ROWTYPE; n bigint;
BEGIN
    PERFORM public._version_lock_storage_organization(p_org_id);
    SELECT * INTO job FROM public.version_storage_reconciliations WHERE id=p_id FOR UPDATE;
    IF FOUND THEN
        IF job.org_id IS DISTINCT FROM p_org_id THEN RAISE EXCEPTION 'storage_reconciliation_binding_mismatch'; END IF;
        IF job.result IS NULL AND job.expires_at<=clock_timestamp() THEN RAISE EXCEPTION 'storage_reconciliation_expired'; END IF;
        RETURN jsonb_build_object('id',p_id,'org_id',p_org_id,'project_count',job.project_count,'result',job.result);
    END IF;
    -- Backpressure: a failed/crashed capture cannot create unbounded repeated
    -- inventories. Explicit cancellation/expiry permits bounded metadata cleanup;
    -- a new capture waits until the previous unfinished inventory is drained.
    IF EXISTS(SELECT 1 FROM public.version_storage_reconciliations j WHERE j.org_id=p_org_id
        AND j.result IS NULL AND j.expires_at>clock_timestamp()) OR EXISTS(
        SELECT 1 FROM public.version_storage_reconciliation_projects p JOIN public.version_storage_reconciliations j ON j.id=p.reconciliation_id
        WHERE j.org_id=p_org_id AND j.result IS NULL) THEN
        RAISE EXCEPTION 'storage_reconciliation_pending';
    END IF;
    PERFORM public._version_assert_storage_inventory(p_org_id);
    PERFORM public._version_billing_entitlement(p_org_id);
    INSERT INTO public.version_storage_reconciliations(id,org_id) VALUES(p_id,p_org_id);
    INSERT INTO public.version_storage_reconciliation_projects(reconciliation_id,project_id,state)
        SELECT p_id,s.project_id,s.state FROM public._version_storage_project_states(p_org_id) s;
    GET DIAGNOSTICS n=ROW_COUNT;
    UPDATE public.version_storage_reconciliations SET project_count=n WHERE id=p_id;
    RETURN jsonb_build_object('id',p_id,'org_id',p_org_id,'project_count',n,'result',NULL);
END $$;

CREATE FUNCTION public.get_version_storage_reconciliation_page(p_org_id text,p_id uuid,p_after text DEFAULT '',p_limit integer DEFAULT 200)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE job public.version_storage_reconciliations%ROWTYPE; items jsonb;
BEGIN
    IF p_after IS NULL OR p_limit IS NULL OR p_limit<1 OR p_limit>200 THEN RAISE EXCEPTION 'invalid_storage_inventory_cursor'; END IF;
    SELECT * INTO job FROM public.version_storage_reconciliations WHERE id=p_id AND org_id=p_org_id;
    IF NOT FOUND THEN RAISE EXCEPTION 'storage_reconciliation_unavailable'; END IF;
    IF job.result IS NULL AND job.expires_at<=clock_timestamp() THEN RAISE EXCEPTION 'storage_reconciliation_expired'; END IF;
    SELECT coalesce(jsonb_agg(jsonb_build_object('project_id',s.project_id,'state',s.state,
        'logical_bytes',s.logical_bytes) ORDER BY s.project_id COLLATE "C"),'[]'::jsonb) INTO items
    FROM (SELECT * FROM public.version_storage_reconciliation_projects
        WHERE reconciliation_id=p_id AND project_id COLLATE "C">p_after COLLATE "C"
        ORDER BY project_id COLLATE "C" LIMIT p_limit) s;
    RETURN jsonb_build_object('id',p_id,'org_id',p_org_id,'projects',items);
END $$;

CREATE FUNCTION public.record_version_storage_measurements(p_org_id text,p_id uuid,p_values jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE job public.version_storage_reconciliations%ROWTYPE; item record; old bigint; measured bigint;
BEGIN
    SELECT * INTO job FROM public.version_storage_reconciliations WHERE id=p_id AND org_id=p_org_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'storage_reconciliation_unavailable'; END IF;
    IF job.expires_at<=clock_timestamp() THEN RAISE EXCEPTION 'storage_reconciliation_expired'; END IF;
    IF job.result IS NOT NULL THEN RAISE EXCEPTION 'storage_reconciliation_finished'; END IF;
    IF jsonb_typeof(p_values) IS DISTINCT FROM 'object' THEN RAISE EXCEPTION 'invalid_storage_measurements'; END IF;
    IF (SELECT count(*) FROM jsonb_object_keys(p_values)) NOT BETWEEN 1 AND 200 THEN RAISE EXCEPTION 'invalid_storage_measurements'; END IF;
    FOR item IN SELECT * FROM jsonb_each(p_values) LOOP
        IF jsonb_typeof(item.value)<>'number' OR item.value#>>'{}' !~ '^[0-9]+$' THEN RAISE EXCEPTION 'invalid_storage_measurements'; END IF;
        measured:=(item.value#>>'{}')::bigint;
        SELECT logical_bytes INTO old FROM public.version_storage_reconciliation_projects
            WHERE reconciliation_id=p_id AND project_id=item.key FOR UPDATE;
        IF NOT FOUND THEN RAISE EXCEPTION 'storage_measurement_project_mismatch'; END IF;
        IF old IS NOT NULL AND old<>measured THEN RAISE EXCEPTION 'storage_measurement_reused'; END IF;
        IF measured<>0 AND EXISTS(SELECT 1 FROM public.version_storage_reconciliation_projects
            WHERE reconciliation_id=p_id AND project_id=item.key AND state->>'root' IS NULL) THEN
            RAISE EXCEPTION 'invalid_storage_measurements';
        END IF;
        UPDATE public.version_storage_reconciliation_projects SET logical_bytes=measured
            WHERE reconciliation_id=p_id AND project_id=item.key;
    END LOOP;
    IF job.expires_at<=clock_timestamp() THEN RAISE EXCEPTION 'storage_reconciliation_expired'; END IF;
    RETURN jsonb_build_object('recorded',(SELECT count(*) FROM jsonb_object_keys(p_values)));
END $$;

CREATE OR REPLACE FUNCTION public._version_fence_legacy_storage_reconciliation()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE deadline timestamptz;
BEGIN
    IF NEW.metric='storage.logical_bytes' AND NEW.source='storage_reconciler'
       AND EXISTS(SELECT 1 FROM public.version_repository_billing WHERE org_id=NEW.org_id) THEN
        SELECT expires_at INTO deadline FROM public.version_storage_reconciliations WHERE org_id=NEW.org_id
           AND 'storage-checked:'||id::text=NEW.idempotency_key AND transaction_xid=pg_current_xact_id()
           AND result IS NULL;
        IF NOT FOUND THEN RAISE EXCEPTION 'native_storage_reconciliation_required' USING ERRCODE='55000'; END IF;
        IF deadline<=clock_timestamp() THEN RAISE EXCEPTION 'storage_reconciliation_expired'; END IF;
    END IF;
    RETURN NEW;
END $$;

CREATE FUNCTION public.finish_version_storage_reconciliation(p_org_id text,p_id uuid)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE job public.version_storage_reconciliations%ROWTYPE; entitlement jsonb; total bigint; settled jsonb;
BEGIN
    PERFORM public._version_lock_storage_organization(p_org_id);
    SELECT * INTO job FROM public.version_storage_reconciliations WHERE id=p_id AND org_id=p_org_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'storage_reconciliation_unavailable'; END IF;
    IF job.result IS NOT NULL THEN RETURN job.result; END IF;
    IF job.expires_at<=clock_timestamp() THEN RAISE EXCEPTION 'storage_reconciliation_expired'; END IF;
    PERFORM public._version_assert_storage_inventory(p_org_id);
    -- Compare BOTH directions: missing/added Projects and root/HEAD/format/epoch
    -- changes must never become a partial or stale absolute counter overwrite.
    IF EXISTS((SELECT project_id,state FROM public._version_storage_project_states(p_org_id)
               EXCEPT SELECT project_id,state FROM public.version_storage_reconciliation_projects WHERE reconciliation_id=p_id)
        UNION ALL (SELECT project_id,state FROM public.version_storage_reconciliation_projects WHERE reconciliation_id=p_id
               EXCEPT SELECT project_id,state FROM public._version_storage_project_states(p_org_id))) THEN
        RAISE EXCEPTION 'storage_reconciliation_snapshot_changed' USING ERRCODE='40001';
    END IF;
    IF EXISTS(SELECT 1 FROM public.version_storage_reconciliation_projects WHERE reconciliation_id=p_id AND logical_bytes IS NULL) THEN
        RAISE EXCEPTION 'storage_reconciliation_incomplete';
    END IF;
    entitlement:=public._version_billing_entitlement(p_org_id);
    SELECT coalesce(sum(logical_bytes),0)::bigint INTO total FROM public.version_storage_reconciliation_projects WHERE reconciliation_id=p_id;
    UPDATE public.version_storage_reconciliations SET transaction_xid=pg_current_xact_id() WHERE id=p_id;
    settled:=public.reconcile_organization_usage_counter(p_org_id,'storage.logical_bytes',total,
        (entitlement->>'storage_limit')::bigint,'storage-checked:'||p_id::text,'storage_reconciler',
        jsonb_build_object('schema_version','1.0','reconciliation_id',p_id,'project_count',job.project_count,
            'entitlement_source_revision',entitlement->'source_revision'));
    PERFORM public._version_billing_entitlement(p_org_id,(entitlement->>'source_revision')::bigint);
    IF job.expires_at<=clock_timestamp() THEN RAISE EXCEPTION 'storage_reconciliation_expired'; END IF;
    UPDATE public.version_storage_reconciliations SET result=settled WHERE id=p_id;
    DELETE FROM public.version_storage_reconciliation_projects WHERE reconciliation_id=p_id;
    RETURN settled;
END $$;

CREATE FUNCTION public.cancel_version_storage_reconciliation(p_org_id text,p_id uuid)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
    UPDATE public.version_storage_reconciliations SET expires_at=least(expires_at,clock_timestamp())
        WHERE org_id=p_org_id AND id=p_id AND result IS NULL;
    RETURN FOUND;
END $$;

-- Bounded metadata-only cleanup. This never settles storage I/O claims, drops
-- result receipts, or turns an expired/incomplete capture into a success.
CREATE FUNCTION public.prune_version_storage_measurements(p_limit integer DEFAULT 200)
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE removed bigint;
BEGIN
    IF p_limit IS NULL OR p_limit<1 OR p_limit>200 THEN RAISE EXCEPTION 'invalid_storage_inventory_limit'; END IF;
    WITH doomed AS (
        SELECT p.ctid FROM public.version_storage_reconciliation_projects p
        JOIN public.version_storage_reconciliations j ON j.id=p.reconciliation_id
        WHERE j.result IS NOT NULL OR j.expires_at<=clock_timestamp()
        ORDER BY j.id,p.project_id LIMIT p_limit FOR UPDATE OF j,p SKIP LOCKED
    ) DELETE FROM public.version_storage_reconciliation_projects WHERE ctid IN (SELECT ctid FROM doomed);
    GET DIAGNOSTICS removed=ROW_COUNT;
    RETURN removed;
END $$;

REVOKE ALL ON FUNCTION public._version_lock_storage_organization(text),public._version_assert_storage_inventory(text),
    public._version_storage_project_states(text),
    public._version_fence_legacy_storage_reconciliation(),public.begin_version_storage_reconciliation(text,uuid),
    public.get_version_storage_reconciliation_page(text,uuid,text,integer),
    public.record_version_storage_measurements(text,uuid,jsonb),public.finish_version_storage_reconciliation(text,uuid),
    public.prune_version_storage_measurements(integer),public.cancel_version_storage_reconciliation(text,uuid)
    FROM PUBLIC,anon,authenticated,service_role;
GRANT EXECUTE ON FUNCTION public.begin_version_storage_reconciliation(text,uuid),
    public.get_version_storage_reconciliation_page(text,uuid,text,integer),
    public.record_version_storage_measurements(text,uuid,jsonb),public.finish_version_storage_reconciliation(text,uuid),
    public.prune_version_storage_measurements(integer),public.cancel_version_storage_reconciliation(text,uuid)
    TO service_role;
NOTIFY pgrst,'reload schema';
COMMIT;
