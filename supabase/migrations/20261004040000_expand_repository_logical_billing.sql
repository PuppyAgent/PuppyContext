-- ISSUE-062: dormant checked logical billing. Expand only; no enrollment,
-- baseline reconciliation, activation, remote I/O or changes to invoice metrics.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '2min';

CREATE TABLE public.version_repository_billing (
    project_id text PRIMARY KEY REFERENCES public.version_repositories(project_id) ON DELETE CASCADE,
    org_id text NOT NULL REFERENCES public.organizations(id),
    initialized boolean NOT NULL DEFAULT false
);
CREATE INDEX version_repository_billing_org_idx ON public.version_repository_billing(org_id);
CREATE TABLE public.version_repository_billing_authorizations (
    project_id text NOT NULL,
    actor text NOT NULL,
    request_key uuid NOT NULL,
    transaction_xid xid8 NOT NULL,
    PRIMARY KEY (project_id,actor,request_key),
    FOREIGN KEY (project_id,actor,request_key)
        REFERENCES public.version_ref_transactions(project_id,actor,request_key)
        ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED
);
ALTER TABLE public.version_repository_billing ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.version_repository_billing_authorizations ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.version_repository_billing, public.version_repository_billing_authorizations
    FROM PUBLIC, anon, authenticated, service_role;
GRANT SELECT ON public.version_repository_billing, public.version_repository_billing_authorizations TO service_role;

CREATE FUNCTION public._version_default_head_oid(p_project_id text)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE direct text; symbolic bytea;
BEGIN
    SELECT target_oid,symbolic_target INTO direct,symbolic
    FROM public.version_repository_refs WHERE project_id=p_project_id AND name=convert_to('HEAD','UTF8');
    IF NOT FOUND THEN RAISE EXCEPTION 'repository_metadata_incomplete' USING ERRCODE='55000'; END IF;
    IF symbolic IS NULL THEN RETURN direct; END IF;
    RETURN (SELECT target_oid FROM public.version_repository_refs WHERE project_id=p_project_id AND name=symbolic);
END $$;

CREATE FUNCTION public._version_billing_entitlement(p_org_id text,p_revision bigint DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE entitlement public.organization_entitlements%ROWTYPE; quota jsonb; value text; maximum bigint;
BEGIN
    SELECT * INTO entitlement FROM public.organization_entitlements WHERE org_id=p_org_id FOR SHARE;
    IF NOT FOUND THEN RAISE EXCEPTION 'storage_billing_entitlement_unavailable' USING ERRCODE='55000'; END IF;
    IF entitlement.source IS DISTINCT FROM 'puppypay' OR entitlement.source_revision <= 0
       OR entitlement.payload_hash !~ '^[0-9a-f]{64}$' OR entitlement.schema_version !~ '^1([.][0-9]+)?$'
       OR (entitlement.effective_until IS NOT NULL AND entitlement.effective_until <= clock_timestamp())
       OR jsonb_typeof(entitlement.entitlements) IS DISTINCT FROM 'object'
       OR jsonb_typeof(entitlement.entitlements->'limits') IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'storage_billing_entitlement_invalid' USING ERRCODE='55000';
    END IF;
    quota := entitlement.entitlements->'limits'->'storage.max_bytes';
    IF jsonb_typeof(quota)='null' THEN maximum := NULL;
    ELSIF jsonb_typeof(quota)='number' THEN
        value := quota #>> '{}';
        IF value !~ '^[0-9]+$' OR value::numeric > 9223372036854775807::numeric THEN
            RAISE EXCEPTION 'storage_billing_entitlement_invalid' USING ERRCODE='55000';
        END IF;
        maximum := value::bigint;
    ELSE RAISE EXCEPTION 'storage_billing_entitlement_invalid' USING ERRCODE='55000'; END IF;
    IF p_revision IS NOT NULL AND p_revision IS DISTINCT FROM entitlement.source_revision THEN
        RAISE EXCEPTION 'storage_billing_entitlement_changed' USING ERRCODE='55000';
    END IF;
    RETURN jsonb_build_object('org_id',p_org_id,'source_revision',entitlement.source_revision,'storage_limit',maximum);
END $$;

-- Called before Project locks, matching existing PuppyPay publication and the
-- legacy metered publisher. No new lock order or alternative billing source.
CREATE FUNCTION public._version_lock_billing_organization(p_project_id text)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE org text; project public.projects%ROWTYPE;
BEGIN
    SELECT org_id INTO org FROM public.projects WHERE id=p_project_id;
    IF org IS NULL THEN RAISE EXCEPTION 'repository_unavailable' USING ERRCODE='55000'; END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(org,0));
    PERFORM pg_advisory_xact_lock(hashtextextended(org || ':storage.logical_bytes',0));
    project := public._version_lock_admission_project(p_project_id);
    IF project.org_id IS DISTINCT FROM org THEN
        RAISE EXCEPTION 'storage_billing_context_mismatch' USING ERRCODE='55000';
    END IF;
    PERFORM public._version_lock_native_repository(p_project_id,false);
    RETURN org;
END $$;

CREATE FUNCTION public._version_billing_usage(p_project_id text,p_org_id text)
RETURNS public.organization_usage_counters LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE policy public.version_repository_billing%ROWTYPE; usage public.organization_usage_counters%ROWTYPE;
BEGIN
    SELECT * INTO policy FROM public.version_repository_billing WHERE project_id=p_project_id FOR SHARE;
    IF NOT FOUND OR NOT policy.initialized OR policy.org_id IS DISTINCT FROM p_org_id THEN
        RAISE EXCEPTION 'repository_billing_uninitialized' USING ERRCODE='55000';
    END IF;
    SELECT * INTO usage FROM public.organization_usage_counters
    WHERE org_id=p_org_id AND metric='storage.logical_bytes' FOR UPDATE;
    IF NOT FOUND OR usage.version <= 0 THEN
        RAISE EXCEPTION 'storage_billing_usage_uninitialized' USING ERRCODE='55000';
    END IF;
    RETURN usage;
END $$;

CREATE FUNCTION public.check_version_repository_billing(p_project_id text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE org text; usage public.organization_usage_counters%ROWTYPE;
BEGIN
    org := public._version_lock_billing_organization(p_project_id);
    usage := public._version_billing_usage(p_project_id,org);
    RETURN public._version_billing_entitlement(org) || jsonb_build_object(
        'project_id',p_project_id,'metric','storage.logical_bytes','value',usage.value,'version',usage.version);
END $$;

CREATE FUNCTION public._version_fence_billing_publication()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF NEW.result->>'status'='committed'
       AND EXISTS (SELECT 1 FROM public.version_repository_billing WHERE project_id=NEW.project_id)
       AND NOT EXISTS (SELECT 1 FROM public.version_repository_billing_authorizations
           WHERE project_id=NEW.project_id AND actor=NEW.actor AND request_key=NEW.request_key
             AND transaction_xid=pg_current_xact_id()) THEN
        RAISE EXCEPTION 'repository_billing_coordination_required' USING ERRCODE='55000';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER version_transaction_billing_fence BEFORE INSERT ON public.version_ref_transactions
FOR EACH ROW EXECUTE FUNCTION public._version_fence_billing_publication();

-- The existing background reconciler reads legacy Project roots. It must not
-- overwrite a native Organization's incremental usage with that stale view.
-- The counter update and this event insert share a transaction, so rejection
-- rolls both back. Native-aware full reconciliation is a separate required gate.
CREATE FUNCTION public._version_fence_legacy_storage_reconciliation()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF NEW.metric='storage.logical_bytes' AND NEW.source='storage_reconciler'
       AND EXISTS (SELECT 1 FROM public.version_repository_billing WHERE org_id=NEW.org_id) THEN
        RAISE EXCEPTION 'native_storage_reconciliation_required' USING ERRCODE='55000';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER version_legacy_storage_reconciliation_fence BEFORE INSERT ON public.organization_usage_events
FOR EACH ROW EXECUTE FUNCTION public._version_fence_legacy_storage_reconciliation();

CREATE FUNCTION public.apply_billed_version_ref_transaction(
    p_project_id text,p_actor text,p_request_key uuid,p_generation bigint,
    p_updates jsonb,p_receipt_id uuid,p_message text,p_lease_id uuid,p_holder_id text,p_usage jsonb
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE org text; result jsonb; before_oid text; after_oid text; delta bigint := 0;
    usage public.organization_usage_counters%ROWTYPE; entitlement jsonb;
    revision bigint; maximum bigint; new_value bigint; old_bytes bigint; new_bytes bigint;
BEGIN
    org := public._version_lock_billing_organization(p_project_id);
    -- Original replay is current-read authorized by the existing admitted RPC.
    -- Its digest excludes server-derived metering facts; no new write/charge.
    IF public.get_version_ref_transaction(p_project_id,p_actor,p_request_key) IS NOT NULL THEN
        RETURN public.apply_admitted_version_ref_transaction(p_project_id,p_actor,p_request_key,p_generation,
            p_updates,p_receipt_id,p_message,p_lease_id,p_holder_id);
    END IF;
    PERFORM public.check_version_repository_write_admission(p_project_id,p_actor,p_lease_id,p_holder_id);
    usage := public._version_billing_usage(p_project_id,org);
    before_oid := public._version_default_head_oid(p_project_id);
    INSERT INTO public.version_repository_billing_authorizations(project_id,actor,request_key,transaction_xid)
    VALUES(p_project_id,p_actor,p_request_key,pg_current_xact_id());
    result := public.apply_admitted_version_ref_transaction(p_project_id,p_actor,p_request_key,p_generation,
        p_updates,p_receipt_id,p_message,p_lease_id,p_holder_id);
    IF result->>'status'='rejected' THEN RETURN result; END IF;
    IF jsonb_typeof(p_usage) IS DISTINCT FROM 'object' OR p_usage->>'org_id' IS DISTINCT FROM org
       OR jsonb_typeof(p_usage->'source_revision') IS DISTINCT FROM 'number'
       OR p_usage->>'source_revision' !~ '^[1-9][0-9]*$' THEN
        RAISE EXCEPTION 'invalid_storage_usage_input' USING ERRCODE='22023';
    END IF;
    revision := (p_usage->>'source_revision')::bigint;
    entitlement := public._version_billing_entitlement(org,revision);
    maximum := (entitlement->>'storage_limit')::bigint;
    after_oid := public._version_default_head_oid(p_project_id);
    IF before_oid IS DISTINCT FROM after_oid THEN
        IF p_usage->>'old_head_oid' IS DISTINCT FROM before_oid OR p_usage->>'new_head_oid' IS DISTINCT FROM after_oid THEN
            RAISE EXCEPTION 'storage_billing_snapshot_changed' USING ERRCODE='40001';
        END IF;
        IF jsonb_typeof(p_usage->'old_bytes') IS DISTINCT FROM 'number'
           OR jsonb_typeof(p_usage->'new_bytes') IS DISTINCT FROM 'number'
           OR p_usage->>'old_bytes' !~ '^[0-9]+$' OR p_usage->>'new_bytes' !~ '^[0-9]+$' THEN
            RAISE EXCEPTION 'invalid_storage_usage_input' USING ERRCODE='22023';
        END IF;
        old_bytes := (p_usage->>'old_bytes')::bigint;
        new_bytes := (p_usage->>'new_bytes')::bigint;
        IF (before_oid IS NULL AND old_bytes <> 0) OR (after_oid IS NULL AND new_bytes <> 0) THEN
            RAISE EXCEPTION 'invalid_storage_usage_input' USING ERRCODE='22023';
        END IF;
        delta := new_bytes-old_bytes;
        new_value := (usage.value::numeric + delta::numeric)::bigint;
        IF new_value < 0 THEN RAISE EXCEPTION 'storage_billing_counter_inconsistent' USING ERRCODE='55000'; END IF;
        IF delta > 0 AND maximum IS NOT NULL AND new_value > maximum THEN
            RAISE EXCEPTION 'storage_quota_exceeded:%:%',new_value,maximum USING ERRCODE='P0001';
        END IF;
        -- Operation identity, not commit OID: a later rewind/HEAD switch can
        -- legitimately revisit the same commit and must settle its own delta.
        PERFORM public.reconcile_organization_usage_counter(org,'storage.logical_bytes',new_value,maximum,
            'storage-ref:' || (result->>'transaction_id'),'version_engine',jsonb_build_object(
                'project_id',p_project_id,'ref_transaction_id',result->>'transaction_id',
                'old_head_oid',before_oid,'new_head_oid',after_oid,'delta_bytes',delta,
                'limit_source','entitlement_projection','entitlement_source_revision',revision));
    END IF;
    UPDATE public.version_ref_events SET payload=payload || jsonb_build_object('logical_storage',jsonb_build_object(
        'metric','storage.logical_bytes','delta_bytes',delta,'entitlement_source_revision',revision))
    WHERE transaction_id=(result->>'transaction_id')::uuid;
    -- Quota/usage/trigger waits happen after the admitted primitive returns.
    -- Recheck all time-sensitive facts before allowing this transaction to ACK.
    PERFORM public._version_billing_entitlement(org,revision);
    PERFORM public._version_assert_current_repository_actor(p_project_id,org,p_actor,true);
    PERFORM public._version_assert_write_lease(p_project_id,p_lease_id,p_holder_id);
    RETURN result;
END $$;

REVOKE ALL ON FUNCTION public._version_default_head_oid(text),
    public._version_billing_entitlement(text,bigint), public._version_lock_billing_organization(text),
    public._version_billing_usage(text,text), public._version_fence_billing_publication(),
    public._version_fence_legacy_storage_reconciliation(),
    public.check_version_repository_billing(text),
    public.apply_billed_version_ref_transaction(text,text,uuid,bigint,jsonb,uuid,text,uuid,text,jsonb)
    FROM PUBLIC,anon,authenticated,service_role;
GRANT EXECUTE ON FUNCTION public.check_version_repository_billing(text),
    public.apply_billed_version_ref_transaction(text,text,uuid,bigint,jsonb,uuid,text,uuid,text,jsonb)
    TO service_role;
NOTIFY pgrst, 'reload schema';
COMMIT;
