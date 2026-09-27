-- ============================================================================
-- Connector policy — CLI FS command allow-list
-- ============================================================================
-- Access keys and scope geometry live on repo_scopes. Connector-specific
-- runtime policy lives on connectors. For the built-in CLI connector, V1 policy
-- is intentionally narrow: an allow-list of filesystem commands the access
-- point may run through /api/v1/ap-fs/*.
--
-- Delete commands stay off by default; users opt into them from the Access UI.
-- ============================================================================

BEGIN;

ALTER TABLE public.connectors
    ADD COLUMN IF NOT EXISTS policy JSONB NOT NULL DEFAULT '{}'::JSONB;

UPDATE public.connectors
   SET policy = jsonb_set(
       COALESCE(policy, '{}'::JSONB),
       '{fs,allowed_commands}',
       '[
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
        ]'::JSONB,
       true
   )
 WHERE provider = 'cli'
   AND policy #> '{fs,allowed_commands}' IS NULL;

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
        ),
        (
            NEW.project_id,
            NEW.id,
            'filesystem',
            'Local Folder Sync',
            'bidirectional',
            '{}'::JSONB,
            '{}'::JSONB,
            'active'
        );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

COMMIT;
