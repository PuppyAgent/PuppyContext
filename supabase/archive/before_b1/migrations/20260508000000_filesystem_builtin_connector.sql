-- ============================================================================
-- Promote 'filesystem' (MUT folder sync) to a built-in connector
-- ============================================================================
-- Why
--   In the post-redesign access-points world, every scope ALWAYS gets a
--   'cli' and an 'agent' connector auto-created by a trigger. Filesystem
--   sync — the third "default" connection method users expect, where a
--   local folder pulls/pushes via the MUT protocol — was created on
--   demand by a now-deprecated /filesystem/bootstrap endpoint that wrote
--   to the dropped legacy access_points table.
--
--   The user-facing model is "every scope has three built-in connection
--   methods (CLI / Agent / Folder sync), each one toggleable via
--   pause/resume". This migration aligns the data model with that
--   contract:
--
--     1. The unique-one-per-scope index now covers 'filesystem' too.
--     2. The auto-create trigger now inserts a 'filesystem' row alongside
--        'cli' and 'agent' on every repo_scopes INSERT.
--     3. Existing scopes get a backfill INSERT so the three-method
--        invariant holds for already-created data.
--
--   The pause/resume mechanism (POST /connectors/:id/{pause,resume}) is
--   provider-agnostic — once these rows exist, the existing UI button on
--   each AP card already toggles them on/off.
--
--   See docs/design/access-point-redesign-2026-05-02.md and the original
--   trigger in 20260502000100_connectors_table.sql.
--
-- Idempotency
--   - DROP INDEX IF EXISTS / CREATE UNIQUE INDEX IF NOT EXISTS for the
--     partial unique index swap (the WHERE clause changed, so we replace).
--   - CREATE OR REPLACE FUNCTION for the trigger body — re-running yields
--     the same final SQL.
--   - Backfill uses ON CONFLICT DO NOTHING via the new unique index, so
--     scopes that already have a filesystem connector are skipped.
-- ============================================================================

BEGIN;

-- ── 1. Replace the partial unique index to include 'filesystem' ──────────
-- The original index lives in 20260502000100_connectors_table.sql and only
-- enforces uniqueness for cli + agent. Filesystem joins the built-in club.
DROP INDEX IF EXISTS public.idx_connectors_builtin_one_per_scope;

CREATE UNIQUE INDEX IF NOT EXISTS idx_connectors_builtin_one_per_scope
    ON public.connectors (scope_id, provider)
    WHERE provider IN ('cli', 'agent', 'filesystem');

-- ── 2. Replace the auto-create trigger function ──────────────────────────
-- New scopes from this point forward auto-receive all three built-ins.
CREATE OR REPLACE FUNCTION public.create_builtin_connectors_for_scope()
RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO public.connectors
        (project_id, scope_id, provider, name, direction, config, status)
    VALUES
        (NEW.project_id, NEW.id, 'cli',        'Local CLI',         'bidirectional', '{}'::JSONB, 'active'),
        (NEW.project_id, NEW.id, 'agent',      'AI Agent',          'bidirectional', '{}'::JSONB, 'active'),
        (NEW.project_id, NEW.id, 'filesystem', 'Local Folder Sync', 'bidirectional', '{}'::JSONB, 'active');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- The trigger itself was already created in the original migration; the
-- function body swap is enough. Re-asserting to be defensive in environments
-- where the trigger was dropped.
DROP TRIGGER IF EXISTS trg_create_builtin_connectors ON public.repo_scopes;
CREATE TRIGGER trg_create_builtin_connectors
    AFTER INSERT ON public.repo_scopes
    FOR EACH ROW
    EXECUTE FUNCTION public.create_builtin_connectors_for_scope();

-- ── 3. Backfill existing scopes ──────────────────────────────────────────
-- Insert one filesystem connector for every scope that currently doesn't
-- have one. ON CONFLICT DO NOTHING is a safety belt — the index above
-- already guarantees one-per-scope, but if a manual filesystem row exists
-- (carried over from earlier ad-hoc bootstrap calls) we leave it alone.
INSERT INTO public.connectors
    (project_id, scope_id, provider, name, direction, config, status)
SELECT
    s.project_id,
    s.id,
    'filesystem',
    'Local Folder Sync',
    'bidirectional',
    '{}'::JSONB,
    'active'
FROM public.repo_scopes s
WHERE NOT EXISTS (
    SELECT 1
      FROM public.connectors c
     WHERE c.scope_id = s.id
       AND c.provider = 'filesystem'
)
ON CONFLICT DO NOTHING;

-- ── 4. Sanity check ──────────────────────────────────────────────────────
-- Every scope should now have exactly one row per built-in provider.
DO $$
DECLARE
    missing INTEGER;
BEGIN
    SELECT COUNT(*) INTO missing
      FROM public.repo_scopes s
     CROSS JOIN (VALUES ('cli'), ('agent'), ('filesystem')) AS p(provider)
     WHERE NOT EXISTS (
         SELECT 1
           FROM public.connectors c
          WHERE c.scope_id = s.id
            AND c.provider = p.provider
     );
    IF missing > 0 THEN
        RAISE EXCEPTION 'Backfill failed: % (scope, builtin-provider) pair(s) missing', missing;
    END IF;
END $$;

COMMIT;
