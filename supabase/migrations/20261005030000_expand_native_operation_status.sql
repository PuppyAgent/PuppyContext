-- Metadata-only result discovery. This adds no result ledger or write authority.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '2min';

CREATE FUNCTION public.get_admitted_version_operation_status(
    p_project_id text,p_actor text,p_request_key uuid
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE project public.projects%ROWTYPE; operation public.version_product_operations%ROWTYPE;
    published public.version_ref_transactions%ROWTYPE; has_operation boolean; has_result boolean;
    product jsonb; canonical_result jsonb;
BEGIN
    project := public._version_lock_admission_project(p_project_id);
    PERFORM public._version_assert_current_repository_actor(p_project_id,project.org_id,p_actor,false);
    SELECT * INTO operation FROM public.version_product_operations
        WHERE project_id=p_project_id AND actor=p_actor AND request_key=p_request_key FOR SHARE;
    has_operation := FOUND;
    SELECT * INTO published FROM public.version_ref_transactions
        WHERE project_id=p_project_id AND actor=p_actor AND request_key=p_request_key FOR SHARE;
    has_result := FOUND;
    IF has_result THEN
        canonical_result := published.result;
        IF has_operation THEN
            -- Check original preparation OR an admitted physical attempt against
            -- the canonical digest. Preparation is never a substitute result.
            canonical_result := public._version_product_operation_result(operation);
            IF canonical_result->>'project_id' IS DISTINCT FROM p_project_id
               OR canonical_result->'generation' IS DISTINCT FROM to_jsonb(operation.generation) THEN
                RAISE EXCEPTION 'invalid_version_operation_result' USING ERRCODE='55000';
            END IF;
            IF canonical_result->>'status'='committed' THEN
                product := operation.proposal->'product_result';
            END IF;
        END IF;
    END IF;
    -- Credential expiry is wall-clock based, including time spent waiting on
    -- operation/result rows. Check even the absent/pending return paths.
    PERFORM public._version_assert_current_repository_actor(p_project_id,project.org_id,p_actor,false);
    IF NOT has_operation AND NOT has_result THEN RETURN NULL; END IF;
    RETURN jsonb_build_object(
        'project_id',p_project_id,'actor',p_actor,'request_key',p_request_key,
        'status',CASE WHEN has_result THEN canonical_result->>'status' ELSE 'pending' END,
        'input_sha256',CASE WHEN has_operation THEN operation.input_sha256 ELSE NULL END,
        'ref_request_sha256',CASE WHEN has_result THEN published.request_sha256 ELSE NULL END,
        'result',canonical_result,'product',product);
END $$;

REVOKE ALL ON FUNCTION public.get_admitted_version_operation_status(text,text,uuid)
    FROM PUBLIC,anon,authenticated,service_role;
GRANT EXECUTE ON FUNCTION public.get_admitted_version_operation_status(text,text,uuid) TO service_role;
NOTIFY pgrst, 'reload schema';
COMMIT;
