-- ============================================================
-- Migration: scope_path canonical form (end-state)
--
-- Problem: mut_commits.scope_path was written in raw form
-- (e.g. "/docs/") while mut_scope_state.scope_path was written
-- in normalized form (e.g. "docs"). This caused
-- get_previous_scope_hash() to miss historical commits on the
-- affected projects, which in turn caused the post-push graft
-- to take a wrong branch: merges were performed with an empty
-- base, resurrecting deleted files when the client deleted a
-- path from a scope.
--
-- End-state: scope_path everywhere uses a single canonical form:
--   * no leading/trailing '/'
--   * empty scope uses '' (never NULL)
--
-- Defense in depth:
--   1. One-shot normalization of existing rows
--   2. BEFORE INSERT/UPDATE trigger — the database enforces the
--      rule even if application code forgets
--   3. CHECK constraint — last line of defense if the trigger
--      is ever dropped / disabled
--
-- Related code change: backend/src/mut_engine/server/backends/
--   supabase_history.py — record() / get_since() now normalize
--   their scope_path arguments on entry (fixes root cause).
-- ============================================================

BEGIN;

-- ────────────────────────────────────────────────────────────
-- PART 1: Normalize existing rows
-- ────────────────────────────────────────────────────────────

UPDATE mut_commits
   SET scope_path = TRIM(BOTH '/' FROM COALESCE(scope_path, ''))
 WHERE scope_path IS DISTINCT FROM TRIM(BOTH '/' FROM COALESCE(scope_path, ''));

UPDATE mut_scope_state
   SET scope_path = TRIM(BOTH '/' FROM COALESCE(scope_path, ''))
 WHERE scope_path IS DISTINCT FROM TRIM(BOTH '/' FROM COALESCE(scope_path, ''));


-- ────────────────────────────────────────────────────────────
-- PART 2: Trigger — automatic normalization on write
-- ────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION public.normalize_scope_path()
RETURNS TRIGGER AS $$
BEGIN
    NEW.scope_path := TRIM(BOTH '/' FROM COALESCE(NEW.scope_path, ''));
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_normalize_scope_path ON mut_commits;
CREATE TRIGGER trg_normalize_scope_path
    BEFORE INSERT OR UPDATE ON mut_commits
    FOR EACH ROW EXECUTE FUNCTION public.normalize_scope_path();

DROP TRIGGER IF EXISTS trg_normalize_scope_path ON mut_scope_state;
CREATE TRIGGER trg_normalize_scope_path
    BEFORE INSERT OR UPDATE ON mut_scope_state
    FOR EACH ROW EXECUTE FUNCTION public.normalize_scope_path();


-- ────────────────────────────────────────────────────────────
-- PART 3: CHECK constraint — last line of defense
-- ────────────────────────────────────────────────────────────
--
-- The trigger rewrites NEW.scope_path before the row lands, so
-- under normal conditions the constraint is never evaluated as
-- false. It exists to catch the case where the trigger is
-- accidentally dropped, disabled, or bypassed by a direct COPY.

ALTER TABLE mut_commits
    DROP CONSTRAINT IF EXISTS scope_path_canonical;
ALTER TABLE mut_commits
    ADD CONSTRAINT scope_path_canonical
    CHECK (scope_path = TRIM(BOTH '/' FROM COALESCE(scope_path, '')));

ALTER TABLE mut_scope_state
    DROP CONSTRAINT IF EXISTS scope_path_canonical;
ALTER TABLE mut_scope_state
    ADD CONSTRAINT scope_path_canonical
    CHECK (scope_path = TRIM(BOTH '/' FROM COALESCE(scope_path, '')));


COMMIT;
