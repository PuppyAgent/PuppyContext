-- Integration target paths are project-root write destinations.
-- Access scopes remain permission boundaries and must not encode where a
-- third-party Integration writes its fetched data.

ALTER TABLE public.connections
    ADD COLUMN IF NOT EXISTS target_path text;

UPDATE public.connections c
SET target_path = btrim(COALESCE(c.config->>'target_path', s.path, ''), '/')
FROM public.repo_scopes s
WHERE c.scope_id = s.id
  AND NULLIF(c.target_path, '') IS NULL
  AND COALESCE(c.config->>'target_path', s.path, '') IS NOT NULL;

UPDATE public.connections
SET target_path = btrim(config->>'target_path', '/')
WHERE NULLIF(target_path, '') IS NULL
  AND NULLIF(config->>'target_path', '') IS NOT NULL;

UPDATE public.connections
SET config = jsonb_set(
    COALESCE(config, '{}'::jsonb),
    '{target_path}',
    to_jsonb(target_path),
    true
)
WHERE NULLIF(target_path, '') IS NOT NULL
  AND COALESCE(config->>'target_path', '') <> target_path;

CREATE INDEX IF NOT EXISTS idx_connections_project_target_path
    ON public.connections(project_id, target_path)
    WHERE target_path IS NOT NULL;

COMMENT ON COLUMN public.connections.target_path IS
    'Project-root destination path for Integration writes. scope_id is legacy/root compatibility, not the Integration write destination.';
