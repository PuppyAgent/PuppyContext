DO $$ BEGIN
    IF EXISTS(SELECT 1 FROM public.projects p LEFT JOIN public.version_repositories r ON r.project_id=p.id
        WHERE p.lifecycle_status<>'ready' OR r.authority IS DISTINCT FROM 'native') THEN
        RAISE EXCEPTION 'native migration incomplete: not every project is ready/native';
    END IF;
    IF EXISTS(SELECT 1 FROM public.version_repository_migrations m
        LEFT JOIN public.version_repositories r ON r.project_id=m.project_id
        LEFT JOIN public.version_repository_billing b ON b.project_id=m.project_id
        LEFT JOIN public.version_repository_capacity c ON c.project_id=m.project_id
        LEFT JOIN public.version_repository_file_policies f ON f.project_id=m.project_id
        WHERE m.state<>'activated' OR m.manifest_sha256 IS NULL OR m.activated_at IS NULL
          OR r.authority IS DISTINCT FROM 'native' OR b.initialized IS DISTINCT FROM true
          OR b.accounted_bytes IS NULL OR c.initialized IS DISTINCT FROM true OR f.initialized IS DISTINCT FROM true
          OR NOT EXISTS(SELECT 1 FROM public.version_repository_refs h
              WHERE h.project_id=m.project_id AND h.name=convert_to('HEAD','UTF8'))) THEN
        RAISE EXCEPTION 'native migration completion evidence missing';
    END IF;
END $$;
