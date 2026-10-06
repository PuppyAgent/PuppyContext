-- New empty repositories only. No enrollment or conversion of existing Projects.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='2min';

ALTER TABLE public.version_repository_billing ADD COLUMN accounted_bytes bigint
    CHECK (accounted_bytes >= 0);
COMMENT ON COLUMN public.version_repository_billing.accounted_bytes IS
    'Per-Project acknowledged logical usage; NULL requires explicit baseline reconciliation. New empty native repositories start at zero.';

CREATE FUNCTION public.create_native_project_repository(
    p_operation_key text, p_payload_hash text, p_project_id text, p_name text,
    p_description text, p_org_id text, p_created_by uuid, p_share_token text,
    p_publication_mode text, p_project_limit integer DEFAULT NULL,
    p_request_hash text DEFAULT NULL, p_result_metadata jsonb DEFAULT '{}'::jsonb,
    p_object_format text DEFAULT 'sha1', p_default_branch text DEFAULT 'main'
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE outcome jsonb; project_row public.projects%ROWTYPE; head bytea;
BEGIN
    head:=convert_to('refs/heads/'||p_default_branch,'UTF8');
    IF p_publication_mode IS DISTINCT FROM 'empty'
       OR p_object_format IS NULL OR p_object_format NOT IN ('sha1','sha256')
       OR p_default_branch IS NULL OR p_default_branch='HEAD' OR left(p_default_branch,1)='-'
       OR NOT public._version_ref_name_valid(head) THEN
        RETURN jsonb_build_object('outcome','invalid');
    END IF;
    -- The existing coordinator owns membership, quota, operation identity and
    -- creator Admin. This wrapper publishes the empty native aggregate in the
    -- SAME transaction: a crash cannot leave a legacy-initializable Project.
    PERFORM pg_advisory_xact_lock(hashtextextended(p_org_id,0));
    PERFORM pg_advisory_xact_lock(hashtextextended(p_org_id || ':storage.logical_bytes',0));
    outcome:=public.create_project_idempotent(
        p_operation_key,p_payload_hash,p_project_id,p_name,p_description,p_org_id,
        p_created_by,p_share_token,p_publication_mode,p_project_limit,p_request_hash,p_result_metadata);
    IF outcome->>'outcome' IS DISTINCT FROM 'initializing_created' THEN RETURN outcome; END IF;

    -- Never manufacture billing entitlements or reset an existing counter.
    -- An absent nonempty legacy baseline requires the existing reconciler.
    PERFORM public._version_file_policy_limit(p_org_id);
    IF NOT EXISTS (SELECT 1 FROM public.organization_usage_counters
                   WHERE org_id=p_org_id AND metric='storage.logical_bytes' AND version>0) THEN
        IF EXISTS(SELECT 1 FROM public.projects WHERE org_id=p_org_id AND id<>p_project_id
                  AND coalesce(version_root_hash,'') NOT IN ('','4b825dc642cb6eb9a060e54bf8d69288fbee4904'))
           OR EXISTS(SELECT 1 FROM public.version_repository_billing WHERE org_id=p_org_id) THEN
            RAISE EXCEPTION 'storage_billing_usage_uninitialized' USING ERRCODE='55000';
        END IF;
        INSERT INTO public.organization_usage_counters(org_id,metric,value,version)
            VALUES(p_org_id,'storage.logical_bytes',0,1);
    END IF;
    INSERT INTO public.version_organization_capacity(org_id,initialized,max_body_bytes,max_objects)
        VALUES(p_org_id,true,34359738368,4000000) ON CONFLICT(org_id) DO NOTHING;
    IF NOT EXISTS(SELECT 1 FROM public.version_organization_capacity WHERE org_id=p_org_id AND initialized) THEN
        RAISE EXCEPTION 'repository_capacity_uninitialized' USING ERRCODE='55000';
    END IF;
    INSERT INTO public.version_repositories(project_id,authority,object_format)
        VALUES(p_project_id,'native',p_object_format);
    INSERT INTO public.version_repository_refs(project_id,name,object_format,symbolic_target)
        VALUES(p_project_id,convert_to('HEAD','UTF8'),p_object_format,head);
    INSERT INTO public.version_repository_capacity(project_id,org_id,initialized,max_body_bytes,max_objects)
        VALUES(p_project_id,p_org_id,true,268435456,100000);
    INSERT INTO public.version_repository_billing(project_id,org_id,initialized,accounted_bytes) VALUES(p_project_id,p_org_id,true,0);
    INSERT INTO public.version_repository_file_policies(project_id,initialized) VALUES(p_project_id,true);
    UPDATE public.projects SET lifecycle_status='ready',bound_git_branch=p_default_branch,updated_at=now()
        WHERE id=p_project_id AND lifecycle_status='initializing' RETURNING * INTO project_row;
    IF NOT FOUND THEN RAISE EXCEPTION 'native_creation_lifecycle_conflict'; END IF;
    UPDATE public.project_create_operations SET status='ready',project_snapshot=to_jsonb(project_row),
        ready_at=now(),initialization_claimed_at=NULL,initialization_claimed_by=NULL,
        initialization_last_error=NULL
        WHERE actor_user_id=p_created_by AND operation_key=p_operation_key;
    RETURN jsonb_build_object('outcome','native_created','project',to_jsonb(project_row));
END $$;

-- Defense at persistence prevents disabled Scope resources from being created
-- through another management entrypoint. This never widens a Scope grant.
CREATE FUNCTION public._reject_native_scope_configuration()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
    IF EXISTS(SELECT 1 FROM public.version_repositories WHERE project_id=NEW.project_id AND authority='native') THEN
        RAISE EXCEPTION 'native_scope_not_supported' USING ERRCODE='0A000';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER native_scope_configuration_guard BEFORE INSERT OR UPDATE ON public.repository_scopes
    FOR EACH ROW EXECUTE FUNCTION public._reject_native_scope_configuration();

REVOKE ALL ON FUNCTION public.create_native_project_repository(text,text,text,text,text,text,uuid,text,text,integer,text,jsonb,text,text),
    public._reject_native_scope_configuration() FROM PUBLIC,anon,authenticated,service_role;
GRANT EXECUTE ON FUNCTION public.create_native_project_repository(text,text,text,text,text,text,uuid,text,text,integer,text,jsonb,text,text)
    TO service_role;
NOTIFY pgrst,'reload schema';
COMMIT;
