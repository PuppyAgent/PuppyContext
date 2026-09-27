DO $$
DECLARE
    report jsonb;
    conflict_count bigint;
BEGIN
    IF to_regclass('public.repo_user_permissions') IS NULL THEN
        RETURN;
    END IF;

    LOCK TABLE public.repo_user_permissions IN SHARE ROW EXCLUSIVE MODE;

    report := public.unified_authorization_preflight();
    IF (report->>'legacy_denied')::bigint > 0
       OR (report->>'legacy_scoped')::bigint > 0
       OR (report->>'legacy_tenant_mismatch')::bigint > 0
       OR (report->>'legacy_unknown_roles')::bigint > 0
       OR (report->>'invalid_project_members')::bigint > 0
       OR (report->>'creator_admin_unresolved')::bigint > 0
       OR (report->>'duplicate_or_missing_root_scopes')::bigint > 0
       OR (report->>'orphan_access_surfaces')::bigint > 0
       OR (report->>'orphan_access_credentials')::bigint > 0
       OR (report->>'invalid_access_tool_bindings')::bigint > 0 THEN
        RAISE EXCEPTION
          'repo_user_permissions data migration blocked; preflight=%', report;
    END IF;

    SELECT count(*) INTO conflict_count
    FROM public.repo_user_permissions rp
    JOIN public.project_members pm
      ON pm.project_id = rp.project_id AND pm.user_id = rp.user_id
    WHERE pm.role <> CASE rp.role
        WHEN 'admin' THEN 'admin'
        WHEN 'editor' THEN 'editor'
        WHEN 'reader' THEN 'viewer'
        ELSE pm.role
    END;
    IF conflict_count > 0 THEN
        RAISE EXCEPTION
          'repo_user_permissions data migration blocked: % conflicting explicit roles',
          conflict_count;
    END IF;

    INSERT INTO public.project_members (
        id, org_id, project_id, user_id, role, granted_by, created_at, updated_at
    )
    SELECT
        gen_random_uuid()::text,
        p.org_id,
        rp.project_id,
        rp.user_id,
        CASE rp.role
            WHEN 'admin' THEN 'admin'
            WHEN 'editor' THEN 'editor'
            WHEN 'reader' THEN 'viewer'
        END,
        rp.granted_by,
        rp.granted_at,
        rp.granted_at
    FROM public.repo_user_permissions rp
    JOIN public.projects p ON p.id = rp.project_id
    WHERE rp.role IN ('admin', 'editor', 'reader')
    ON CONFLICT (project_id, user_id) DO NOTHING;
END;
$$;
