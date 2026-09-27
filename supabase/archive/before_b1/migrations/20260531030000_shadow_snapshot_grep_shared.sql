-- ============================================================================
-- local_shadow_snapshots.grep_shared — explicit opt-in for cross-user grep
-- ============================================================================
-- Why
--   docs/architecture/08-shadow-snapshots.md §1: "Shadow content is
--   user-private by default; no other teammate can read another user's
--   shadow snapshots without an explicit opt-in." The `--ref local:` grep
--   (GAP-11) resolved a snapshot by (project_id, machine_id, ref_name)
--   only — never by the caller — so ANY access-point holder for the
--   project could grep ANY user's un-pushed working tree. There was no
--   opt-in mechanism to gate that.
--
--   This adds the opt-in flag. A snapshot is grep-able through the shared
--   `--ref local:` path ONLY when its owner sets grep_shared = TRUE; the
--   default (FALSE) keeps shadow content user-private, matching the doc.
--   The per-user authenticated endpoints (list/get/delete) are already
--   user-scoped and unaffected.
--
-- Idempotency: ADD COLUMN IF NOT EXISTS — safe to re-run.
-- ============================================================================

BEGIN;

ALTER TABLE public.local_shadow_snapshots
    ADD COLUMN IF NOT EXISTS grep_shared BOOLEAN NOT NULL DEFAULT FALSE;

NOTIFY pgrst, 'reload schema';

COMMIT;
