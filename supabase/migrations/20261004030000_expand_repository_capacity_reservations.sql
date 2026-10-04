-- Expand only. Backend technical capacity, NOT customer logical-tree billing.
-- No automatic policy enrollment, inventory backfill, activation or external I/O.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '2min';

CREATE TABLE public.version_organization_capacity (
    org_id text PRIMARY KEY REFERENCES public.organizations(id) ON DELETE CASCADE,
    initialized boolean NOT NULL DEFAULT false,
    max_body_bytes bigint CHECK (max_body_bytes >= 0),
    max_objects bigint CHECK (max_objects >= 0),
    used_body_bytes bigint NOT NULL DEFAULT 0 CHECK (used_body_bytes >= 0),
    used_objects bigint NOT NULL DEFAULT 0 CHECK (used_objects >= 0)
);
CREATE TABLE public.version_repository_capacity (
    -- Like object locations, cleanup identity must survive Project deletion.
    project_id text PRIMARY KEY,
    org_id text NOT NULL REFERENCES public.version_organization_capacity(org_id) ON DELETE CASCADE,
    initialized boolean NOT NULL DEFAULT false,
    max_body_bytes bigint CHECK (max_body_bytes >= 0),
    max_objects bigint CHECK (max_objects >= 0),
    used_body_bytes bigint NOT NULL DEFAULT 0 CHECK (used_body_bytes >= 0),
    used_objects bigint NOT NULL DEFAULT 0 CHECK (used_objects >= 0)
);
CREATE TABLE public.version_repository_object_capacity (
    project_id text NOT NULL REFERENCES public.version_repository_capacity(project_id) ON DELETE CASCADE,
    object_id text NOT NULL CHECK (object_id ~ '^([0-9a-f]{40}|[0-9a-f]{64})$'),
    object_kind text NOT NULL CHECK (object_kind IN ('commit','tree','blob','tag')),
    body_bytes bigint NOT NULL CHECK (body_bytes >= 0),
    admitted_pin_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(project_id,object_id)
);
CREATE TABLE public.version_repository_capacity_inflight (
    project_id text NOT NULL,
    object_id text NOT NULL,
    -- Deliberately no expiring lease/FK cascade: deleting a pin is not evidence
    -- that its worker and outstanding physical I/O have quiesced.
    pin_id uuid NOT NULL,
    -- Each storage invocation has its own identity, including concurrent retries
    -- of the SAME operation/pin. Sealing/releasing a pin cannot settle another I/O.
    io_id uuid NOT NULL,
    PRIMARY KEY(project_id,object_id,pin_id,io_id),
    FOREIGN KEY(project_id,object_id) REFERENCES public.version_repository_object_capacity(project_id,object_id) ON DELETE CASCADE
);
CREATE FUNCTION public.settle_version_object_capacity_io(
    p_project_id text,p_actor text,p_pin_id uuid,p_io_id uuid
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE settled bigint;
BEGIN
    PERFORM public._version_lock_admission_project(p_project_id);
    PERFORM public._version_lock_native_repository(p_project_id,false);
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

CREATE TABLE public.version_repository_capacity_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id text NOT NULL REFERENCES public.version_repository_capacity(project_id) ON DELETE CASCADE,
    operation_id uuid NOT NULL,
    action text NOT NULL CHECK (action IN ('reserve','collect')),
    body_bytes_delta bigint NOT NULL,
    objects_delta bigint NOT NULL,
    object_ids jsonb NOT NULL CHECK (jsonb_typeof(object_ids)='array'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE public.version_repository_capacity_proofs (
    pin_id uuid PRIMARY KEY REFERENCES public.version_object_pins(id) ON DELETE CASCADE,
    project_id text NOT NULL REFERENCES public.version_repositories(project_id) ON DELETE CASCADE,
    manifest_sha256 text NOT NULL CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$')
);

CREATE FUNCTION public.check_version_repository_capacity(p_project_id text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE project public.projects%ROWTYPE; capacity public.version_repository_capacity%ROWTYPE;
    org_capacity public.version_organization_capacity%ROWTYPE;
BEGIN
    project := public._version_lock_admission_project(p_project_id);
    PERFORM public._version_lock_native_repository(p_project_id);
    SELECT * INTO org_capacity FROM public.version_organization_capacity WHERE org_id=project.org_id FOR SHARE;
    IF NOT FOUND OR NOT org_capacity.initialized THEN
        RAISE EXCEPTION 'repository_capacity_uninitialized' USING ERRCODE='55000';
    END IF;
    SELECT * INTO capacity FROM public.version_repository_capacity WHERE project_id=p_project_id FOR SHARE;
    IF NOT FOUND OR NOT capacity.initialized OR capacity.org_id IS DISTINCT FROM project.org_id THEN
        RAISE EXCEPTION 'repository_capacity_uninitialized' USING ERRCODE='55000';
    END IF;
    RETURN jsonb_build_object('project_id',p_project_id,'org_id',project.org_id,
        'metric','git.object_body_bytes','used_body_bytes',capacity.used_body_bytes,'used_objects',capacity.used_objects,
        'max_body_bytes',capacity.max_body_bytes,'max_objects',capacity.max_objects);
END $$;

-- Existing lower-level primitives must not bypass an explicitly enrolled
-- capacity policy. Exact result replay does not INSERT and remains a read.
CREATE FUNCTION public._version_fence_capacity_publication()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE capacity public.version_repository_capacity%ROWTYPE;
    receipt_id uuid; digest text; actual_org text;
BEGIN
    IF TG_TABLE_NAME='version_ref_transactions' THEN
        IF NEW.result->>'status' <> 'committed' THEN RETURN NEW; END IF;
    END IF;
    SELECT * INTO capacity FROM public.version_repository_capacity WHERE project_id=NEW.project_id FOR SHARE;
    IF NOT FOUND THEN RETURN NEW; END IF; -- Dormant low-level/migration profile, not canonical admission.
    SELECT org_id INTO actual_org FROM public.projects WHERE id=NEW.project_id;
    IF NOT capacity.initialized OR capacity.org_id IS DISTINCT FROM actual_org THEN
        RAISE EXCEPTION 'repository_capacity_uninitialized' USING ERRCODE='55000';
    END IF;
    -- Never lock Organization after the ref primitive's Project lock. Capacity
    -- setters/allocators lock Org -> Project, so row locks below cannot invert it.
    PERFORM 1 FROM public.version_organization_capacity WHERE org_id=actual_org AND initialized FOR SHARE;
    IF NOT FOUND THEN RAISE EXCEPTION 'repository_capacity_uninitialized' USING ERRCODE='55000'; END IF;
    IF TG_TABLE_NAME='version_publication_receipts' THEN
        receipt_id := NEW.id; digest := NEW.manifest_sha256;
    ELSE
        receipt_id := (NEW.result->>'receipt_id')::uuid;
        IF receipt_id IS NULL THEN RETURN NEW; END IF;
        SELECT manifest_sha256 INTO digest FROM public.version_publication_receipts WHERE id=receipt_id AND project_id=NEW.project_id;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.version_repository_capacity_proofs
                   WHERE pin_id=receipt_id AND project_id=NEW.project_id AND manifest_sha256=digest) THEN
        RAISE EXCEPTION 'publication_capacity_proof_required' USING ERRCODE='55000';
    END IF;
    IF EXISTS (SELECT 1 FROM public.version_repository_capacity_inflight
               WHERE project_id=NEW.project_id AND pin_id=receipt_id) THEN
        RAISE EXCEPTION 'repository_capacity_io_unsettled' USING ERRCODE='55000';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER version_receipt_capacity_fence BEFORE INSERT ON public.version_publication_receipts
    FOR EACH ROW EXECUTE FUNCTION public._version_fence_capacity_publication();
CREATE TRIGGER version_transaction_capacity_fence BEFORE INSERT ON public.version_ref_transactions
    FOR EACH ROW EXECUTE FUNCTION public._version_fence_capacity_publication();

CREATE FUNCTION public.seal_capacity_version_object_publication(
    p_project_id text,p_actor text,p_pin_id uuid,p_manifest_sha256 text,p_root_details jsonb
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE proof public.version_repository_capacity_proofs%ROWTYPE;
BEGIN
    PERFORM public.check_version_repository_capacity(p_project_id);
    -- Trusted backend attests that this exact physically verified closure was
    -- reconciled against capacity. No claim of SQL-side remote-byte verification.
    INSERT INTO public.version_repository_capacity_proofs(pin_id,project_id,manifest_sha256)
        VALUES(p_pin_id,p_project_id,p_manifest_sha256) ON CONFLICT (pin_id) DO NOTHING;
    SELECT * INTO proof FROM public.version_repository_capacity_proofs WHERE pin_id=p_pin_id;
    IF proof.project_id IS DISTINCT FROM p_project_id OR proof.manifest_sha256 IS DISTINCT FROM p_manifest_sha256 THEN
        RAISE EXCEPTION 'publication_capacity_proof_mismatch' USING ERRCODE='22023';
    END IF;
    -- Failure rolls back the attestation as well. Missing raw seal is fatal.
    RETURN public.seal_version_object_publication(p_project_id,p_actor,p_pin_id,p_manifest_sha256,p_root_details);
END $$;

CREATE FUNCTION public.reserve_version_object_capacity(
    p_project_id text,p_actor text,p_pin_id uuid,p_objects jsonb,p_required boolean DEFAULT true,
    p_io_id uuid DEFAULT gen_random_uuid()
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE project public.projects%ROWTYPE; repo public.version_repositories%ROWTYPE; pin public.version_object_pins%ROWTYPE;
    org_capacity public.version_organization_capacity%ROWTYPE;
    capacity public.version_repository_capacity%ROWTYPE; existing public.version_repository_object_capacity%ROWTYPE;
    item jsonb; oid text; kind text; size_text text; object_bytes bigint;
    new_bytes numeric := 0; new_count bigint := 0; new_ids jsonb := '[]'::jsonb;
BEGIN
    project := public._version_lock_admission_project(p_project_id);
    repo := public._version_lock_native_repository(p_project_id);
    SELECT * INTO pin FROM public.version_object_pins WHERE id=p_pin_id FOR SHARE;
    IF NOT FOUND OR pin.project_id IS DISTINCT FROM p_project_id OR pin.actor IS DISTINCT FROM p_actor
       OR pin.purpose <> 'publication' OR pin.state <> 'uploading'
       OR pin.object_format IS DISTINCT FROM repo.object_format OR pin.generation <> repo.generation
       OR pin.gc_epoch <> repo.gc_epoch OR repo.gc_token IS NOT NULL OR pin.expires_at <= clock_timestamp() THEN
        RAISE EXCEPTION 'publication_pin_unavailable' USING ERRCODE='55000';
    END IF;
    -- A dormant low-level profile may have no capacity enrollment. A context
    -- flag can require enrollment, but cannot bypass an existing policy.
    IF p_required=false AND NOT EXISTS (SELECT 1 FROM public.version_repository_capacity WHERE project_id=p_project_id) THEN
        RETURN jsonb_build_object('new_body_bytes',0,'new_objects',0,'profile','dormant');
    END IF;
    SELECT * INTO org_capacity FROM public.version_organization_capacity WHERE org_id=project.org_id FOR UPDATE;
    IF NOT FOUND OR NOT org_capacity.initialized THEN
        RAISE EXCEPTION 'repository_capacity_uninitialized' USING ERRCODE='55000';
    END IF;
    SELECT * INTO capacity FROM public.version_repository_capacity WHERE project_id=p_project_id FOR UPDATE;
    IF NOT FOUND OR NOT capacity.initialized OR capacity.org_id IS DISTINCT FROM project.org_id THEN
        RAISE EXCEPTION 'repository_capacity_uninitialized' USING ERRCODE='55000';
    END IF;
    IF pin.expires_at <= clock_timestamp() THEN
        RAISE EXCEPTION 'publication_pin_unavailable' USING ERRCODE='55000';
    END IF;
    IF jsonb_typeof(p_objects) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION 'invalid_capacity_objects' USING ERRCODE='22023';
    END IF;
    IF jsonb_array_length(p_objects) NOT BETWEEN 1 AND 200
       OR (SELECT count(DISTINCT value->>'object_id') FROM jsonb_array_elements(p_objects)) <> jsonb_array_length(p_objects) THEN
        RAISE EXCEPTION 'invalid_capacity_objects' USING ERRCODE='22023';
    END IF;
    FOR item IN SELECT value FROM jsonb_array_elements(p_objects) ORDER BY value->>'object_id' LOOP
        oid := item->>'object_id'; kind := item->>'object_kind'; size_text := item->>'body_bytes';
        IF public._version_oid_valid(oid,repo.object_format) IS DISTINCT FROM true
           OR kind IS NULL OR kind NOT IN ('commit','tree','blob','tag')
           OR jsonb_typeof(item->'body_bytes') IS DISTINCT FROM 'number'
           OR size_text !~ '^(0|[1-9][0-9]*)$' OR size_text::numeric > 9223372036854775807::numeric THEN
            RAISE EXCEPTION 'invalid_capacity_objects' USING ERRCODE='22023';
        END IF;
        object_bytes := size_text::bigint;
        SELECT * INTO existing FROM public.version_repository_object_capacity
            WHERE project_id=p_project_id AND object_id=oid FOR UPDATE;
        IF FOUND THEN
            IF existing.object_kind IS DISTINCT FROM kind OR existing.body_bytes <> object_bytes THEN
                RAISE EXCEPTION 'repository_object_identity_mismatch' USING ERRCODE='22023';
            END IF;
        ELSE
            new_bytes := new_bytes + object_bytes; new_count := new_count + 1;
            new_ids := new_ids || jsonb_build_array(oid);
        END IF;
    END LOOP;
    IF new_count > 0 AND (
        (capacity.max_body_bytes IS NOT NULL AND capacity.used_body_bytes::numeric + new_bytes > capacity.max_body_bytes)
        OR (capacity.max_objects IS NOT NULL AND capacity.used_objects::numeric + new_count > capacity.max_objects)
        OR (org_capacity.max_body_bytes IS NOT NULL AND org_capacity.used_body_bytes::numeric + new_bytes > org_capacity.max_body_bytes)
        OR (org_capacity.max_objects IS NOT NULL AND org_capacity.used_objects::numeric + new_count > org_capacity.max_objects)
    ) THEN
        RAISE EXCEPTION 'repository_capacity_exceeded' USING ERRCODE='P0001';
    END IF;
    IF new_count > 0 THEN
        INSERT INTO public.version_repository_object_capacity(project_id,object_id,object_kind,body_bytes,admitted_pin_id)
        SELECT p_project_id,r.object_id,r.object_kind,r.body_bytes,p_pin_id
        FROM jsonb_to_recordset(p_objects) AS r(object_id text,object_kind text,body_bytes bigint)
        ORDER BY r.object_id ON CONFLICT (project_id,object_id) DO NOTHING;
        UPDATE public.version_repository_capacity SET used_body_bytes=(used_body_bytes::numeric+new_bytes)::bigint,
            used_objects=used_objects+new_count WHERE project_id=p_project_id;
        UPDATE public.version_organization_capacity SET used_body_bytes=(used_body_bytes::numeric+new_bytes)::bigint,
            used_objects=used_objects+new_count WHERE org_id=project.org_id;
        INSERT INTO public.version_repository_capacity_events(project_id,operation_id,action,body_bytes_delta,objects_delta,object_ids)
        VALUES(p_project_id,p_pin_id,'reserve',new_bytes::bigint,new_count,new_ids);
    END IF;
    -- NULL is a trusted backend's metadata-only reconciliation of already
    -- verified bytes. Physical writers MUST supply a fresh invocation identity.
    IF p_io_id IS NOT NULL THEN
        INSERT INTO public.version_repository_capacity_inflight(project_id,object_id,pin_id,io_id)
            SELECT p_project_id,value->>'object_id',p_pin_id,p_io_id FROM jsonb_array_elements(p_objects)
            ORDER BY value->>'object_id' ON CONFLICT DO NOTHING;
    END IF;
    -- Time can pass while waiting on owner repair/inventory rows. No provisional
    -- allocation may survive an expired producer, even with a captured context.
    IF pin.expires_at <= clock_timestamp() THEN
        RAISE EXCEPTION 'publication_pin_unavailable' USING ERRCODE='55000';
    END IF;
    RETURN jsonb_build_object('new_body_bytes',new_bytes,'new_objects',new_count);
END $$;

CREATE FUNCTION public.collect_version_object_capacity(
    p_project_id text,p_gc_token uuid,p_object_ids text[]
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE project public.projects%ROWTYPE; repo public.version_repositories%ROWTYPE;
    removed_bytes bigint; removed_count bigint; removed_ids jsonb;
BEGIN
    project := public._version_lock_admission_project(p_project_id);
    repo := public._version_lock_native_repository(p_project_id,false);
    IF p_gc_token IS NULL OR repo.gc_token IS DISTINCT FROM p_gc_token THEN
        RAISE EXCEPTION 'repository_gc_token_mismatch' USING ERRCODE='55000';
    END IF;
    IF cardinality(p_object_ids) IS NULL OR cardinality(p_object_ids) NOT BETWEEN 1 AND 200
       OR EXISTS (SELECT 1 FROM unnest(p_object_ids) oid WHERE public._version_oid_valid(oid,repo.object_format) IS DISTINCT FROM true) THEN
        RAISE EXCEPTION 'invalid_capacity_objects' USING ERRCODE='22023';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.version_repository_capacity WHERE project_id=p_project_id) THEN
        RETURN jsonb_build_object('removed_body_bytes',0,'removed_objects',0,'profile','dormant');
    END IF;
    PERFORM 1 FROM public.version_organization_capacity WHERE org_id=project.org_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'repository_capacity_uninitialized' USING ERRCODE='55000'; END IF;
    PERFORM 1 FROM public.version_repository_capacity WHERE project_id=p_project_id AND org_id=project.org_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'repository_capacity_uninitialized' USING ERRCODE='55000'; END IF;
    IF EXISTS (SELECT 1 FROM public.version_object_locations WHERE project_id=p_project_id AND object_id=ANY(p_object_ids)) THEN
        RAISE EXCEPTION 'repository_capacity_collection_incomplete' USING ERRCODE='55000';
    END IF;
    IF EXISTS (SELECT 1 FROM public.version_repository_capacity_inflight
               WHERE project_id=p_project_id AND object_id=ANY(p_object_ids)) THEN
        RAISE EXCEPTION 'repository_capacity_io_unsettled' USING ERRCODE='55000';
    END IF;
    -- Caller must finish physical deletion and index removal first. Uncertain
    -- deletion keeps both its non-expiring GC fence and capacity allocation.
    WITH removed AS (
        DELETE FROM public.version_repository_object_capacity
        WHERE project_id=p_project_id AND object_id=ANY(p_object_ids) RETURNING object_id,body_bytes
    ) SELECT COALESCE(sum(body_bytes),0)::bigint,count(*),COALESCE(jsonb_agg(object_id ORDER BY object_id),'[]'::jsonb)
        INTO removed_bytes,removed_count,removed_ids FROM removed;
    IF removed_count > 0 THEN
        UPDATE public.version_repository_capacity SET used_body_bytes=used_body_bytes-removed_bytes,
            used_objects=used_objects-removed_count WHERE project_id=p_project_id;
        UPDATE public.version_organization_capacity SET used_body_bytes=used_body_bytes-removed_bytes,
            used_objects=used_objects-removed_count WHERE org_id=project.org_id;
        INSERT INTO public.version_repository_capacity_events(project_id,operation_id,action,body_bytes_delta,objects_delta,object_ids)
        VALUES(p_project_id,p_gc_token,'collect',-removed_bytes,-removed_count,removed_ids);
    END IF;
    RETURN jsonb_build_object('removed_body_bytes',removed_bytes,'removed_objects',removed_count);
END $$;

CREATE FUNCTION public.get_version_repository_capacity_inventory(
    p_project_id text,p_token uuid,p_after text DEFAULT '',p_limit integer DEFAULT 200
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE repo public.version_repositories%ROWTYPE; inventory jsonb;
BEGIN
    repo := public._version_lock_native_repository(p_project_id,false);
    IF p_token IS NULL OR (repo.gc_token IS DISTINCT FROM p_token AND NOT EXISTS (
        SELECT 1 FROM public.version_object_pins WHERE id=p_token AND project_id=p_project_id
        AND purpose='read' AND state='uploading' AND expires_at>clock_timestamp()
    )) THEN RAISE EXCEPTION 'repository_gc_token_mismatch' USING ERRCODE='55000'; END IF;
    IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 200 OR p_after IS NULL
       OR (p_after<>'' AND public._version_oid_valid(p_after,repo.object_format) IS DISTINCT FROM true) THEN
        RAISE EXCEPTION 'invalid_capacity_inventory_cursor' USING ERRCODE='22023';
    END IF;
    SELECT COALESCE(jsonb_object_agg(object_id,jsonb_build_object('created_at',created_at,'unsettled',unsettled)),'{}')
    INTO inventory FROM (
        SELECT c.object_id,c.created_at,EXISTS (
            SELECT 1 FROM public.version_repository_capacity_inflight i
            WHERE i.project_id=c.project_id AND i.object_id=c.object_id
        ) AS unsettled FROM public.version_repository_object_capacity c
        WHERE c.project_id=p_project_id AND c.object_id>p_after ORDER BY c.object_id LIMIT p_limit
    ) page;
    RETURN jsonb_build_object('objects',inventory);
END $$;
REVOKE ALL ON FUNCTION public.get_version_repository_capacity_inventory(text,uuid,text,integer) FROM PUBLIC,anon,authenticated,service_role;
GRANT EXECUTE ON FUNCTION public.get_version_repository_capacity_inventory(text,uuid,text,integer) TO service_role;

ALTER TABLE public.version_repository_capacity_inflight ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.version_repository_capacity_inflight FROM PUBLIC,anon,authenticated,service_role;
GRANT SELECT ON TABLE public.version_repository_capacity_inflight TO service_role;
REVOKE ALL ON FUNCTION public.settle_version_object_capacity_io(text,text,uuid,uuid) FROM PUBLIC,anon,authenticated,service_role;
GRANT EXECUTE ON FUNCTION public.settle_version_object_capacity_io(text,text,uuid,uuid) TO service_role;
ALTER TABLE public.version_repository_capacity_proofs ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.version_repository_capacity_proofs FROM PUBLIC,anon,authenticated,service_role;
GRANT SELECT ON TABLE public.version_repository_capacity_proofs TO service_role;
REVOKE ALL ON FUNCTION public._version_fence_capacity_publication() FROM PUBLIC,anon,authenticated,service_role;
REVOKE ALL ON FUNCTION public.check_version_repository_capacity(text),
    public.seal_capacity_version_object_publication(text,text,uuid,text,jsonb) FROM PUBLIC,anon,authenticated,service_role;
GRANT EXECUTE ON FUNCTION public.check_version_repository_capacity(text),
    public.seal_capacity_version_object_publication(text,text,uuid,text,jsonb) TO service_role;
ALTER TABLE public.version_organization_capacity ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.version_repository_capacity ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.version_repository_object_capacity ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.version_repository_capacity_events ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.version_organization_capacity,public.version_repository_capacity,public.version_repository_object_capacity,
    public.version_repository_capacity_events FROM PUBLIC,anon,authenticated,service_role;
GRANT SELECT ON TABLE public.version_organization_capacity,public.version_repository_capacity,public.version_repository_object_capacity,
    public.version_repository_capacity_events TO service_role;
REVOKE ALL ON SEQUENCE public.version_repository_capacity_events_id_seq FROM PUBLIC,anon,authenticated,service_role;
REVOKE ALL ON FUNCTION public.reserve_version_object_capacity(text,text,uuid,jsonb,boolean,uuid),
    public.collect_version_object_capacity(text,uuid,text[]) FROM PUBLIC,anon,authenticated,service_role;
GRANT EXECUTE ON FUNCTION public.reserve_version_object_capacity(text,text,uuid,jsonb,boolean,uuid),
    public.collect_version_object_capacity(text,uuid,text[]) TO service_role;
COMMENT ON TABLE public.version_repository_capacity IS 'Explicitly initialized technical Git object-body capacity, separate from customer logical-tree billing. No automatic enrollment.';
COMMENT ON TABLE public.version_repository_object_capacity IS 'Unique retained/reserved local object facts. Pin expiry or rejected refs never refunds uncertain physical I/O; only fenced collection releases capacity.';
NOTIFY pgrst, 'reload schema';
COMMIT;
