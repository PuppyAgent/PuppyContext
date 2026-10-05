-- ISSUE-062: explicit full-repository file-policy enrollment and current I/O
-- admission. Expand only; no enrollment, activation, backfill or external I/O.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='2min';
CREATE TABLE public.version_repository_file_policies (
    project_id text PRIMARY KEY REFERENCES public.version_repositories(project_id) ON DELETE CASCADE,
    initialized boolean NOT NULL DEFAULT false,
    FOREIGN KEY(project_id) REFERENCES public.version_repository_capacity(project_id),
    FOREIGN KEY(project_id) REFERENCES public.version_repository_billing(project_id)
);
CREATE TABLE public.version_repository_file_authorizations (
    project_id text NOT NULL, actor text NOT NULL, request_key uuid NOT NULL, transaction_xid xid8 NOT NULL,
    PRIMARY KEY(project_id,actor,request_key),
    FOREIGN KEY(project_id,actor,request_key) REFERENCES public.version_ref_transactions(project_id,actor,request_key)
        ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE public.version_publication_admissions (
    pin_id uuid PRIMARY KEY REFERENCES public.version_object_pins(id) ON DELETE CASCADE,
    project_id text NOT NULL REFERENCES public.version_repositories(project_id) ON DELETE CASCADE,
    actor text NOT NULL, lease_id uuid NOT NULL, holder_id text NOT NULL
);
ALTER TABLE public.version_repository_file_policies ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.version_repository_file_authorizations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.version_publication_admissions ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.version_repository_file_policies,public.version_repository_file_authorizations,
    public.version_publication_admissions FROM PUBLIC,anon,authenticated,service_role;
GRANT SELECT ON public.version_repository_file_policies,public.version_repository_file_authorizations,
    public.version_publication_admissions TO service_role;

CREATE FUNCTION public._version_file_policy_limit(p_org_id text,p_revision bigint DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE context jsonb; quota jsonb; maximum bigint;
BEGIN
    context:=public._version_billing_entitlement(p_org_id,p_revision);
    SELECT entitlements->'limits'->'upload.max_single_file_bytes' INTO quota
        FROM public.organization_entitlements WHERE org_id=p_org_id;
    IF jsonb_typeof(quota)='null' THEN maximum:=NULL;
    ELSIF jsonb_typeof(quota)='number' AND quota#>>'{}' ~ '^[0-9]+$' THEN maximum:=(quota#>>'{}')::bigint;
    ELSE RAISE EXCEPTION 'repository_file_policy_unavailable' USING ERRCODE='55000'; END IF;
    RETURN jsonb_build_object('org_id',p_org_id,'source_revision',context->'source_revision','file_limit',maximum);
END $$;

CREATE FUNCTION public.check_version_repository_file_policy(p_project_id text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE org text;
BEGIN
    org:=public._version_lock_billing_organization(p_project_id);
    PERFORM 1 FROM public.version_repository_file_policies WHERE project_id=p_project_id AND initialized FOR SHARE;
    IF NOT FOUND THEN RAISE EXCEPTION 'repository_file_policy_uninitialized'; END IF;
    PERFORM public._version_billing_usage(p_project_id,org);
    PERFORM public.check_version_repository_capacity(p_project_id);
    RETURN public._version_file_policy_limit(org)||jsonb_build_object('project_id',p_project_id);
END $$;

CREATE FUNCTION public.begin_admitted_version_object_publication(
    p_project_id text,p_actor text,p_pin_id uuid,p_generation bigint,p_roots jsonb,p_lease_id uuid,p_holder_id text
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE result jsonb;
BEGIN
    PERFORM public.check_version_repository_write_admission(p_project_id,p_actor,p_lease_id,p_holder_id);
    result:=public.begin_version_object_publication(p_project_id,p_actor,p_pin_id,p_generation,p_roots);
    INSERT INTO public.version_publication_admissions(pin_id,project_id,actor,lease_id,holder_id)
        VALUES(p_pin_id,p_project_id,p_actor,p_lease_id,p_holder_id)
        ON CONFLICT(pin_id) DO UPDATE SET lease_id=EXCLUDED.lease_id,holder_id=EXCLUDED.holder_id
        WHERE version_publication_admissions.project_id=EXCLUDED.project_id AND version_publication_admissions.actor=EXCLUDED.actor
          AND ((version_publication_admissions.lease_id=EXCLUDED.lease_id AND version_publication_admissions.holder_id=EXCLUDED.holder_id)
               OR EXISTS(SELECT 1 FROM public.version_object_pins WHERE id=p_pin_id AND state='verified'));
    IF NOT FOUND THEN RAISE EXCEPTION 'native_publication_admission_mismatch'; END IF;
    PERFORM public.check_version_repository_write_admission(p_project_id,p_actor,p_lease_id,p_holder_id);
    RETURN result;
END $$;

CREATE FUNCTION public.begin_admitted_version_repository_read(p_project_id text,p_actor text,p_pin_id uuid)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE project public.projects%ROWTYPE; result jsonb;
BEGIN
    project:=public._version_lock_admission_project(p_project_id);
    PERFORM public._version_assert_current_repository_actor(p_project_id,project.org_id,p_actor,false);
    result:=public.begin_version_repository_read(p_project_id,p_actor,p_pin_id);
    PERFORM public._version_assert_current_repository_actor(p_project_id,project.org_id,p_actor,false);
    RETURN result;
END $$;

CREATE FUNCTION public.get_admitted_version_repository_snapshot(p_project_id text,p_actor text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE project public.projects%ROWTYPE; result jsonb;
BEGIN
    project:=public._version_lock_admission_project(p_project_id);
    PERFORM public._version_assert_current_repository_actor(p_project_id,project.org_id,p_actor,false);
    PERFORM public._version_lock_native_repository(p_project_id,false);
    result:=public.get_version_repository_snapshot(p_project_id);
    PERFORM public._version_assert_current_repository_actor(p_project_id,project.org_id,p_actor,false);
    RETURN result;
END $$;

CREATE FUNCTION public._version_assert_native_pin_admission(p_project_id text,p_pin_id uuid)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE admission public.version_publication_admissions%ROWTYPE; org text;
BEGIN
    -- Reservation already holds Organization/Project/repository locks. Neither
    -- a copied ContextVar nor a formerly valid credential extends authority.
    SELECT org_id INTO org FROM public.projects WHERE id=p_project_id;
    SELECT * INTO admission FROM public.version_publication_admissions
        WHERE pin_id=p_pin_id AND project_id=p_project_id FOR SHARE;
    IF NOT FOUND THEN RAISE EXCEPTION 'native_publication_admission_required'; END IF;
    PERFORM public._version_assert_current_repository_actor(p_project_id,org,admission.actor,true);
    PERFORM public._version_assert_write_lease(p_project_id,admission.lease_id,admission.holder_id);
    RETURN org;
END $$;

CREATE FUNCTION public._version_fence_native_io_admission()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE org text;
BEGIN
    IF EXISTS(SELECT 1 FROM public.version_repository_file_policies WHERE project_id=NEW.project_id) THEN
        PERFORM 1 FROM public.version_repository_file_policies WHERE project_id=NEW.project_id AND initialized FOR SHARE;
        IF NOT FOUND THEN RAISE EXCEPTION 'repository_file_policy_uninitialized'; END IF;
        org:=public._version_assert_native_pin_admission(NEW.project_id,NEW.pin_id);
        PERFORM public._version_file_policy_limit(org);
        PERFORM public._version_assert_native_pin_admission(NEW.project_id,NEW.pin_id);
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER version_io_admission_fence BEFORE INSERT ON public.version_repository_capacity_inflight
FOR EACH ROW EXECUTE FUNCTION public._version_fence_native_io_admission();

CREATE FUNCTION public._version_fence_new_blob_size()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE org text; quota jsonb; maximum bigint;
BEGIN
    IF EXISTS(SELECT 1 FROM public.version_repository_file_policies WHERE project_id=NEW.project_id) THEN
        PERFORM 1 FROM public.version_repository_file_policies WHERE project_id=NEW.project_id AND initialized FOR SHARE;
        IF NOT FOUND THEN RAISE EXCEPTION 'repository_file_policy_uninitialized'; END IF;
        org:=public._version_assert_native_pin_admission(NEW.project_id,NEW.admitted_pin_id);
        quota:=public._version_file_policy_limit(org);
        maximum:=(quota->>'file_limit')::bigint;
        IF NEW.object_kind='blob' AND maximum IS NOT NULL AND NEW.body_bytes>maximum
           AND NOT EXISTS(SELECT 1 FROM public.version_repository_object_capacity
               WHERE project_id=NEW.project_id AND object_id=NEW.object_id) THEN
            RAISE EXCEPTION 'file_size_limit_exceeded' USING ERRCODE='P0001';
        END IF;
        PERFORM public._version_assert_native_pin_admission(NEW.project_id,NEW.admitted_pin_id);
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER version_new_blob_size_fence BEFORE INSERT ON public.version_repository_object_capacity
FOR EACH ROW EXECUTE FUNCTION public._version_fence_new_blob_size();

CREATE FUNCTION public._version_fence_file_policy_publication()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
    IF NEW.result->>'status'='committed'
       AND EXISTS(SELECT 1 FROM public.version_repository_file_policies WHERE project_id=NEW.project_id)
       AND NOT EXISTS(SELECT 1 FROM public.version_repository_file_authorizations WHERE project_id=NEW.project_id
           AND actor=NEW.actor AND request_key=NEW.request_key AND transaction_xid=pg_current_xact_id()) THEN
        RAISE EXCEPTION 'repository_file_policy_coordination_required';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER version_file_policy_publication_fence BEFORE INSERT ON public.version_ref_transactions
FOR EACH ROW EXECUTE FUNCTION public._version_fence_file_policy_publication();

CREATE FUNCTION public.apply_policy_version_ref_transaction(
    p_project_id text,p_actor text,p_request_key uuid,p_generation bigint,p_updates jsonb,p_receipt_id uuid,
    p_message text,p_lease_id uuid,p_holder_id text,p_usage jsonb,p_policy jsonb
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE org text; result jsonb; before_oid text; after_oid text; quota jsonb; digest text;
BEGIN
    org:=public._version_lock_billing_organization(p_project_id);
    IF public.get_version_ref_transaction(p_project_id,p_actor,p_request_key) IS NOT NULL THEN
        RETURN public.apply_billed_version_ref_transaction(p_project_id,p_actor,p_request_key,p_generation,
            p_updates,p_receipt_id,p_message,p_lease_id,p_holder_id,p_usage);
    END IF;
    PERFORM 1 FROM public.version_repository_file_policies WHERE project_id=p_project_id AND initialized FOR SHARE;
    IF NOT FOUND THEN RAISE EXCEPTION 'repository_file_policy_uninitialized'; END IF;
    PERFORM public.check_version_repository_capacity(p_project_id);
    before_oid:=public._version_default_head_oid(p_project_id);
    INSERT INTO public.version_repository_file_authorizations(project_id,actor,request_key,transaction_xid)
        VALUES(p_project_id,p_actor,p_request_key,pg_current_xact_id());
    result:=public.apply_billed_version_ref_transaction(p_project_id,p_actor,p_request_key,p_generation,
        p_updates,p_receipt_id,p_message,p_lease_id,p_holder_id,p_usage);
    IF result->>'status'='rejected' THEN RETURN result; END IF;
    IF jsonb_typeof(p_policy) IS DISTINCT FROM 'object' OR p_policy->>'org_id' IS DISTINCT FROM org
       OR jsonb_typeof(p_policy->'source_revision') IS DISTINCT FROM 'number'
       OR p_policy->>'source_revision' !~ '^[1-9][0-9]*$' THEN RAISE EXCEPTION 'invalid_file_policy_proof'; END IF;
    quota:=public._version_file_policy_limit(org,(p_policy->>'source_revision')::bigint);
    IF p_policy->'file_limit' IS DISTINCT FROM quota->'file_limit' THEN RAISE EXCEPTION 'file_policy_projection_changed'; END IF;
    IF result->>'receipt_id' IS NOT NULL THEN
        SELECT manifest_sha256 INTO digest FROM public.version_publication_receipts
            WHERE project_id=p_project_id AND id=p_receipt_id;
        IF digest IS NULL OR digest IS DISTINCT FROM p_policy->>'manifest_sha256' THEN
            RAISE EXCEPTION 'file_policy_manifest_mismatch';
        END IF;
    END IF;
    after_oid:=public._version_default_head_oid(p_project_id);
    IF before_oid IS DISTINCT FROM after_oid AND
       (before_oid IS DISTINCT FROM p_policy->>'old_head_oid' OR after_oid IS DISTINCT FROM p_policy->>'new_head_oid') THEN
        RAISE EXCEPTION 'file_policy_snapshot_changed' USING ERRCODE='40001';
    END IF;
    PERFORM public._version_assert_current_repository_actor(p_project_id,org,p_actor,true);
    PERFORM public._version_assert_write_lease(p_project_id,p_lease_id,p_holder_id);
    RETURN result;
END $$;

REVOKE ALL ON FUNCTION public._version_file_policy_limit(text,bigint),public.check_version_repository_file_policy(text),
    public.begin_admitted_version_object_publication(text,text,uuid,bigint,jsonb,uuid,text),
    public.begin_admitted_version_repository_read(text,text,uuid),public.get_admitted_version_repository_snapshot(text,text),
    public._version_assert_native_pin_admission(text,uuid),
    public._version_fence_native_io_admission(),public._version_fence_new_blob_size(),public._version_fence_file_policy_publication(),
    public.apply_policy_version_ref_transaction(text,text,uuid,bigint,jsonb,uuid,text,uuid,text,jsonb,jsonb)
    FROM PUBLIC,anon,authenticated,service_role;
GRANT EXECUTE ON FUNCTION public.check_version_repository_file_policy(text),
    public.begin_admitted_version_object_publication(text,text,uuid,bigint,jsonb,uuid,text),
    public.begin_admitted_version_repository_read(text,text,uuid),public.get_admitted_version_repository_snapshot(text,text),
    public.apply_policy_version_ref_transaction(text,text,uuid,bigint,jsonb,uuid,text,uuid,text,jsonb,jsonb) TO service_role;
NOTIFY pgrst,'reload schema';
COMMIT;
