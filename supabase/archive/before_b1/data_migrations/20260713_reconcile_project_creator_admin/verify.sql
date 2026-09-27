DO $$
DECLARE
    report jsonb;
BEGIN
    report := public.project_creator_authorization_preflight();

    IF (report->>'creator_missing_profile')::bigint > 0
       OR (report->>'creator_missing_org_membership')::bigint > 0
       OR (report->>'creator_missing_project_membership')::bigint > 0
       OR (report->>'creator_non_admin_project_membership')::bigint > 0 THEN
        RAISE EXCEPTION
          'Project creator reconciliation verification failed; preflight=%',
          report;
    END IF;

    IF (public.unified_authorization_preflight()
          ->>'creator_admin_unresolved')::bigint > 0 THEN
        RAISE EXCEPTION
          'Project creator reconciliation verification failed: unresolved creator Admin facts remain';
    END IF;
END;
$$;
