-- Runner owns this transaction and receipt. Bound the lock wait; retry safely.
SET LOCAL lock_timeout = '5s';
LOCK TABLE public.access_tools, public.github_sync_log, public.uploads,
  public.search_index_tasks IN SHARE ROW EXCLUSIVE MODE;
DO $$
BEGIN
IF NOT EXISTS (SELECT 1 FROM information_schema.columns
  WHERE table_schema='public' AND table_name='access_tools' AND column_name='access_point_id') THEN
  RETURN;
END IF;
UPDATE public.access_tools SET access_surface_id = access_point_id
  WHERE access_surface_id IS NULL;
UPDATE public.github_sync_log SET binding_id = integration_id WHERE binding_id IS NULL;
INSERT INTO public.search_index_tasks (id, created_by, project_id, path, config, status, progress, message, error, result_path, result, created_at, updated_at, started_at, completed_at)
SELECT id, created_by, project_id, path, config, status, progress, message, error, result_path, result, created_at, updated_at, started_at, completed_at FROM public.uploads WHERE type = 'search_index'
ON CONFLICT (id) DO NOTHING;
END $$;
