-- ISSUE-062: pin provenance does not grant ongoing actor/lease authority.
-- Empty Expand only; no enrollment, data rewrite, physical I/O or claim settlement.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='2min';
CREATE FUNCTION public.renew_admitted_version_object_pin(p_project_id text,p_actor text,p_pin_id uuid)
RETURNS timestamptz LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE project public.projects%ROWTYPE; pin_purpose text; expiration timestamptz;
BEGIN
    project:=public._version_lock_admission_project(p_project_id);
    SELECT purpose INTO pin_purpose FROM public.version_object_pins
        WHERE id=p_pin_id AND project_id=p_project_id AND actor=p_actor;
    IF NOT FOUND THEN RAISE EXCEPTION 'publication_pin_unavailable' USING ERRCODE='55000'; END IF;
    PERFORM public._version_assert_current_repository_actor(p_project_id,project.org_id,p_actor,pin_purpose='publication');
    IF pin_purpose='publication' THEN
        PERFORM public._version_assert_native_pin_admission(p_project_id,p_pin_id);
    END IF;
    expiration:=public.renew_version_object_publication(p_project_id,p_actor,p_pin_id);
    -- Credential and lease wall clocks keep advancing while the repository/pin
    -- lock is queued. Rejection rolls back the attempted expiration extension.
    PERFORM public._version_assert_current_repository_actor(p_project_id,project.org_id,p_actor,pin_purpose='publication');
    IF pin_purpose='publication' THEN
        PERFORM public._version_assert_native_pin_admission(p_project_id,p_pin_id);
    END IF;
    RETURN expiration;
END $$;
REVOKE ALL ON FUNCTION public.renew_admitted_version_object_pin(text,text,uuid)
    FROM PUBLIC,anon,authenticated,service_role;
GRANT EXECUTE ON FUNCTION public.renew_admitted_version_object_pin(text,text,uuid) TO service_role;
NOTIFY pgrst,'reload schema';
COMMIT;
