-- ============================================================================
-- Backfill root scope for every existing project
-- ============================================================================
-- Why
--   `repo_scopes` was created empty. Every existing project needs a root
--   scope (path='', is_root=true) before the application starts assuming
--   "every project has at least one scope".
--
--   The trigger from 20260502000100_connectors_table.sql will fire on each
--   INSERT here and auto-create the cli + agent connectors for the new
--   root scope, so this migration ALSO populates the connectors table for
--   existing projects.
--
--   See docs/design/access-point-redesign-2026-05-02.md (section 5.7,
--   migration #6).
--
-- Why a fresh access_key per existing project (not copied from access_points)?
--   The data migration in 20260502000600_split_access_points.py is what
--   carries forward existing filesystem CLI keys. THIS migration just
--   bootstraps the root scope so the table is non-empty; the script
--   then UPDATEs the access_key on each backfilled root scope to match
--   the project's pre-existing filesystem AP key (if one exists), so old
--   `mut connect` clients continue to work.
--
-- Idempotency
--   ON CONFLICT DO NOTHING via the (project_id, path) UNIQUE — re-running
--   skips projects that already have a root scope.
-- ============================================================================

BEGIN;

-- Helper: random urlsafe-ish 32-char key. Postgres doesn't have a true
-- urlsafe base64 builtin, so we use encode(gen_random_bytes(24), 'base64')
-- (32 chars before padding) and strip the +/= chars. Good enough for a
-- backfill key — the data migration script will overwrite it anyway when
-- a real cli_xxx key is available.
DO $$
DECLARE
    proj RECORD;
    key_str TEXT;
BEGIN
    FOR proj IN
        SELECT p.id
          FROM public.projects p
         WHERE NOT EXISTS (
             SELECT 1 FROM public.repo_scopes s
              WHERE s.project_id = p.id AND s.is_root = TRUE
         )
    LOOP
        key_str := 'cli_' || translate(
            encode(gen_random_bytes(24), 'base64'),
            '+/=',
            '___'
        );

        INSERT INTO public.repo_scopes
            (project_id, name, path, exclude, mode, is_root, access_key)
        VALUES
            (proj.id, 'Root', '', '[]'::JSONB, 'rw', TRUE, key_str)
        ON CONFLICT (project_id, path) DO NOTHING;
    END LOOP;
END $$;

-- ── Sanity check: every project has exactly one root scope after this ─────
DO $$
DECLARE
    missing INTEGER;
BEGIN
    SELECT COUNT(*) INTO missing
      FROM public.projects p
     WHERE NOT EXISTS (
         SELECT 1 FROM public.repo_scopes s
          WHERE s.project_id = p.id AND s.is_root = TRUE
     );
    IF missing > 0 THEN
        RAISE EXCEPTION 'Backfill failed: % project(s) missing root scope', missing;
    END IF;
END $$;

COMMIT;
