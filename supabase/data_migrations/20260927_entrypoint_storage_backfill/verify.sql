-- Also valid after Contract: old columns and mirror rows are intentionally gone.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM public.access_tools WHERE access_surface_id IS NULL)
     OR EXISTS (SELECT 1 FROM public.github_sync_log WHERE binding_id IS NULL) THEN
    RAISE EXCEPTION 'entrypoint identifiers have not been backfilled';
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_schema='public' AND table_name='access_tools' AND column_name='access_point_id') THEN
    IF EXISTS (SELECT 1 FROM public.access_tools WHERE access_surface_id IS DISTINCT FROM access_point_id)
       OR EXISTS (SELECT 1 FROM public.github_sync_log WHERE binding_id IS DISTINCT FROM integration_id) THEN
      RAISE EXCEPTION 'entrypoint identifier mismatch';
    END IF;
    IF EXISTS (
      SELECT 1 FROM public.uploads u FULL JOIN public.search_index_tasks s
        ON u.id = s.id AND u.type = 'search_index'
      WHERE (u.type = 'search_index' OR s.id IS NOT NULL)
        AND (u.id IS NULL OR s.id IS NULL OR to_jsonb(u) - 'type' IS DISTINCT FROM to_jsonb(s))
    ) THEN
      RAISE EXCEPTION 'search index task mismatch';
    END IF;
  ELSE
    IF EXISTS (SELECT 1 FROM public.uploads WHERE type = 'search_index') THEN
      RAISE EXCEPTION 'search index tasks remain in uploads after Contract';
    END IF;
  END IF;
END $$;
