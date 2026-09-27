-- ============================================================================
-- Remove the legacy filesystem Access provider
-- ============================================================================
-- Product model after this migration:
--   - git_remote: native Git clone/pull/push
--   - cli: FS CLI commands through /api/v1/ap-fs/*
--
-- The old provider='filesystem' / kind='filesystem' shell did not own the file
-- data plane; Git Remote and AP-FS do. Remove it as a first-class Access
-- surface so new and existing scopes expose only the two real entry points.
-- ============================================================================

BEGIN;

DELETE FROM public.access_surfaces
WHERE kind = 'filesystem';

DELETE FROM public.connectors
WHERE provider = 'filesystem';

DELETE FROM public.connections
WHERE provider = 'filesystem';

CREATE OR REPLACE FUNCTION public.create_builtin_connectors_for_scope()
RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO public.connectors
        (project_id, scope_id, provider, name, direction, config, policy, status)
    VALUES
        (
            NEW.project_id,
            NEW.id,
            'cli',
            'Local CLI',
            'bidirectional',
            '{}'::JSONB,
            '{
              "fs": {
                "allowed_commands": [
                  "semantics",
                  "ls",
                  "tree",
                  "find",
                  "grep",
                  "stat",
                  "cat",
                  "head",
                  "tail",
                  "download",
                  "write",
                  "mkdir",
                  "touch",
                  "upload",
                  "cp",
                  "mv"
                ]
              }
            }'::JSONB,
            'active'
        ),
        (
            NEW.project_id,
            NEW.id,
            'agent',
            'AI Agent',
            'bidirectional',
            '{}'::JSONB,
            '{}'::JSONB,
            'active'
        );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP INDEX IF EXISTS public.idx_access_surfaces_builtin_one_per_scope;
CREATE UNIQUE INDEX IF NOT EXISTS idx_access_surfaces_builtin_one_per_scope
    ON public.access_surfaces(scope_id, kind)
    WHERE kind IN ('git_remote', 'cli');

ALTER TABLE public.access_surfaces
    DROP CONSTRAINT IF EXISTS access_surfaces_kind_check;

ALTER TABLE public.access_surfaces
    ADD CONSTRAINT access_surfaces_kind_check
    CHECK (kind IN ('git_remote', 'cli', 'agent', 'mcp', 'sandbox'));

DROP INDEX IF EXISTS public.idx_connectors_builtin_one_per_scope;
CREATE UNIQUE INDEX IF NOT EXISTS idx_connectors_builtin_one_per_scope
    ON public.connectors (scope_id, provider)
    WHERE provider IN ('cli', 'agent');

COMMIT;
