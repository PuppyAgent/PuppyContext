DO $$
BEGIN
    IF to_regclass('public.repo_user_permissions') IS NOT NULL THEN
        RAISE EXCEPTION 'legacy permission table was not retired';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM public.project_members
        WHERE project_id = 'db-migration-project'
          AND user_id = '00000000-0000-0000-0000-000000012902'::uuid
          AND role = 'viewer'
    ) THEN
        RAISE EXCEPTION 'legacy reader was not migrated to Project Viewer';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM public.migration_log
        WHERE name = '20260712_repo_user_permissions_to_project_members'
          AND COALESCE((summary->>'verified')::boolean, false)
          AND summary->>'artifact_checksum' =
              '649b84361ea1c8b72dfcef8f6c9e5beeafa520a1322b4d9f1ecbb79202fd6bce'
    ) THEN
        RAISE EXCEPTION 'verified data migration receipt is missing';
    END IF;
END;
$$;
