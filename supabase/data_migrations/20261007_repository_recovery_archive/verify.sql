DO $$ BEGIN
    IF EXISTS(SELECT 1 FROM public.projects p LEFT JOIN public.version_repositories r ON r.project_id=p.id
        WHERE p.lifecycle_status<>'ready' OR r.authority IS DISTINCT FROM 'native') THEN
        RAISE EXCEPTION 'native_repository_required'; END IF;
    IF EXISTS(SELECT 1 FROM public.projects m
        LEFT JOIN public.version_repository_archives a ON a.project_id=m.id
        WHERE a.state IS DISTINCT FROM 'verified' OR a.manifest_sha256 IS NULL OR a.verified_at IS NULL OR a.source_deleted_at IS NULL)
      OR EXISTS(SELECT 1 FROM public.version_repository_archives
        WHERE state<>'verified' OR manifest_sha256 IS NULL OR verified_at IS NULL OR source_deleted_at IS NULL) THEN
        RAISE EXCEPTION 'repository_recovery_archive_required'; END IF;
END $$;
