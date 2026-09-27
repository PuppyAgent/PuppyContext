-- ============================================================================
-- github_sync_log — every import/export attempt and its outcome
-- ============================================================================
-- Why
--   Each ``github_integrations`` row stores only the *latest successful*
--   import/export watermark. To diagnose failures, render a sync history
--   in the UI ("settings → GitHub → Sync log"), and dedupe duplicate
--   webhook deliveries from GitHub, we keep an append-only audit log
--   with one row per sync attempt.
--
--   Webhook duplicate detection: GitHub retries a webhook delivery up to
--   ~30 minutes if the receiver doesn't 200. The unique index
--   ``(integration_id, direction, git_sha)`` makes the importer's
--   ``ON CONFLICT DO NOTHING`` safely no-op on retries — exactly the
--   guarantee called out in
--   docs/design/mut-git-puppyone-adaptation-plan.md §7 ("Webhook
--   duplicate fires").
--
--   We do NOT mirror the rows back into the existing audit_logs table —
--   that table is keyed on (project_id, node_id) and tracks USER actions
--   on content, while github_sync_log tracks PLATFORM-side syncs. Mixing
--   them would either pollute audit_logs with rows whose ``who`` is the
--   GitHub webhook, or force a confusing ``provider='github_sync'`` row
--   shape on a table that wasn't designed for it.
--
-- What
--   1. Create ``public.github_sync_log`` (append-only).
--   2. Index ``(integration_id, created_at DESC)`` for "most recent first"
--      list queries from the UI.
--   3. UNIQUE ``(integration_id, direction, git_sha)`` — webhook idempotency.
--      Composite is correct: an import and an export of the same git_sha
--      are different events.
--   4. RLS: enabled, service_role full access.
--
-- Idempotency
--   * ``CREATE TABLE IF NOT EXISTS`` + ``CREATE … INDEX IF NOT EXISTS``.
--   * No data backfill; this is a fresh log.
--
-- See also
--   * docs/design/mut-git-puppyone-adaptation-plan.md §3 (D5)
--   * 20260509000100_github_integrations_table.sql (parent table)
-- ============================================================================

BEGIN;

-- ── 1. Table ───────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.github_sync_log (
    id                  TEXT        PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
    integration_id      TEXT        NOT NULL
                                     REFERENCES public.github_integrations(id)
                                     ON DELETE CASCADE,

    -- 'import' = GitHub → MUT; 'export' = MUT → GitHub.
    direction           TEXT        NOT NULL
                                     CHECK (direction IN ('import', 'export')),

    -- Git side: the GitHub commit SHA being imported, or the resulting
    -- GitHub commit SHA after a successful export. Always 40-char hex
    -- when present (NULL allowed for failed exports that never reached
    -- GitHub).
    git_sha             TEXT,

    -- MUT side: the ``mut_commits.commit_id`` that resulted from this
    -- sync. NULL when the import failed before producing a MUT commit.
    -- Stored as TEXT (no length constraint) — the contract is now
    -- 40-char SHA-1 (per the mut/ feat/git-format-storage migration),
    -- but the column doesn't enforce length so older 16-char rows
    -- created before that branch ships still validate.
    mut_commit_id       TEXT,

    -- Lifecycle:
    --   pending  — webhook accepted; ARQ job queued but not yet run.
    --   success  — sync completed; both git_sha and mut_commit_id set.
    --   failed   — fatal error mid-sync; error_message populated.
    --   conflict — refused because target side has unpushed changes
    --              (MUT has work the user hasn't exported, or GitHub
    --              has commits we haven't imported). Surfaced in UI
    --              for the user to resolve.
    status              TEXT        NOT NULL
                                     CHECK (status IN
                                            ('pending', 'success',
                                             'failed', 'conflict')),

    error_message       TEXT,

    -- Number of files added/modified/deleted in this sync. Used by the
    -- UI to render "imported 17 files" tags. NULL for failed syncs
    -- where we never enumerated the change set.
    files_changed       INTEGER,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ── 2. Indexes ─────────────────────────────────────────────────────────────

-- "Most recent first" listing for the per-integration sync log UI.
CREATE INDEX IF NOT EXISTS idx_github_sync_log_integration_recent
    ON public.github_sync_log (integration_id, created_at DESC);

-- Webhook idempotency: a (integration, direction, git_sha) triple
-- should appear at most once with status='success'. Pending / failed
-- rows for the same triple are allowed (retries are normal). We
-- enforce this at the application layer, not as a UNIQUE constraint,
-- because a partial-unique-on-status index on Postgres TEXT works but
-- adds complexity and the importer's existence check is already cheap.
-- The simple non-unique index speeds up that lookup:
CREATE INDEX IF NOT EXISTS idx_github_sync_log_dedupe_lookup
    ON public.github_sync_log (integration_id, direction, git_sha)
    WHERE git_sha IS NOT NULL;


-- ── 3. RLS ─────────────────────────────────────────────────────────────────

ALTER TABLE public.github_sync_log ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS github_sync_log_service_role_all
    ON public.github_sync_log;
CREATE POLICY github_sync_log_service_role_all
    ON public.github_sync_log
    FOR ALL
    TO service_role
    USING (true) WITH CHECK (true);

COMMIT;
