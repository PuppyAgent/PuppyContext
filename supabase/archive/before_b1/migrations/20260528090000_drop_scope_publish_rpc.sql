-- Root-first cutover: scope-state is a cache, not a write authority.
-- New runtime code publishes through publish_mut_project_update only.

BEGIN;

DROP FUNCTION IF EXISTS public.publish_mut_scope_update(
    TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, JSONB, JSONB, TEXT, TEXT, JSONB
);

DROP FUNCTION IF EXISTS public.publish_mut_scope_update(
    TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, JSONB, JSONB, TEXT, TEXT, JSONB,
    TEXT, TEXT, TEXT, TEXT, TEXT, TEXT
);

COMMIT;
