-- Expand only: current-actor and lifecycle checks for admitted native writes.
-- No enrollment, backfill, quota-policy change or external I/O. The lower-level
-- ref primitive remains backend-only; canonical routing still requires complete
-- policy/quota/consumer admission before activation.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '2min';

CREATE FUNCTION public._version_lock_admission_project(p_project_id text)
RETURNS public.projects LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE project public.projects%ROWTYPE; selected_org text;
BEGIN
    SELECT org_id INTO selected_org FROM public.projects WHERE id=p_project_id;
    IF NOT FOUND THEN RAISE EXCEPTION 'repository_unavailable' USING ERRCODE='55000'; END IF;
    PERFORM 1 FROM public.organizations WHERE id=selected_org FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'repository_unavailable' USING ERRCODE='55000'; END IF;
    SELECT * INTO project FROM public.projects WHERE id=p_project_id FOR UPDATE;
    IF NOT FOUND OR project.org_id IS DISTINCT FROM selected_org OR project.lifecycle_status <> 'ready' THEN
        RAISE EXCEPTION 'repository_unavailable' USING ERRCODE='55000';
    END IF;
    RETURN project;
END $$;

CREATE FUNCTION public._version_assert_current_repository_actor(p_project_id text,p_org_id text,p_actor text,p_write boolean)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE subject_user_id uuid; hint public.access_surface_credentials%ROWTYPE;
    credential public.access_surface_credentials%ROWTYPE; allowed boolean; resolved record;
BEGIN
    -- Call only after Organization -> Project locking. Lock membership before
    -- Surface -> credential, matching membership/surface deletion cascades.
    -- The platform's canonical resolvers still decide roles and narrowed modes.
    IF p_actor LIKE 'user:%' THEN
        BEGIN subject_user_id := substring(p_actor FROM 6)::uuid;
        EXCEPTION WHEN invalid_text_representation THEN
            RAISE EXCEPTION 'repository_action_denied' USING ERRCODE='42501';
        END;
    ELSIF p_actor LIKE 'runtime:%' THEN
        SELECT * INTO hint FROM public.access_surface_credentials
            WHERE id=substring(p_actor FROM 9) AND project_id=p_project_id AND org_id=p_org_id;
        IF NOT FOUND THEN RAISE EXCEPTION 'repository_action_denied' USING ERRCODE='42501'; END IF;
        subject_user_id := hint.user_id;
    ELSE
        RAISE EXCEPTION 'repository_action_denied' USING ERRCODE='42501';
    END IF;
    IF subject_user_id IS NOT NULL THEN
        PERFORM 1 FROM public.org_members m WHERE m.org_id=p_org_id AND m.user_id=subject_user_id FOR SHARE;
        PERFORM 1 FROM public.project_members m WHERE m.project_id=p_project_id AND m.user_id=subject_user_id FOR SHARE;
        SELECT s.can_write INTO allowed FROM public.get_version_project_write_state(p_project_id,subject_user_id::text) s;
        IF NOT FOUND OR (p_write AND NOT allowed) THEN
            RAISE EXCEPTION 'repository_action_denied' USING ERRCODE='42501';
        END IF;
    END IF;
    IF p_actor LIKE 'runtime:%' THEN
        PERFORM 1 FROM public.access_surfaces s WHERE s.id=hint.access_surface_id
            AND s.project_id=p_project_id AND s.org_id=p_org_id FOR SHARE;
        IF NOT FOUND THEN RAISE EXCEPTION 'repository_action_denied' USING ERRCODE='42501'; END IF;
        SELECT * INTO credential FROM public.access_surface_credentials WHERE id=hint.id FOR SHARE;
        IF NOT FOUND OR credential.project_id IS DISTINCT FROM p_project_id
           OR credential.org_id IS DISTINCT FROM p_org_id
           OR credential.user_id IS DISTINCT FROM hint.user_id
           OR credential.access_surface_id IS DISTINCT FROM hint.access_surface_id
           OR credential.status <> 'active'
           OR (credential.expires_at IS NOT NULL AND credential.expires_at <= clock_timestamp()) THEN
            RAISE EXCEPTION 'repository_action_denied' USING ERRCODE='42501';
        END IF;
        SELECT * INTO resolved FROM public.resolve_git_runtime_credential(credential.key_hash) r
            WHERE r.credential_id=credential.id;
        IF NOT FOUND OR resolved.project_id IS DISTINCT FROM p_project_id
           OR resolved.org_id IS DISTINCT FROM p_org_id OR resolved.target_kind <> 'project_root'
           OR (p_write AND resolved.effective_mode <> 'rw') THEN
            RAISE EXCEPTION 'repository_action_denied' USING ERRCODE='42501';
        END IF;
    END IF;
END $$;

CREATE FUNCTION public._version_assert_write_lease(p_project_id text,p_lease_id uuid,p_holder_id text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE lease public.project_write_leases%ROWTYPE;
BEGIN
    SELECT * INTO lease FROM public.project_write_leases WHERE id=p_lease_id FOR SHARE;
    IF NOT FOUND OR lease.project_id IS DISTINCT FROM p_project_id
       OR lease.holder_id IS DISTINCT FROM p_holder_id OR lease.expires_at <= clock_timestamp() THEN
        RAISE EXCEPTION 'repository_write_lease_unavailable' USING ERRCODE='55000';
    END IF;
END $$;

CREATE FUNCTION public.check_version_repository_write_admission(
    p_project_id text,p_actor text,p_lease_id uuid,p_holder_id text
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE project public.projects%ROWTYPE; repo public.version_repositories%ROWTYPE;
BEGIN
    project := public._version_lock_admission_project(p_project_id);
    PERFORM public._version_assert_current_repository_actor(p_project_id,project.org_id,p_actor,true);
    PERFORM public._version_assert_write_lease(p_project_id,p_lease_id,p_holder_id);
    repo := public._version_lock_native_repository(p_project_id);
    -- A queued lock may outlive credential/lease wall-clock validity even
    -- though its rows cannot change. Recheck after acquiring every lock.
    PERFORM public._version_assert_current_repository_actor(p_project_id,project.org_id,p_actor,true);
    PERFORM public._version_assert_write_lease(p_project_id,p_lease_id,p_holder_id);
    RETURN jsonb_build_object('project_id',p_project_id,'org_id',project.org_id,
        'generation',repo.generation,'object_format',repo.object_format,'ref_sequence',repo.ref_sequence);
END $$;

CREATE FUNCTION public.apply_admitted_version_ref_transaction(
    p_project_id text,p_actor text,p_request_key uuid,p_generation bigint,
    p_updates jsonb,p_receipt_id uuid,p_message text,p_lease_id uuid,p_holder_id text
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE project public.projects%ROWTYPE; replay boolean; result jsonb;
BEGIN
    project := public._version_lock_admission_project(p_project_id);
    replay := public.get_version_ref_transaction(p_project_id,p_actor,p_request_key) IS NOT NULL;
    -- Current read access suffices to recover this actor's original result;
    -- replay never creates a new mutation or requires an expired write lease.
    -- The original primitive still validates the complete request digest.
    PERFORM public._version_assert_current_repository_actor(p_project_id,project.org_id,p_actor,NOT replay);
    IF NOT replay THEN
        PERFORM public._version_assert_write_lease(p_project_id,p_lease_id,p_holder_id);
    END IF;
    result := public.apply_version_ref_transaction(p_project_id,p_actor,p_request_key,p_generation,
        p_updates,p_receipt_id,p_message);
    -- Ref/receipt locks can queue after initial admission. A late expiry must
    -- roll back refs, result, reflog, audit and outbox together, not escape as ACK.
    PERFORM public._version_assert_current_repository_actor(p_project_id,project.org_id,p_actor,NOT replay);
    IF NOT replay THEN
        PERFORM public._version_assert_write_lease(p_project_id,p_lease_id,p_holder_id);
    END IF;
    RETURN result;
END $$;

REVOKE ALL ON FUNCTION public._version_lock_admission_project(text),
    public._version_assert_current_repository_actor(text,text,text,boolean),
    public._version_assert_write_lease(text,uuid,text),
    public.check_version_repository_write_admission(text,text,uuid,text),
    public.apply_admitted_version_ref_transaction(text,text,uuid,bigint,jsonb,uuid,text,uuid,text)
    FROM PUBLIC,anon,authenticated,service_role;
GRANT EXECUTE ON FUNCTION public.check_version_repository_write_admission(text,text,uuid,text),
    public.apply_admitted_version_ref_transaction(text,text,uuid,bigint,jsonb,uuid,text,uuid,text)
    TO service_role;
NOTIFY pgrst, 'reload schema';
COMMIT;
