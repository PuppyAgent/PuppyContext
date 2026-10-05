-- Empty Expand: retry-stable Product intent/candidate metadata only.
-- No enrollment, backfill, object I/O, quota initialization or publication.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '2min';

CREATE TABLE public.version_product_operations (
    project_id text NOT NULL,
    actor text NOT NULL CHECK (length(actor) BETWEEN 1 AND 512),
    request_key uuid NOT NULL,
    input_sha256 text NOT NULL CHECK (input_sha256 ~ '^[0-9a-f]{64}$'),
    object_format text NOT NULL CHECK (object_format IN ('sha1','sha256')),
    generation bigint NOT NULL CHECK (generation > 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    proposal jsonb,
    native_request_sha256 text CHECK (native_request_sha256 ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (project_id,actor,request_key),
    CHECK ((proposal IS NULL) = (native_request_sha256 IS NULL)),
    CHECK (proposal IS NULL OR (jsonb_typeof(proposal)='object' AND octet_length(proposal::text)<=65536))
);
COMMENT ON TABLE public.version_product_operations IS
    'Product intent metadata, not current-tree/publication/read or I/O-settlement authority. No Project cascade; pending metadata is not an acknowledged object root.';

CREATE FUNCTION public._version_product_ref_request_sha256(p_generation bigint,p_format text,p_proposal jsonb)
RETURNS text LANGUAGE plpgsql IMMUTABLE SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE item jsonb; name bytea; names bytea[] := ARRAY[]::bytea[]; receipt uuid;
BEGIN
    IF jsonb_typeof(p_proposal) IS DISTINCT FROM 'object'
       OR octet_length(p_proposal::text)>65536
       OR NOT p_proposal ?& ARRAY['updates','receipt_id','message']
       OR (p_proposal-ARRAY['updates','receipt_id','message','product_result'])<>'{}'::jsonb
       OR jsonb_typeof(p_proposal->'updates') IS DISTINCT FROM 'array'
       OR jsonb_typeof(p_proposal->'message') IS DISTINCT FROM 'string'
       OR octet_length(p_proposal->>'message')>8192 THEN
        RAISE EXCEPTION 'invalid_product_proposal' USING ERRCODE='22023';
    END IF;
    IF p_proposal ? 'product_result' THEN
        IF jsonb_typeof(p_proposal->'product_result') IS DISTINCT FROM 'object'
           OR NOT (p_proposal->'product_result') ?& ARRAY['tree_oid','commit_oid','changes']
           OR ((p_proposal->'product_result')-ARRAY['tree_oid','commit_oid','changes'])<>'{}'::jsonb
           OR jsonb_typeof(p_proposal->'product_result'->'tree_oid') IS DISTINCT FROM 'string'
           OR public._version_oid_valid(p_proposal->'product_result'->>'tree_oid',p_format) IS NOT TRUE
           OR jsonb_typeof(p_proposal->'product_result'->'commit_oid') NOT IN ('string','null')
           OR ((p_proposal->'product_result'->>'commit_oid') IS NOT NULL
               AND NOT public._version_oid_valid(p_proposal->'product_result'->>'commit_oid',p_format))
           OR jsonb_typeof(p_proposal->'product_result'->'changes') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'invalid_product_proposal' USING ERRCODE='22023';
        END IF;
        FOR item IN SELECT value FROM jsonb_array_elements(p_proposal->'product_result'->'changes') LOOP
            IF jsonb_typeof(item) IS DISTINCT FROM 'array' THEN
                RAISE EXCEPTION 'invalid_product_proposal' USING ERRCODE='22023';
            END IF;
            IF jsonb_array_length(item)<>2 OR (item->>0 IN ('add','update','delete')) IS NOT TRUE
               OR jsonb_typeof(item->1) IS DISTINCT FROM 'string' THEN
                RAISE EXCEPTION 'invalid_product_proposal' USING ERRCODE='22023';
            END IF;
            BEGIN name := decode(item->>1,'base64');
            EXCEPTION WHEN invalid_parameter_value OR invalid_text_representation THEN
                RAISE EXCEPTION 'invalid_product_proposal' USING ERRCODE='22023';
            END;
            IF item->>1 IS DISTINCT FROM replace(encode(name,'base64'),E'\n','') THEN
                RAISE EXCEPTION 'invalid_product_proposal' USING ERRCODE='22023';
            END IF;
        END LOOP;
    END IF;
    IF jsonb_array_length(p_proposal->'updates') NOT BETWEEN 1 AND 256 THEN
        RAISE EXCEPTION 'invalid_product_proposal' USING ERRCODE='22023';
    END IF;
    BEGIN receipt := (p_proposal->>'receipt_id')::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RAISE EXCEPTION 'invalid_product_proposal' USING ERRCODE='22023';
    END;
    IF p_proposal->'receipt_id' IS DISTINCT FROM coalesce(to_jsonb(receipt::text),'null'::jsonb) THEN
        RAISE EXCEPTION 'invalid_product_proposal' USING ERRCODE='22023';
    END IF;
    FOR item IN SELECT value FROM jsonb_array_elements(p_proposal->'updates') LOOP
        IF jsonb_typeof(item) IS DISTINCT FROM 'object'
           OR NOT item ?& ARRAY['name_b64','expected']
           OR (item-ARRAY['name_b64','expected','new'])<>'{}'::jsonb
           OR NOT public._version_ref_state_valid(item->'expected',p_format)
           OR (item ? 'new' AND NOT public._version_ref_state_valid(item->'new',p_format)) THEN
            RAISE EXCEPTION 'invalid_product_proposal' USING ERRCODE='22023';
        END IF;
        BEGIN name := decode(item->>'name_b64','base64');
        EXCEPTION WHEN invalid_parameter_value OR invalid_text_representation THEN
            RAISE EXCEPTION 'invalid_product_proposal' USING ERRCODE='22023';
        END;
        IF name IS NULL OR NOT public._version_ref_name_valid(name)
           OR item->'name_b64' <> to_jsonb(replace(encode(name,'base64'),E'\n',''))
           OR name=ANY(names) THEN
            RAISE EXCEPTION 'invalid_product_proposal' USING ERRCODE='22023';
        END IF;
        names := array_append(names,name);
    END LOOP;
    -- Exactly the existing ref transaction digest, not a replacement authority.
    RETURN encode(sha256(convert_to(jsonb_build_object(
        'generation',p_generation,'updates',p_proposal->'updates',
        'receipt',receipt,'message',p_proposal->>'message')::text,'UTF8')),'hex');
END $$;

CREATE FUNCTION public._version_product_operation_result(p_operation public.version_product_operations)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE published public.version_ref_transactions%ROWTYPE;
BEGIN
    SELECT * INTO published FROM public.version_ref_transactions
    WHERE project_id=p_operation.project_id AND actor=p_operation.actor AND request_key=p_operation.request_key;
    IF NOT FOUND THEN RETURN NULL; END IF;
    IF p_operation.native_request_sha256 IS NULL
       OR published.request_sha256 IS DISTINCT FROM p_operation.native_request_sha256 THEN
        RAISE EXCEPTION 'request_key_reused' USING ERRCODE='22023';
    END IF;
    RETURN published.result;
END $$;

CREATE FUNCTION public.read_admitted_version_product_operation(
    p_project_id text,p_actor text,p_request_key uuid,p_input_sha256 text,p_generation bigint
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE project public.projects%ROWTYPE; operation public.version_product_operations%ROWTYPE; result jsonb;
BEGIN
    project := public._version_lock_admission_project(p_project_id);
    PERFORM public._version_assert_current_repository_actor(p_project_id,project.org_id,p_actor,false);
    SELECT * INTO operation FROM public.version_product_operations
        WHERE project_id=p_project_id AND actor=p_actor AND request_key=p_request_key FOR SHARE;
    IF NOT FOUND THEN RETURN NULL; END IF;
    IF operation.input_sha256 IS DISTINCT FROM p_input_sha256 OR operation.generation IS DISTINCT FROM p_generation THEN
        RAISE EXCEPTION 'request_key_reused' USING ERRCODE='22023';
    END IF;
    result := public._version_product_operation_result(operation);
    PERFORM public._version_assert_current_repository_actor(p_project_id,project.org_id,p_actor,false);
    RETURN to_jsonb(operation)||jsonb_build_object('result',result);
END $$;

CREATE FUNCTION public.begin_admitted_version_product_operation(
    p_project_id text,p_actor text,p_request_key uuid,p_input_sha256 text,p_generation bigint,
    p_lease_id uuid,p_holder_id text
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE project public.projects%ROWTYPE; repo public.version_repositories%ROWTYPE;
    operation public.version_product_operations%ROWTYPE; existing boolean; result jsonb;
BEGIN
    IF p_request_key IS NULL OR p_input_sha256 IS NULL OR p_input_sha256 !~ '^[0-9a-f]{64}$'
       OR p_generation IS NULL OR p_generation<1 OR p_actor IS NULL OR length(p_actor) NOT BETWEEN 1 AND 512 THEN
        RAISE EXCEPTION 'invalid_product_operation' USING ERRCODE='22023';
    END IF;
    project := public._version_lock_admission_project(p_project_id);
    PERFORM public._version_assert_current_repository_actor(p_project_id,project.org_id,p_actor,false);
    SELECT * INTO operation FROM public.version_product_operations
        WHERE project_id=p_project_id AND actor=p_actor AND request_key=p_request_key FOR UPDATE;
    existing := FOUND;
    IF existing THEN
        IF operation.input_sha256<>p_input_sha256 OR operation.generation<>p_generation THEN
            RAISE EXCEPTION 'request_key_reused' USING ERRCODE='22023';
        END IF;
        result := public._version_product_operation_result(operation);
        IF result IS NOT NULL THEN
            PERFORM public._version_assert_current_repository_actor(p_project_id,project.org_id,p_actor,false);
            RETURN to_jsonb(operation)||jsonb_build_object('result',result);
        END IF;
    ELSIF EXISTS (SELECT 1 FROM public.version_ref_transactions
                  WHERE project_id=p_project_id AND actor=p_actor AND request_key=p_request_key) THEN
        RAISE EXCEPTION 'request_key_reused' USING ERRCODE='22023';
    END IF;
    PERFORM public._version_assert_current_repository_actor(p_project_id,project.org_id,p_actor,true);
    PERFORM public._version_assert_write_lease(p_project_id,p_lease_id,p_holder_id);
    repo := public._version_lock_native_repository(p_project_id);
    IF repo.generation<>p_generation OR (existing AND operation.object_format<>repo.object_format) THEN
        RAISE EXCEPTION 'generation_mismatch' USING ERRCODE='55000';
    END IF;
    IF NOT existing THEN
        INSERT INTO public.version_product_operations(project_id,actor,request_key,input_sha256,object_format,generation)
        VALUES(p_project_id,p_actor,p_request_key,p_input_sha256,repo.object_format,repo.generation)
        RETURNING * INTO operation;
    END IF;
    -- Wall-clock validity after all lock waits; failed checks roll back insertion.
    PERFORM public._version_assert_current_repository_actor(p_project_id,project.org_id,p_actor,true);
    PERFORM public._version_assert_write_lease(p_project_id,p_lease_id,p_holder_id);
    RETURN to_jsonb(operation)||jsonb_build_object('result',NULL);
END $$;

CREATE FUNCTION public.prepare_admitted_version_product_operation(
    p_project_id text,p_actor text,p_request_key uuid,p_input_sha256 text,p_proposal jsonb,
    p_lease_id uuid,p_holder_id text
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE project public.projects%ROWTYPE; repo public.version_repositories%ROWTYPE;
    operation public.version_product_operations%ROWTYPE; result jsonb; request_hash text;
BEGIN
    project := public._version_lock_admission_project(p_project_id);
    PERFORM public._version_assert_current_repository_actor(p_project_id,project.org_id,p_actor,false);
    SELECT * INTO operation FROM public.version_product_operations
        WHERE project_id=p_project_id AND actor=p_actor AND request_key=p_request_key FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'product_operation_unavailable' USING ERRCODE='55000'; END IF;
    IF operation.input_sha256 IS DISTINCT FROM p_input_sha256
       OR (operation.proposal IS NOT NULL AND operation.proposal IS DISTINCT FROM p_proposal) THEN
        RAISE EXCEPTION 'request_key_reused' USING ERRCODE='22023';
    END IF;
    result := public._version_product_operation_result(operation);
    IF result IS NOT NULL THEN
        PERFORM public._version_assert_current_repository_actor(p_project_id,project.org_id,p_actor,false);
        RETURN to_jsonb(operation)||jsonb_build_object('result',result);
    END IF;
    PERFORM public._version_assert_current_repository_actor(p_project_id,project.org_id,p_actor,true);
    PERFORM public._version_assert_write_lease(p_project_id,p_lease_id,p_holder_id);
    repo := public._version_lock_native_repository(p_project_id);
    IF repo.generation<>operation.generation OR repo.object_format<>operation.object_format THEN
        RAISE EXCEPTION 'generation_mismatch' USING ERRCODE='55000';
    END IF;
    request_hash := public._version_product_ref_request_sha256(operation.generation,operation.object_format,p_proposal);
    IF operation.proposal IS NULL THEN
        UPDATE public.version_product_operations SET proposal=p_proposal,native_request_sha256=request_hash
        WHERE project_id=p_project_id AND actor=p_actor AND request_key=p_request_key
        RETURNING * INTO operation;
    END IF;
    PERFORM public._version_assert_current_repository_actor(p_project_id,project.org_id,p_actor,true);
    PERFORM public._version_assert_write_lease(p_project_id,p_lease_id,p_holder_id);
    RETURN to_jsonb(operation)||jsonb_build_object('result',NULL);
END $$;

CREATE FUNCTION public._version_fence_product_operation_result()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE operation public.version_product_operations%ROWTYPE;
BEGIN
    -- Publication already holds the Project lock. This guard rolls back ALL
    -- ref/audit/outbox/usage effects if a producer loses its prepared binding.
    SELECT * INTO operation FROM public.version_product_operations
        WHERE project_id=NEW.project_id AND actor=NEW.actor AND request_key=NEW.request_key FOR UPDATE;
    IF FOUND AND (operation.native_request_sha256 IS NULL
                  OR operation.native_request_sha256 IS DISTINCT FROM NEW.request_sha256) THEN
        RAISE EXCEPTION 'request_key_reused' USING ERRCODE='22023';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER version_product_operation_result_fence BEFORE INSERT ON public.version_ref_transactions
    FOR EACH ROW EXECUTE FUNCTION public._version_fence_product_operation_result();

CREATE FUNCTION public._version_attach_product_event()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE product jsonb;
BEGIN
    SELECT operation.proposal->'product_result' INTO product
    FROM public.version_ref_transactions published
    JOIN public.version_product_operations operation
      ON operation.project_id=published.project_id AND operation.actor=published.actor
      AND operation.request_key=published.request_key
    WHERE published.id=NEW.transaction_id AND published.project_id=NEW.project_id;
    IF product IS NOT NULL THEN
        NEW.payload := NEW.payload||jsonb_build_object('product',product);
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER version_product_event BEFORE INSERT ON public.version_ref_events
    FOR EACH ROW EXECUTE FUNCTION public._version_attach_product_event();

ALTER TABLE public.version_product_operations ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.version_product_operations FROM PUBLIC,anon,authenticated,service_role;
GRANT SELECT ON TABLE public.version_product_operations TO service_role;
REVOKE ALL ON FUNCTION public._version_attach_product_event(),
    public.read_admitted_version_product_operation(text,text,uuid,text,bigint),
    public._version_fence_product_operation_result(),
    public._version_product_ref_request_sha256(bigint,text,jsonb),
    public._version_product_operation_result(public.version_product_operations),
    public.begin_admitted_version_product_operation(text,text,uuid,text,bigint,uuid,text),
    public.prepare_admitted_version_product_operation(text,text,uuid,text,jsonb,uuid,text)
    FROM PUBLIC,anon,authenticated,service_role;
GRANT EXECUTE ON FUNCTION public.read_admitted_version_product_operation(text,text,uuid,text,bigint),
    public.begin_admitted_version_product_operation(text,text,uuid,text,bigint,uuid,text),
    public.prepare_admitted_version_product_operation(text,text,uuid,text,jsonb,uuid,text) TO service_role;
NOTIFY pgrst, 'reload schema';
COMMIT;
