-- ============================================================================
-- Version transactions + structured conflict records + audit_logs columns
-- ============================================================================
-- Implements:
--   * B2 — `version_transactions` table (07-version-engine-todo.md)
--   * B3 — `mut_conflicts` table for structured pending-conflict records
--   * D4 — first-class columns on `audit_logs` so the activity stream
--          can join cleanly to version facts without parsing JSONB
--          (commit_id stays nullable since rejected / pending rows have
--          no commit).
-- ============================================================================

BEGIN;

-- ── version_transactions ─────────────────────────────────────────
-- Every write intent gets one row. Status transitions follow
-- 01-version-engine.md §6:
--   received → validated → policy_selected → (rejected | pending_* | resolving)
--   → publish_attempt → (committed | rejected | retryable_conflict)

CREATE TABLE IF NOT EXISTS public.version_transactions (
    id                          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id                  TEXT NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,
    scope_path                  TEXT NOT NULL DEFAULT '',
    source_channel              TEXT NOT NULL,
    actor                       TEXT NOT NULL DEFAULT '',
    intent_type                 TEXT NOT NULL,
    status                      TEXT NOT NULL DEFAULT 'received',
    policy                      TEXT NOT NULL DEFAULT '',
    base_commit_id              TEXT NOT NULL DEFAULT '',
    client_commit_id            TEXT NOT NULL DEFAULT '',
    proposed_tree_id            TEXT NOT NULL DEFAULT '',
    current_head_at_start       TEXT NOT NULL DEFAULT '',
    committed_commit_id         TEXT NOT NULL DEFAULT '',
    project_view_commit_id      TEXT NOT NULL DEFAULT '',
    message                     TEXT NOT NULL DEFAULT '',
    audit_detail                JSONB NOT NULL DEFAULT '{}'::JSONB,
    reason                      TEXT NOT NULL DEFAULT '',
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE public.version_transactions
    DROP CONSTRAINT IF EXISTS version_transactions_status_valid;

ALTER TABLE public.version_transactions
    ADD CONSTRAINT version_transactions_status_valid
        CHECK (status IN (
            'received',
            'validated',
            'policy_selected',
            'pending_manual_review',
            'pending_agent_resolution',
            'resolving',
            'publish_attempt',
            'committed',
            'rejected',
            'retryable_conflict'
        ));

ALTER TABLE public.version_transactions
    DROP CONSTRAINT IF EXISTS version_transactions_intent_type_valid;

ALTER TABLE public.version_transactions
    ADD CONSTRAINT version_transactions_intent_type_valid
        CHECK (intent_type IN (
            'operation',
            'submission',
            'rollback',
            'resolution'
        ));

CREATE INDEX IF NOT EXISTS idx_version_transactions_project_status
    ON public.version_transactions (project_id, status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_version_transactions_committed_commit
    ON public.version_transactions (project_id, committed_commit_id)
    WHERE committed_commit_id <> '';

ALTER TABLE public.version_transactions ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'version_transactions'
          AND policyname = 'version_transactions_service_role_all'
    ) THEN
        CREATE POLICY "version_transactions_service_role_all"
            ON public.version_transactions
            FOR ALL TO service_role
            USING (true) WITH CHECK (true);
    END IF;
END $$;

-- ── mut_conflicts ────────────────────────────────────────────────
-- Structured records for pending conflicts. The engine writes one row
-- whenever a transaction enters `pending_manual_review` or
-- `pending_agent_resolution`. ConflictResolutionIntent looks up the row
-- and re-enters the publish pipeline.

CREATE TABLE IF NOT EXISTS public.mut_conflicts (
    id                      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    pending_conflict_id     TEXT NOT NULL UNIQUE,
    transaction_id          BIGINT REFERENCES public.version_transactions(id) ON DELETE CASCADE,
    project_id              TEXT NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,
    scope_path              TEXT NOT NULL DEFAULT '',
    base_commit_id          TEXT NOT NULL DEFAULT '',
    base_tree_id            TEXT NOT NULL DEFAULT '',
    current_commit_id       TEXT NOT NULL DEFAULT '',
    current_tree_id         TEXT NOT NULL DEFAULT '',
    client_commit_id        TEXT NOT NULL DEFAULT '',
    proposed_tree_id        TEXT NOT NULL DEFAULT '',
    changed_paths           JSONB NOT NULL DEFAULT '[]'::JSONB,
    conflict_records        JSONB NOT NULL DEFAULT '[]'::JSONB,
    policy                  TEXT NOT NULL DEFAULT 'manual_review',
    status                  TEXT NOT NULL DEFAULT 'pending',
    resolver_actor          TEXT NOT NULL DEFAULT '',
    resolver_kind           TEXT NOT NULL DEFAULT '',
    resolution_commit_id    TEXT NOT NULL DEFAULT '',
    resolution_detail       JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at             TIMESTAMPTZ
);

ALTER TABLE public.mut_conflicts
    DROP CONSTRAINT IF EXISTS mut_conflicts_status_valid;

ALTER TABLE public.mut_conflicts
    ADD CONSTRAINT mut_conflicts_status_valid
        CHECK (status IN ('pending', 'resolving', 'resolved', 'rejected'));

CREATE INDEX IF NOT EXISTS idx_mut_conflicts_project_status
    ON public.mut_conflicts (project_id, status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_mut_conflicts_scope
    ON public.mut_conflicts (project_id, scope_path, status);

ALTER TABLE public.mut_conflicts ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'mut_conflicts'
          AND policyname = 'mut_conflicts_service_role_all'
    ) THEN
        CREATE POLICY "mut_conflicts_service_role_all"
            ON public.mut_conflicts
            FOR ALL TO service_role
            USING (true) WITH CHECK (true);
    END IF;
END $$;

-- ── audit_logs first-class columns ───────────────────────────────
-- Today the engine writes the full transaction state into
-- audit_logs.metadata JSONB. Lifting the most-joined fields into
-- typed columns makes the activity feed cheap to query and matches
-- 01-version-engine.md §13. commit_id stays nullable because
-- rejected and pending rows have no commit.

ALTER TABLE public.audit_logs
    ADD COLUMN IF NOT EXISTS transaction_id           BIGINT,
    ADD COLUMN IF NOT EXISTS canonical_commit_id      TEXT,
    ADD COLUMN IF NOT EXISTS original_commit_id       TEXT,
    ADD COLUMN IF NOT EXISTS project_view_commit_id   TEXT,
    ADD COLUMN IF NOT EXISTS scope_view_commit_id     TEXT,
    ADD COLUMN IF NOT EXISTS scope_path               TEXT,
    ADD COLUMN IF NOT EXISTS source_channel           TEXT,
    ADD COLUMN IF NOT EXISTS policy                   TEXT,
    ADD COLUMN IF NOT EXISTS status                   TEXT;

CREATE INDEX IF NOT EXISTS idx_audit_logs_transaction
    ON public.audit_logs (transaction_id)
    WHERE transaction_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_audit_logs_canonical_commit
    ON public.audit_logs (project_id, canonical_commit_id)
    WHERE canonical_commit_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_audit_logs_scope_channel
    ON public.audit_logs (project_id, scope_path, source_channel, created_at DESC);

COMMIT;
