-- ISSUE-062 Expand: physical attempts are not logical operations or I/O claims.
-- Empty metadata expansion only: no enrollment, backfill, publication or remote I/O.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='2min';

-- No Project/pin/lease cascade: retirement and cleanup identity must survive them.
CREATE TABLE public.version_product_publication_attempts (
    pin_id uuid PRIMARY KEY,
    project_id text NOT NULL,
    actor text NOT NULL,
    request_key uuid NOT NULL,
    native_request_sha256 text NOT NULL CHECK (native_request_sha256 ~ '^[0-9a-f]{64}$'),
    active boolean NOT NULL,
    initial_tombstone boolean NOT NULL DEFAULT false,
    lease_id uuid,
    holder_id text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY(project_id,actor,request_key)
        REFERENCES public.version_product_operations(project_id,actor,request_key),
    CHECK ((initial_tombstone AND NOT active AND lease_id IS NULL AND holder_id IS NULL)
        OR (NOT initial_tombstone AND lease_id IS NOT NULL AND holder_id IS NOT NULL))
);
CREATE UNIQUE INDEX version_product_active_attempt_idx
    ON public.version_product_publication_attempts(project_id,actor,request_key) WHERE active;
ALTER TABLE public.version_product_publication_attempts ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.version_product_publication_attempts FROM PUBLIC,anon,authenticated,service_role;
GRANT SELECT ON public.version_product_publication_attempts TO service_role;

CREATE OR REPLACE FUNCTION public._version_product_operation_result(p_operation public.version_product_operations)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE published public.version_ref_transactions%ROWTYPE;
BEGIN
    SELECT * INTO published FROM public.version_ref_transactions
        WHERE project_id=p_operation.project_id AND actor=p_operation.actor AND request_key=p_operation.request_key;
    IF NOT FOUND THEN RETURN NULL; END IF;
    IF p_operation.native_request_sha256 IS NULL OR (
        published.request_sha256 IS DISTINCT FROM p_operation.native_request_sha256 AND NOT EXISTS (
            SELECT 1 FROM public.version_product_publication_attempts a
            WHERE a.project_id=p_operation.project_id AND a.actor=p_operation.actor AND a.request_key=p_operation.request_key
              AND a.native_request_sha256=published.request_sha256
        )) THEN RAISE EXCEPTION 'request_key_reused' USING ERRCODE='22023'; END IF;
    RETURN published.result;
END $$;

CREATE OR REPLACE FUNCTION public._version_fence_product_operation_result()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE operation public.version_product_operations%ROWTYPE; digest text;
BEGIN
    SELECT * INTO operation FROM public.version_product_operations
        WHERE project_id=NEW.project_id AND actor=NEW.actor AND request_key=NEW.request_key FOR UPDATE;
    IF FOUND THEN
        SELECT native_request_sha256 INTO digest FROM public.version_product_publication_attempts
            WHERE project_id=NEW.project_id AND actor=NEW.actor AND request_key=NEW.request_key AND active FOR SHARE;
        IF NOT FOUND THEN
            IF EXISTS(SELECT 1 FROM public.version_product_publication_attempts
                WHERE project_id=NEW.project_id AND actor=NEW.actor AND request_key=NEW.request_key) THEN
                RAISE EXCEPTION 'product_attempt_superseded' USING ERRCODE='55000';
            END IF;
            digest:=operation.native_request_sha256;
        END IF;
        IF digest IS NULL OR digest IS DISTINCT FROM NEW.request_sha256 THEN
            RAISE EXCEPTION 'request_key_reused' USING ERRCODE='22023';
        END IF;
    END IF;
    RETURN NEW;
END $$;

CREATE FUNCTION public._version_fence_product_attempt_pin()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE attempt public.version_product_publication_attempts%ROWTYPE;
    operation public.version_product_operations%ROWTYPE; roots jsonb;
BEGIN
    -- A retired invocation may not start late, even if it never created its pin.
    -- Release is always permitted, but it never settles capacity/I/O claims.
    IF NEW.purpose='publication' AND NEW.state<>'released' THEN
        SELECT * INTO attempt FROM public.version_product_publication_attempts WHERE pin_id=NEW.id FOR SHARE;
        IF FOUND THEN
            IF NOT attempt.active OR attempt.project_id<>NEW.project_id OR attempt.actor<>NEW.actor THEN
                RAISE EXCEPTION 'product_attempt_superseded' USING ERRCODE='55000';
            END IF;
            SELECT * INTO operation FROM public.version_product_operations
                WHERE project_id=attempt.project_id AND actor=attempt.actor AND request_key=attempt.request_key FOR SHARE;
            SELECT jsonb_object_agg(item->'new'->>'oid','commit') INTO roots
                FROM jsonb_array_elements(operation.proposal->'updates') item WHERE item->'new'->>'kind'='oid';
            IF NEW.generation<>operation.generation OR NEW.object_format<>operation.object_format
               OR NEW.roots IS DISTINCT FROM roots THEN
                RAISE EXCEPTION 'invalid_product_attempt_binding' USING ERRCODE='22023';
            END IF;
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER version_product_attempt_pin_fence BEFORE INSERT OR UPDATE ON public.version_object_pins
    FOR EACH ROW EXECUTE FUNCTION public._version_fence_product_attempt_pin();

CREATE FUNCTION public._version_fence_product_attempt_admission()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE attempt public.version_product_publication_attempts%ROWTYPE; state text;
BEGIN
    SELECT * INTO attempt FROM public.version_product_publication_attempts WHERE pin_id=NEW.pin_id FOR SHARE;
    IF FOUND THEN
        SELECT p.state INTO state FROM public.version_object_pins p WHERE p.id=NEW.pin_id FOR SHARE;
        IF NOT attempt.active OR attempt.project_id<>NEW.project_id OR attempt.actor<>NEW.actor
           OR (state IS DISTINCT FROM 'verified' AND (attempt.lease_id IS DISTINCT FROM NEW.lease_id
                                                     OR attempt.holder_id IS DISTINCT FROM NEW.holder_id)) THEN
            RAISE EXCEPTION 'product_attempt_admission_mismatch' USING ERRCODE='55000';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER version_product_attempt_admission_fence BEFORE INSERT OR UPDATE ON public.version_publication_admissions
    FOR EACH ROW EXECUTE FUNCTION public._version_fence_product_attempt_admission();

CREATE FUNCTION public.open_admitted_version_product_attempt(
    p_project_id text,p_actor text,p_request_key uuid,p_input_sha256 text,p_generation bigint,
    p_attempt_id uuid,p_lease_id uuid,p_holder_id text
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE project public.projects%ROWTYPE; repo public.version_repositories%ROWTYPE;
    operation public.version_product_operations%ROWTYPE; attempt public.version_product_publication_attempts%ROWTYPE;
    previous public.version_product_publication_attempts%ROWTYPE; pin public.version_object_pins%ROWTYPE;
    admission public.version_publication_admissions%ROWTYPE; lease public.project_write_leases%ROWTYPE;
    result jsonb; candidate jsonb; expected_roots jsonb;
    previous_pin uuid; selected_pin uuid; digest text; previous_lease uuid; previous_holder text;
BEGIN
    project:=public._version_lock_admission_project(p_project_id);
    PERFORM public._version_assert_current_repository_actor(p_project_id,project.org_id,p_actor,false);
    SELECT * INTO operation FROM public.version_product_operations
        WHERE project_id=p_project_id AND actor=p_actor AND request_key=p_request_key FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'product_operation_unavailable' USING ERRCODE='55000'; END IF;
    IF operation.input_sha256 IS DISTINCT FROM p_input_sha256 OR operation.generation IS DISTINCT FROM p_generation THEN
        RAISE EXCEPTION 'request_key_reused' USING ERRCODE='22023';
    END IF;
    result:=public._version_product_operation_result(operation);
    IF result IS NOT NULL THEN
        PERFORM public._version_assert_current_repository_actor(p_project_id,project.org_id,p_actor,false);
        RETURN to_jsonb(operation)||jsonb_build_object('result',result);
    END IF;
    PERFORM public._version_assert_current_repository_actor(p_project_id,project.org_id,p_actor,true);
    PERFORM public._version_assert_write_lease(p_project_id,p_lease_id,p_holder_id);
    repo:=public._version_lock_native_repository(p_project_id);
    IF repo.generation<>operation.generation OR repo.object_format<>operation.object_format THEN
        RAISE EXCEPTION 'generation_mismatch' USING ERRCODE='55000';
    END IF;
    IF operation.proposal IS NULL OR p_attempt_id IS NULL
       OR p_attempt_id::text=operation.proposal->>'receipt_id' THEN
        RAISE EXCEPTION 'invalid_product_attempt' USING ERRCODE='22023';
    END IF;
    candidate:=operation.proposal;
    IF candidate->>'receipt_id' IS NULL THEN
        -- A check-only operation has no physical attempt to allocate or rotate.
        PERFORM public._version_assert_current_repository_actor(p_project_id,project.org_id,p_actor,true);
        PERFORM public._version_assert_write_lease(p_project_id,p_lease_id,p_holder_id);
        RETURN to_jsonb(operation)||jsonb_build_object('result',NULL);
    END IF;
    SELECT jsonb_object_agg(item->'new'->>'oid','commit') INTO expected_roots
        FROM jsonb_array_elements(candidate->'updates') item WHERE item->'new'->>'kind'='oid';
    IF expected_roots IS NULL OR candidate->'product_result'->>'commit_oid' IS NULL
       OR expected_roots<>jsonb_build_object(candidate->'product_result'->>'commit_oid','commit') THEN
        RAISE EXCEPTION 'invalid_product_attempt' USING ERRCODE='22023';
    END IF;

    SELECT * INTO attempt FROM public.version_product_publication_attempts WHERE pin_id=p_attempt_id FOR UPDATE;
    IF FOUND THEN
        IF attempt.project_id<>p_project_id OR attempt.actor<>p_actor OR attempt.request_key<>p_request_key
           OR attempt.lease_id IS DISTINCT FROM p_lease_id OR attempt.holder_id IS DISTINCT FROM p_holder_id THEN
            RAISE EXCEPTION 'product_attempt_key_reused' USING ERRCODE='22023';
        END IF;
        IF NOT attempt.active THEN RAISE EXCEPTION 'product_attempt_superseded' USING ERRCODE='55000'; END IF;
        selected_pin:=attempt.pin_id;
    ELSE
        IF EXISTS(SELECT 1 FROM public.version_object_pins WHERE id=p_attempt_id) THEN
            RAISE EXCEPTION 'product_attempt_key_reused' USING ERRCODE='22023';
        END IF;
        SELECT * INTO previous FROM public.version_product_publication_attempts
            WHERE project_id=p_project_id AND actor=p_actor AND request_key=p_request_key AND active FOR UPDATE;
        previous_pin:=coalesce(previous.pin_id,(candidate->>'receipt_id')::uuid);
        previous_lease:=previous.lease_id; previous_holder:=previous.holder_id;
        SELECT * INTO pin FROM public.version_object_pins WHERE id=previous_pin FOR UPDATE;
        IF FOUND THEN
            IF pin.project_id<>p_project_id OR pin.actor<>p_actor OR pin.object_format<>operation.object_format
               OR pin.generation<>operation.generation OR pin.roots<>expected_roots THEN
                RAISE EXCEPTION 'invalid_product_attempt_binding' USING ERRCODE='22023';
            END IF;
            IF pin.state='verified' AND pin.gc_epoch=repo.gc_epoch AND pin.expires_at>clock_timestamp()
               AND EXISTS(SELECT 1 FROM public.version_publication_receipts r WHERE r.id=pin.id
                   AND r.project_id=p_project_id AND r.generation=repo.generation AND r.gc_epoch=repo.gc_epoch
                   AND r.object_format=repo.object_format AND r.roots=expected_roots
                   AND r.expires_at>clock_timestamp()) THEN
                selected_pin:=pin.id; -- Verification will use fresh canonical bytes, never resume PUT.
            ELSE
                SELECT * INTO admission FROM public.version_publication_admissions WHERE pin_id=pin.id FOR SHARE;
                IF FOUND THEN previous_lease:=admission.lease_id; previous_holder:=admission.holder_id; END IF;
            END IF;
        END IF;
        IF selected_pin IS NULL THEN
            IF previous_lease IS DISTINCT FROM p_lease_id THEN
                SELECT * INTO lease FROM public.project_write_leases WHERE id=previous_lease FOR SHARE;
                IF FOUND AND lease.project_id=p_project_id AND lease.holder_id=previous_holder
                   AND lease.expires_at>clock_timestamp() THEN
                    RAISE EXCEPTION 'product_operation_busy' USING ERRCODE='55000';
                END IF;
            END IF;
            -- Register a tombstone even when the original fixed pin never started.
            -- No uncertainty, capacity, object location or remote-I/O inventory is settled.
            IF previous.pin_id IS NULL THEN
                INSERT INTO public.version_product_publication_attempts
                    (pin_id,project_id,actor,request_key,native_request_sha256,active,initial_tombstone)
                VALUES(previous_pin,p_project_id,p_actor,p_request_key,operation.native_request_sha256,false,true);
            ELSE
                UPDATE public.version_product_publication_attempts SET active=false WHERE pin_id=previous.pin_id;
            END IF;
            PERFORM public.release_version_object_publication(p_project_id,p_actor,previous_pin);
            selected_pin:=p_attempt_id;
            candidate:=candidate||jsonb_build_object('receipt_id',selected_pin);
            digest:=public._version_product_ref_request_sha256(operation.generation,operation.object_format,candidate);
            INSERT INTO public.version_product_publication_attempts
                (pin_id,project_id,actor,request_key,native_request_sha256,active,lease_id,holder_id)
            VALUES(selected_pin,p_project_id,p_actor,p_request_key,digest,true,p_lease_id,p_holder_id);
        END IF;
    END IF;
    candidate:=operation.proposal||jsonb_build_object('receipt_id',selected_pin);
    digest:=public._version_product_ref_request_sha256(operation.generation,operation.object_format,candidate);
    -- Locks waited on above never extend actor/lease/wall-clock authority.
    PERFORM public._version_assert_current_repository_actor(p_project_id,project.org_id,p_actor,true);
    PERFORM public._version_assert_write_lease(p_project_id,p_lease_id,p_holder_id);
    RETURN to_jsonb(operation)||jsonb_build_object('proposal',candidate,'native_request_sha256',digest,'result',NULL);
END $$;

REVOKE ALL ON FUNCTION public._version_product_operation_result(public.version_product_operations),
    public._version_fence_product_operation_result(),public._version_fence_product_attempt_pin(),
    public._version_fence_product_attempt_admission(),
    public.open_admitted_version_product_attempt(text,text,uuid,text,bigint,uuid,uuid,text)
    FROM PUBLIC,anon,authenticated,service_role;
GRANT EXECUTE ON FUNCTION public.open_admitted_version_product_attempt(text,text,uuid,text,bigint,uuid,uuid,text) TO service_role;
NOTIFY pgrst,'reload schema';
COMMIT;
