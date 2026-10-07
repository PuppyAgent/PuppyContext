DO $$ BEGIN
    IF NOT EXISTS(
        SELECT FROM public.version_repository_migrations m
        JOIN public.version_repositories r ON r.project_id=m.project_id
        JOIN public.version_repository_billing b ON b.project_id=m.project_id
        JOIN public.version_repository_capacity c ON c.project_id=m.project_id
        JOIN public.version_repository_file_policies f ON f.project_id=m.project_id
        WHERE m.project_id='01a0e7da-6575-7771-af76-48b47dde4ba6'
          AND m.state='activated' AND m.manifest_sha256 IS NOT NULL AND m.activated_at IS NOT NULL
          AND r.authority='native' AND r.write_state='active'
          AND b.initialized AND b.accounted_bytes IS NOT NULL AND c.initialized AND f.initialized
          AND EXISTS(SELECT FROM public.organization_usage_counters u
              WHERE u.org_id=b.org_id AND u.metric='storage.logical_bytes' AND u.version>0)
          AND EXISTS(SELECT FROM public.version_repository_refs h
              WHERE h.project_id=m.project_id AND h.name=convert_to('HEAD','UTF8'))
    ) THEN RAISE EXCEPTION 'selected native project migration incomplete'; END IF;
END $$;
