DO $$
DECLARE
    missing_profile_count bigint;
BEGIN
    SELECT count(*) INTO missing_profile_count
    FROM public.projects p
    LEFT JOIN public.profiles pr ON pr.user_id = p.created_by
    WHERE p.created_by IS NOT NULL AND pr.user_id IS NULL;

    IF missing_profile_count > 0 THEN
        RAISE EXCEPTION
          'Project creator reconciliation blocked: % creator profile(s) are missing',
          missing_profile_count;
    END IF;
END;
$$;

-- Organization membership is the tenant boundary.  Historical creators that
-- predate atomic Project creation receive the least-privileged organization
-- baseline; their Project authority remains explicit and independent below.
WITH candidates AS (
    SELECT DISTINCT p.org_id, p.created_by AS user_id
    FROM public.projects p
    LEFT JOIN public.org_members om
      ON om.org_id = p.org_id AND om.user_id = p.created_by
    WHERE p.created_by IS NOT NULL AND om.id IS NULL
), inserted AS (
    INSERT INTO public.org_members (id, org_id, user_id, role)
    SELECT gen_random_uuid()::text, org_id, user_id, 'viewer'
    FROM candidates
    ON CONFLICT (org_id, user_id) DO NOTHING
    RETURNING org_id, user_id, role
)
INSERT INTO public.audit_logs (
    action, path, project_id, operator_type, operator_id, status, metadata
)
SELECT
    'project_creator.org_membership.reconcile',
    '',
    NULL,
    'system',
    '20260713_reconcile_project_creator_admin',
    'success',
    jsonb_build_object(
        'org_id', org_id,
        'user_id', user_id,
        'role', role
    )
FROM inserted;

-- The creator fact is authoritative: a missing or downgraded explicit Project
-- membership is restored to Admin.  The trigger installed by the guard
-- schema rejects every future downgrade or deletion of this row.
WITH reconciled AS (
    INSERT INTO public.project_members (
        id, org_id, project_id, user_id, role, granted_by
    )
    SELECT
        gen_random_uuid()::text,
        p.org_id,
        p.id,
        p.created_by,
        'admin',
        p.created_by
    FROM public.projects p
    WHERE p.created_by IS NOT NULL
    ON CONFLICT (project_id, user_id) DO UPDATE
      SET org_id = EXCLUDED.org_id,
          role = EXCLUDED.role,
          granted_by = COALESCE(
              public.project_members.granted_by,
              EXCLUDED.granted_by
          )
      WHERE public.project_members.org_id IS DISTINCT FROM EXCLUDED.org_id
         OR public.project_members.role IS DISTINCT FROM EXCLUDED.role
    RETURNING project_id, user_id, role
)
INSERT INTO public.audit_logs (
    action, path, project_id, operator_type, operator_id, status, metadata
)
SELECT
    'project_creator.project_membership.reconcile',
    '',
    project_id,
    'system',
    '20260713_reconcile_project_creator_admin',
    'success',
    jsonb_build_object('user_id', user_id, 'role', role)
FROM reconciled;
