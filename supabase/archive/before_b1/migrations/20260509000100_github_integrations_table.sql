-- ============================================================================
-- github_integrations — bind a project to a GitHub repo + branch
-- ============================================================================
-- Why
--   The MUT ↔ git compatibility strategy adds a GitHub Integration layer
--   at the PuppyOne platform level (docs/mut-git-compatibility-strategy.md
--   §"变更 1" and docs/design/mut-git-puppyone-adaptation-plan.md §4.2).
--   It bridges a GitHub repo+branch with a MUT repo (= a PuppyOne project)
--   in two directions:
--
--     * **Import**: pull the named branch's HEAD tree from GitHub and
--       commit it into MUT as a single snapshot (no commit-history
--       transfer).
--     * **Export**: take the MUT scope's current snapshot and create a
--       commit on the configured GitHub branch.
--
--   Each project binds to at most one (github_repo, branch) pair —
--   matching the design's "one git branch ↔ one MUT repo" rule
--   (see ``projects.bound_git_branch`` from
--   20260509000000_projects_bound_git_branch.sql).
--
--   OAuth tokens are not stored on this row — they live on
--   ``oauth_connections`` (see 20260306085814_qubits_schema.sql / docs)
--   and are referenced via ``oauth_connection_id``. That keeps token
--   refresh + revocation in one place.
--
--   Sync history (every import/export attempt + outcome) goes to the
--   companion ``github_sync_log`` table (next migration); this table
--   only stores the configuration and the last-known sync watermark.
--
--   Relationship to ``connectors`` rows with ``provider='github'``
--   --------------------------------------------------------------
--   The existing ``connectors`` table already supports
--   ``provider='github'`` (see 20260502000100_connectors_table.sql and
--   ``backend/src/connectors/datasource/github/connector.py``). That
--   row represents a SCOPE-LEVEL one-shot import (URL-based ZIP fetch,
--   no branch awareness, ``inbound`` direction only) and predates the
--   git-compatibility design.
--
--   ``github_integrations`` is a different concept: PROJECT-LEVEL
--   bidirectional binding to a specific (repo, branch) pair, with
--   webhook-driven incremental sync, watermarked import/export, and
--   ``projects.bound_git_branch`` enforcement. The two tables can
--   coexist in the same project — they encode different intents — but
--   over time the ``connectors[github]`` flow is expected to fold into
--   this one and that connector row becomes a hint for the UI rather
--   than the binding's source of truth. No data migration is forced
--   today; existing scope-level github connectors keep working
--   unchanged.
--
-- What
--   1. Create ``public.github_integrations`` (one row per project max).
--   2. Index on ``oauth_connection_id`` for "list my GitHub-bound
--      projects" reverse lookup.
--   3. Auto-update ``updated_at`` via a small trigger (matches the
--      ``connectors`` table pattern at
--      20260502000100_connectors_table.sql).
--   4. RLS: enabled, service_role full access (frontend goes through
--      backend, so per-user RLS isn't needed at this level).
--
-- Idempotency
--   * ``CREATE TABLE IF NOT EXISTS`` + ``CREATE INDEX IF NOT EXISTS``.
--   * ``CREATE OR REPLACE FUNCTION`` for the bump-trigger function.
--   * ``DROP TRIGGER IF EXISTS`` + ``CREATE TRIGGER`` for the trigger.
--   * ``DROP POLICY IF EXISTS`` + ``CREATE POLICY`` for the RLS policy.
--
-- See also
--   * docs/design/mut-git-puppyone-adaptation-plan.md §3 (D4)
--   * docs/mut-git-compatibility-strategy.md §"变更 1"
--   * 20260502000100_connectors_table.sql (trigger pattern reference)
-- ============================================================================

BEGIN;

-- ── 1. Table ───────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.github_integrations (
    id                       TEXT        PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
    project_id               TEXT        NOT NULL UNIQUE
                                          REFERENCES public.projects(id)
                                          ON DELETE CASCADE,
    -- The OAuth connection providing the GitHub access token. NULLable
    -- because a project may have its integration configuration recorded
    -- before the user finishes the OAuth flow (or after they
    -- disconnect — we keep the row to preserve last-imported watermark).
    --
    -- Type matches ``oauth_connections.id`` (BIGINT) — see the original
    -- definition in 20260306085814_qubits_schema.sql.
    oauth_connection_id      BIGINT      REFERENCES public.oauth_connections(id)
                                          ON DELETE SET NULL,

    -- GitHub repo coordinates. Stored separately rather than as a single
    -- "owner/name" string so case-changes on either side don't break
    -- joins, and so we can render the two pieces independently in UI.
    -- Non-empty CHECK rules out half-configured rows ('' / NULL → empty)
    -- that would otherwise pass NOT NULL validation but break GitHub
    -- API calls downstream.
    github_repo_owner        TEXT        NOT NULL
                                          CHECK (length(github_repo_owner) > 0),
    github_repo_name         TEXT        NOT NULL
                                          CHECK (length(github_repo_name) > 0),

    -- The branch this project syncs against. Independent of (but
    -- typically equal to) ``projects.bound_git_branch`` — both record
    -- the same fact but the binding lives on projects so the MUT
    -- protocol layer can read it without joining github_integrations.
    default_branch           TEXT        NOT NULL DEFAULT 'main'
                                          CHECK (length(default_branch) > 0),

    -- HMAC-SHA256 secret used to verify ``X-Hub-Signature-256`` on
    -- inbound webhooks. NULL = no webhook configured (manual import only).
    webhook_secret           TEXT,

    -- When TRUE, push events on the configured branch trigger an
    -- automatic import via the ARQ worker. When FALSE, imports are
    -- manual-only via ``POST /github/import``.
    auto_import              BOOLEAN     NOT NULL DEFAULT FALSE,

    -- Last successful import: which GitHub commit SHA we ingested,
    -- which MUT commit_id resulted, and when. NULL = never imported.
    last_imported_sha        TEXT,
    last_imported_at         TIMESTAMPTZ,

    -- Last successful export: which MUT commit we sent to GitHub, the
    -- resulting GitHub commit SHA, and when.
    last_exported_sha        TEXT,
    last_exported_at         TIMESTAMPTZ,

    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Semantic invariant: auto_import requires a webhook to actually
    -- fire, so storing ``auto_import=TRUE`` with NULL webhook_secret
    -- would create a row that's silently dead. Reject the combination
    -- at the schema level so a bug in the UI/service layer can't
    -- produce one.
    CONSTRAINT github_integrations_auto_import_needs_webhook
        CHECK (auto_import = FALSE OR webhook_secret IS NOT NULL)
);


-- ── 2. Indexes ─────────────────────────────────────────────────────────────

-- Reverse lookup: given an OAuth connection (e.g. when revoking),
-- find every project bound to it.
CREATE INDEX IF NOT EXISTS idx_github_integrations_oauth_connection
    ON public.github_integrations (oauth_connection_id)
    WHERE oauth_connection_id IS NOT NULL;

-- Reverse lookup: given a (owner, repo) pair, find any binding. Useful
-- when receiving a webhook — GitHub doesn't tell us our project_id, we
-- have to look it up by repo coordinates.
CREATE INDEX IF NOT EXISTS idx_github_integrations_repo_coords
    ON public.github_integrations (github_repo_owner, github_repo_name);


-- ── 3. updated_at auto-bump ────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION public._github_integrations_bump_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_github_integrations_updated_at
    ON public.github_integrations;
CREATE TRIGGER trg_github_integrations_updated_at
    BEFORE UPDATE ON public.github_integrations
    FOR EACH ROW
    EXECUTE FUNCTION public._github_integrations_bump_updated_at();


-- ── 4. RLS ─────────────────────────────────────────────────────────────────

ALTER TABLE public.github_integrations ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS github_integrations_service_role_all
    ON public.github_integrations;
CREATE POLICY github_integrations_service_role_all
    ON public.github_integrations
    FOR ALL
    TO service_role
    USING (true) WITH CHECK (true);

COMMIT;
