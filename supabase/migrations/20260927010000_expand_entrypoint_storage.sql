-- ISSUE-049: compatible schema for entrypoint storage ownership.
-- Old and new applications share the same records during the rollback window.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '5min';

-- D01: preserve both spellings until every process has switched.
ALTER TABLE public.access_tools ADD COLUMN access_surface_id text;
ALTER TABLE public.access_tools ADD CONSTRAINT access_tools_access_surface_id_fkey
  FOREIGN KEY (access_surface_id) REFERENCES public.access_surfaces(id) ON DELETE CASCADE;
ALTER TABLE public.access_tools ADD CONSTRAINT access_tools_access_surface_id_tool_id_key
  UNIQUE (access_surface_id, tool_id);

CREATE FUNCTION public._sync_access_tool_surface_columns() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  IF TG_OP = 'UPDATE' THEN
    IF NEW.access_surface_id IS DISTINCT FROM OLD.access_surface_id
       AND NEW.access_point_id IS NOT DISTINCT FROM OLD.access_point_id THEN
      NEW.access_point_id := NEW.access_surface_id;
    ELSIF NEW.access_point_id IS DISTINCT FROM OLD.access_point_id
       AND NEW.access_surface_id IS NOT DISTINCT FROM OLD.access_surface_id THEN
      NEW.access_surface_id := NEW.access_point_id;
    END IF;
  END IF;
  NEW.access_surface_id := coalesce(NEW.access_surface_id, NEW.access_point_id);
  NEW.access_point_id := coalesce(NEW.access_point_id, NEW.access_surface_id);
  IF NEW.access_surface_id IS DISTINCT FROM NEW.access_point_id THEN
    RAISE EXCEPTION 'conflicting access surface identifiers' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION public._sync_access_tool_surface_columns() FROM PUBLIC, anon, authenticated;
CREATE TRIGGER a00_sync_access_tool_surface_columns BEFORE INSERT OR UPDATE
  ON public.access_tools FOR EACH ROW EXECUTE FUNCTION public._sync_access_tool_surface_columns();
-- Also check updates made only through the new column. Trigger order is deliberate.
DROP TRIGGER trg_validate_access_tool_project_boundary ON public.access_tools;
CREATE TRIGGER trg_validate_access_tool_project_boundary BEFORE INSERT OR UPDATE
  ON public.access_tools FOR EACH ROW EXECUTE FUNCTION public._validate_access_tool_project_boundary();

-- D02: rename the actual table; the old name is an invoker view, never a copy.
ALTER TABLE public.github_integrations RENAME TO github_sync_bindings;
CREATE VIEW public.github_integrations WITH (security_invoker = true) AS
  SELECT * FROM public.github_sync_bindings;
REVOKE ALL ON public.github_integrations FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.github_integrations TO service_role;
ALTER TABLE public.github_sync_log ADD COLUMN binding_id text;
ALTER TABLE public.github_sync_log ADD CONSTRAINT github_sync_log_binding_id_fkey
  FOREIGN KEY (binding_id) REFERENCES public.github_sync_bindings(id) ON DELETE CASCADE;
CREATE INDEX idx_github_sync_log_binding_recent ON public.github_sync_log(binding_id, created_at DESC);
CREATE INDEX idx_github_sync_log_binding_dedupe ON public.github_sync_log(binding_id, direction, git_sha)
  WHERE status = 'success';
CREATE FUNCTION public._sync_github_binding_columns() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  IF TG_OP = 'UPDATE' THEN
    IF NEW.binding_id IS DISTINCT FROM OLD.binding_id
       AND NEW.integration_id IS NOT DISTINCT FROM OLD.integration_id THEN
      NEW.integration_id := NEW.binding_id;
    ELSIF NEW.integration_id IS DISTINCT FROM OLD.integration_id
       AND NEW.binding_id IS NOT DISTINCT FROM OLD.binding_id THEN
      NEW.binding_id := NEW.integration_id;
    END IF;
  END IF;
  NEW.binding_id := coalesce(NEW.binding_id, NEW.integration_id);
  NEW.integration_id := coalesce(NEW.integration_id, NEW.binding_id);
  IF NEW.binding_id IS DISTINCT FROM NEW.integration_id THEN
    RAISE EXCEPTION 'conflicting GitHub binding identifiers' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION public._sync_github_binding_columns() FROM PUBLIC, anon, authenticated;
CREATE TRIGGER a00_sync_github_binding_columns BEFORE INSERT OR UPDATE
  ON public.github_sync_log FOR EACH ROW EXECUTE FUNCTION public._sync_github_binding_columns();

-- D03: dedicated task storage. Preserve the complete job payload and timestamps.
CREATE TABLE public.search_index_tasks (
  id text PRIMARY KEY,
  created_by uuid REFERENCES auth.users(id) ON DELETE CASCADE,
  project_id text NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,
  path text,
  config jsonb NOT NULL DEFAULT '{}'::jsonb,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','running','completed','failed','cancelled')),
  progress integer NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
  message text,
  error text,
  result_path text,
  result jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  completed_at timestamptz
);
CREATE INDEX idx_search_index_tasks_project_status ON public.search_index_tasks(project_id, status);
ALTER TABLE public.search_index_tasks ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.search_index_tasks FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.search_index_tasks TO service_role;
CREATE POLICY search_index_tasks_service_role_all ON public.search_index_tasks
  TO service_role USING (true) WITH CHECK (true);

-- Synchronize both interfaces in the same transaction. A no-op upsert stops
-- recursion; errors roll back BOTH writes. No eventual-copy window is allowed.
CREATE FUNCTION public._mirror_search_tasks_from_uploads() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    DELETE FROM public.search_index_tasks WHERE id = OLD.id AND OLD.type = 'search_index';
    RETURN OLD;
  END IF;
  IF TG_OP = 'UPDATE' AND OLD.type = 'search_index'
     AND (NEW.type <> 'search_index' OR OLD.id <> NEW.id) THEN
    DELETE FROM public.search_index_tasks WHERE id = OLD.id;
  END IF;
  IF NEW.type <> 'search_index' THEN RETURN NEW; END IF;
  INSERT INTO public.search_index_tasks (id, created_by, project_id, path, config, status, progress, message, error, result_path, result, created_at, updated_at, started_at, completed_at) VALUES (NEW.id, NEW.created_by, NEW.project_id, NEW.path, NEW.config, NEW.status, NEW.progress, NEW.message, NEW.error, NEW.result_path, NEW.result, NEW.created_at, NEW.updated_at, NEW.started_at, NEW.completed_at)
  ON CONFLICT (id) DO UPDATE SET
    created_by = EXCLUDED.created_by,
    project_id = EXCLUDED.project_id,
    path = EXCLUDED.path,
    config = EXCLUDED.config,
    status = EXCLUDED.status,
    progress = EXCLUDED.progress,
    message = EXCLUDED.message,
    error = EXCLUDED.error,
    result_path = EXCLUDED.result_path,
    result = EXCLUDED.result,
    created_at = EXCLUDED.created_at,
    updated_at = EXCLUDED.updated_at,
    started_at = EXCLUDED.started_at,
    completed_at = EXCLUDED.completed_at
  WHERE (search_index_tasks.id, search_index_tasks.created_by, search_index_tasks.project_id, search_index_tasks.path, search_index_tasks.config, search_index_tasks.status, search_index_tasks.progress, search_index_tasks.message, search_index_tasks.error, search_index_tasks.result_path, search_index_tasks.result, search_index_tasks.created_at, search_index_tasks.updated_at, search_index_tasks.started_at, search_index_tasks.completed_at) IS DISTINCT FROM (EXCLUDED.id, EXCLUDED.created_by, EXCLUDED.project_id, EXCLUDED.path, EXCLUDED.config, EXCLUDED.status, EXCLUDED.progress, EXCLUDED.message, EXCLUDED.error, EXCLUDED.result_path, EXCLUDED.result, EXCLUDED.created_at, EXCLUDED.updated_at, EXCLUDED.started_at, EXCLUDED.completed_at);
  RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION public._mirror_search_tasks_from_uploads() FROM PUBLIC, anon, authenticated;
CREATE FUNCTION public._mirror_search_tasks_to_uploads() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  IF TG_OP <> 'DELETE' AND EXISTS (
    SELECT 1 FROM public.uploads WHERE id = NEW.id AND type <> 'search_index'
  ) THEN
    RAISE EXCEPTION 'search task id collides with a non-search upload' USING ERRCODE = '23514';
  END IF;
  IF TG_OP = 'DELETE' THEN
    DELETE FROM public.uploads WHERE id = OLD.id AND type = 'search_index';
    RETURN OLD;
  END IF;
  IF TG_OP = 'UPDATE' AND OLD.id <> NEW.id THEN
    DELETE FROM public.uploads WHERE id = OLD.id AND type = 'search_index';
  END IF;
  INSERT INTO public.uploads (id, created_by, project_id, path, config, status, progress, message, error, result_path, result, created_at, updated_at, started_at, completed_at, type) VALUES (NEW.id, NEW.created_by, NEW.project_id, NEW.path, NEW.config, NEW.status, NEW.progress, NEW.message, NEW.error, NEW.result_path, NEW.result, NEW.created_at, NEW.updated_at, NEW.started_at, NEW.completed_at, 'search_index')
  ON CONFLICT (id) DO UPDATE SET
    created_by = EXCLUDED.created_by,
    project_id = EXCLUDED.project_id,
    path = EXCLUDED.path,
    config = EXCLUDED.config,
    status = EXCLUDED.status,
    progress = EXCLUDED.progress,
    message = EXCLUDED.message,
    error = EXCLUDED.error,
    result_path = EXCLUDED.result_path,
    result = EXCLUDED.result,
    created_at = EXCLUDED.created_at,
    updated_at = EXCLUDED.updated_at,
    started_at = EXCLUDED.started_at,
    completed_at = EXCLUDED.completed_at
  WHERE (uploads.id, uploads.created_by, uploads.project_id, uploads.path, uploads.config, uploads.status, uploads.progress, uploads.message, uploads.error, uploads.result_path, uploads.result, uploads.created_at, uploads.updated_at, uploads.started_at, uploads.completed_at) IS DISTINCT FROM (EXCLUDED.id, EXCLUDED.created_by, EXCLUDED.project_id, EXCLUDED.path, EXCLUDED.config, EXCLUDED.status, EXCLUDED.progress, EXCLUDED.message, EXCLUDED.error, EXCLUDED.result_path, EXCLUDED.result, EXCLUDED.created_at, EXCLUDED.updated_at, EXCLUDED.started_at, EXCLUDED.completed_at);
  RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION public._mirror_search_tasks_to_uploads() FROM PUBLIC, anon, authenticated;
CREATE TRIGGER mirror_search_tasks_from_uploads AFTER INSERT OR UPDATE OR DELETE
  ON public.uploads FOR EACH ROW EXECUTE FUNCTION public._mirror_search_tasks_from_uploads();
CREATE TRIGGER mirror_search_tasks_to_uploads AFTER INSERT OR UPDATE OR DELETE
  ON public.search_index_tasks FOR EACH ROW EXECUTE FUNCTION public._mirror_search_tasks_to_uploads();

-- These are backend-only objects. Do not inherit Supabase's broad default grants.
REVOKE ALL ON public.access_tools, public.github_sync_bindings, public.github_sync_log
  FROM PUBLIC, anon, authenticated;
-- Search-task owners remain visible to Project deletion storage cleanup.
CREATE OR REPLACE FUNCTION "public"."_project_deletion_storage_principals"("p_project_id" "text", "p_requested_by" "uuid") RETURNS "jsonb"
    LANGUAGE "sql" STABLE
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $_$
    SELECT COALESCE(jsonb_agg(source.principal ORDER BY source.principal), '[]'::jsonb)
    FROM (
        SELECT DISTINCT candidate.principal
        FROM (
            SELECT p_requested_by::text AS principal
            UNION ALL
            SELECT project.created_by::text
            FROM public.projects project
            WHERE project.id = p_project_id
            UNION ALL
            SELECT stored.principal
            FROM public.project_storage_principals stored
            WHERE stored.project_id = p_project_id
            UNION ALL
            SELECT COALESCE(upload.created_by::text, p_project_id)
            FROM public.uploads upload
            WHERE upload.project_id = p_project_id
            UNION ALL
            SELECT COALESCE(task.created_by::text, p_project_id)
            FROM public.search_index_tasks task
            WHERE task.project_id = p_project_id
        ) candidate
        WHERE candidate.principal IS NOT NULL
          AND candidate.principal ~ '^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$'
    ) source;
$_$;
NOTIFY pgrst, 'reload schema';
COMMIT;
