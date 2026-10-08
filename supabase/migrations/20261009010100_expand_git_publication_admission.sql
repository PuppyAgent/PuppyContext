-- Expand: one publication admission round trip. Final checked apply remains
-- authoritative for permissions, leases, policy revisions and ref CAS.
BEGIN;
CREATE FUNCTION public.get_version_publication_context(p_project_id text,p_actor text,p_request_key uuid)
RETURNS jsonb LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE prior jsonb;
BEGIN
 prior:=public.get_version_ref_transaction(p_project_id,p_actor,p_request_key);
 IF prior IS NOT NULL THEN RETURN jsonb_build_object('result',prior); END IF;
 RETURN jsonb_build_object('result',NULL,
   'repository',public.get_version_repository_snapshot(p_project_id),
   'capacity',public.check_version_repository_capacity(p_project_id),
   'billing',public.check_version_repository_billing(p_project_id),
   'policy',public.check_version_repository_file_policy(p_project_id));
END $$;
REVOKE ALL ON FUNCTION public.get_version_publication_context(text,text,uuid) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.get_version_publication_context(text,text,uuid) TO service_role;
-- Reconcile the final bounded closure batch and seal it atomically. Earlier
-- batches (for large histories) use the existing reservation operation.
CREATE FUNCTION public.seal_version_capacity_batch(p_project_id text,p_actor text,p_pin_id uuid,
 p_objects jsonb,p_manifest_sha256 text,p_root_details jsonb)
RETURNS jsonb LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF jsonb_typeof(p_objects)<>'array' OR jsonb_array_length(p_objects)>200 THEN
   RAISE EXCEPTION 'capacity_seal_batch_invalid';
 END IF;
 IF jsonb_array_length(p_objects)>0 THEN
   PERFORM public.reserve_version_object_capacity(p_project_id,p_actor,p_pin_id,p_objects,true,NULL);
 END IF;
 RETURN public.seal_capacity_version_object_publication(p_project_id,p_actor,p_pin_id,p_manifest_sha256,p_root_details);
END $$;
REVOKE ALL ON FUNCTION public.seal_version_capacity_batch(text,text,uuid,jsonb,text,jsonb) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.seal_version_capacity_batch(text,text,uuid,jsonb,text,jsonb) TO service_role;
NOTIFY pgrst,'reload schema';
COMMIT;
