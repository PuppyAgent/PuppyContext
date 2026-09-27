-- ============================================================================
-- projects — add prompt_template column (repo identity merge per Q5)
-- ============================================================================
-- Why
--   The repo's "agent prompt" — the paragraph the user pastes into Claude
--   Code / Cursor / Codex / OpenClaw alongside the repo URL — used to be
--   one of those things buried in access_points.config or computed
--   client-side. Per the redesign Q5, the repo identity (URL + prompt)
--   merges into the projects table directly. The URL is just
--   `<base>/api/v1/mut/<project_id>` so it doesn't need a column; the
--   prompt does.
--
--   See docs/design/access-point-redesign-2026-05-02.md (sections 0, 5.5).
--
-- Default content
--   The DEFAULT below is the canonical mut-protocol primer the agent
--   should read on first connect. It's stored per-project so admins can
--   customize, but the default is what 99% of projects will use.
--
-- Idempotency
--   ADD COLUMN IF NOT EXISTS keeps re-runs safe.
-- ============================================================================

BEGIN;

-- Default prompt template — kept in sync with frontend's GetStartedPanel
-- copy. Plain text (no markdown) so it pastes cleanly into any agent.
DO $$
DECLARE
    default_prompt TEXT;
BEGIN
    default_prompt :=
'You are connected to a PuppyOne repo via the mut protocol.

The mut protocol gives you read+write access to a versioned, scoped subtree of files. You can clone the current state, push your changes back, and pull the latest from other agents working on the same repo.

To work with this repo:
  - Use `mut clone <url>` to fetch the current state of your scope.
  - Use `mut push` to commit and upload your changes.
  - Use `mut pull` to get changes from other agents or web users.

Your working scope is constrained — paths outside the scope are invisible. The repo URL above already encodes which scope you have access to.

When the user asks you to make a change to the repo, prefer making it locally first, running tests if applicable, then `mut push` once the change is verified.';

    -- Add the column if missing.
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'projects'
          AND column_name = 'prompt_template'
    ) THEN
        EXECUTE format(
            'ALTER TABLE public.projects ADD COLUMN prompt_template TEXT NOT NULL DEFAULT %L',
            default_prompt
        );
    END IF;
END $$;

COMMIT;
