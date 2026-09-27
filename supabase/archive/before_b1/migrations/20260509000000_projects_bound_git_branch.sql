-- ============================================================================
-- Add ``bound_git_branch`` to projects — a project IS one git branch.
-- ============================================================================
-- Why
--   The MUT ↔ git compatibility design (docs/design/mut-git-puppyone-adaptation-plan.md
--   §1, and the source-of-truth strategy doc docs/mut-git-compatibility-strategy.md
--   §"变更 2") commits to a 1:1 mapping between MUT repos and git branches:
--
--     * Each PuppyOne project corresponds to exactly one (github repo,
--       branch) pair.
--     * Switching git branch in the user's local clone means cloning a
--       different MUT project, not mutating this one.
--     * The MUT CLI already enforces this on the client by recording
--       ``[mut] bound-branch`` in ``.git/config`` (see ``mut/foundation/branch.py``
--       in the ``mut/`` repo at commit 70e3483) and refusing push/pull
--       when the working-tree branch diverges from the binding.
--
--   The server-side equivalent lives on ``projects``: every project
--   declares which git branch it represents. GitHub Integration uses it
--   to know which branch to import from / export to; the MUT protocol
--   layer can later use it to validate ``X-Mut-Branch`` request headers.
--
--   Default ``'main'`` matches GitHub's modern default and what
--   ``mut init`` writes when no current branch can be detected.
--
--   See: docs/design/mut-git-puppyone-adaptation-plan.md §3 (D3).
--
-- What
--   1. Add ``bound_git_branch TEXT NOT NULL DEFAULT 'main'`` to ``public.projects``.
--   2. Backfill existing rows to ``'main'`` (covered by the DEFAULT on the
--      ``ALTER TABLE … ADD COLUMN`` for new rows AND for existing rows).
--   3. Add a CHECK constraint that the branch name is non-empty —
--      cheaper than NOT-EMPTY validation in the application layer and
--      matches git's own constraint (refs can't be empty).
--
-- Idempotency
--   * ``ADD COLUMN IF NOT EXISTS`` for the column.
--   * ``DROP CONSTRAINT IF EXISTS`` + ``ADD CONSTRAINT`` for the CHECK
--     (Postgres has no ``ADD CONSTRAINT IF NOT EXISTS``).
--   * No data backfill statement: ``ADD COLUMN … DEFAULT 'main'`` already
--     populates existing rows with the default value.
-- ============================================================================

BEGIN;

-- ── 1. Add bound_git_branch column ─────────────────────────────────────────

ALTER TABLE public.projects
    ADD COLUMN IF NOT EXISTS bound_git_branch TEXT NOT NULL DEFAULT 'main';


-- ── 2. CHECK constraint: branch name must be non-empty ─────────────────────

ALTER TABLE public.projects
    DROP CONSTRAINT IF EXISTS projects_bound_git_branch_nonempty;

ALTER TABLE public.projects
    ADD CONSTRAINT projects_bound_git_branch_nonempty
        CHECK (length(bound_git_branch) > 0);


-- ── 3. Sanity check ────────────────────────────────────────────────────────
-- Every existing project should now have a non-empty bound_git_branch.

DO $$
DECLARE
    missing INTEGER;
BEGIN
    SELECT COUNT(*) INTO missing
      FROM public.projects
     WHERE bound_git_branch IS NULL OR length(bound_git_branch) = 0;
    IF missing > 0 THEN
        RAISE EXCEPTION
            'projects.bound_git_branch backfill failed: % row(s) NULL or empty',
            missing;
    END IF;
END $$;

COMMIT;
