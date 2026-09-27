DO $$
DECLARE
    report jsonb;
    unresolved_count bigint;
BEGIN
    IF to_regclass('public.repo_user_permissions') IS NULL THEN
        RETURN;
    END IF;

    report := public.unified_authorization_preflight();
    IF (report->>'legacy_denied')::bigint > 0
       OR (report->>'legacy_scoped')::bigint > 0
       OR (report->>'legacy_tenant_mismatch')::bigint > 0
       OR (report->>'legacy_unknown_roles')::bigint > 0 THEN
        RAISE EXCEPTION
          'repo_user_permissions verification blocked; preflight=%', report;
    END IF;

    SELECT count(*) INTO unresolved_count
    FROM public.repo_user_permissions rp
    LEFT JOIN public.project_members pm
      ON pm.project_id = rp.project_id
     AND pm.user_id = rp.user_id
     AND pm.role = CASE rp.role
        WHEN 'admin' THEN 'admin'
        WHEN 'editor' THEN 'editor'
        WHEN 'reader' THEN 'viewer'
        ELSE NULL
     END
    WHERE rp.role IN ('admin', 'editor', 'reader')
      AND pm.id IS NULL;

    IF unresolved_count > 0 THEN
        RAISE EXCEPTION
          'repo_user_permissions verification failed: % row(s) are unresolved',
          unresolved_count;
    END IF;
END;
$$;
