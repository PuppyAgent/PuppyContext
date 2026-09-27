DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM public.org_members
        WHERE org_id = 'creator-repair-org'
          AND user_id = '00000000-0000-0000-0000-000000013102'::uuid
          AND role = 'viewer'
    ) THEN
        RAISE EXCEPTION 'missing creator did not receive the organization Viewer baseline';
    END IF;

    IF (
        SELECT count(*)
        FROM public.project_members
        WHERE project_id IN (
            'creator-repair-missing-project',
            'creator-repair-viewer-project'
        )
          AND role = 'admin'
    ) <> 2 THEN
        RAISE EXCEPTION 'historical Project creators were not reconciled to explicit Admin';
    END IF;

    IF (
        SELECT count(*)
        FROM public.audit_logs
        WHERE operator_id = '20260713_reconcile_project_creator_admin'
          AND action IN (
              'project_creator.org_membership.reconcile',
              'project_creator.project_membership.reconcile'
          )
    ) <> 3 THEN
        RAISE EXCEPTION 'creator reconciliation audit facts are incomplete or duplicated';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM public.migration_log
        WHERE name = '20260713_reconcile_project_creator_admin'
          AND COALESCE((summary->>'verified')::boolean, false)
    ) THEN
        RAISE EXCEPTION 'verified creator reconciliation receipt is missing';
    END IF;
END;
$$;
