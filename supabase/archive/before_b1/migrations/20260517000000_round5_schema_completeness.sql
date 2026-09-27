-- ============================================================================
-- Round 5: schema completeness for V1 version engine
-- ============================================================================
-- Bundles five small migrations into one file so the deploy is a single
-- atomic step:
--
--   D6  More indexes for ``mut_conflicts`` and ``version_transactions``
--       (the Round 2 migration covered the headline cases; this adds
--       the indexes the conflict-resolver UI + audit join view need).
--   D7  ``local_shadow_snapshots`` table for the local-↔-cloud bridge.
--   D8  ``fs_path_index`` materialised path/blob/metadata table for
--       large-project ``puppyone fs find / stat`` performance.
--   J1  Backfill the new typed ``audit_logs`` columns from existing
--       ``metadata`` JSONB rows so historical events join cleanly.
--   J2  Read-only view ``version_activity_feed`` that joins audit_logs,
--       version_transactions, and mut_conflicts in one row per event.
-- ============================================================================

BEGIN;

-- ── D6: additional indexes ──────────────────────────────────────────

-- The resolver UI lists pending conflicts by project; the FK column
-- needs an index so the audit join view (J2) is fast.
CREATE INDEX IF NOT EXISTS idx_mut_conflicts_transaction
    ON public.mut_conflicts (transaction_id)
    WHERE transaction_id IS NOT NULL;

-- The activity feed orders by transaction created_at across the join.
CREATE INDEX IF NOT EXISTS idx_version_transactions_committed
    ON public.version_transactions (project_id, committed_commit_id)
    WHERE committed_commit_id <> '';

-- Mut_version_outbox now carries non-commit events (B13a); pending dispatch
-- claims by event_type to skip the expensive version-committed branch.
CREATE INDEX IF NOT EXISTS idx_mut_version_outbox_event
    ON public.mut_version_outbox (event_type, processed_at)
    WHERE processed_at IS NULL;


-- ── D7: shadow snapshots ────────────────────────────────────────────
-- A shadow snapshot is the manifest of a client's working-tree state
-- that hasn't been pushed yet (07-version-engine-supplement.md §5).
-- It is *user-private* by default and never becomes a real commit
-- until the user explicitly promotes it.

CREATE TABLE IF NOT EXISTS public.local_shadow_snapshots (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      TEXT NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,
    user_id         UUID NOT NULL,           -- the local client's owner
    machine_id      TEXT NOT NULL DEFAULT '',
    ref_name        TEXT NOT NULL DEFAULT '',-- e.g. "main"; the local branch
    -- The manifest is a JSON array of {path, mode, blob_hash, size}
    -- entries, plus a tree_hash if the client already computed one.
    manifest        JSONB NOT NULL DEFAULT '[]'::JSONB,
    tree_hash       TEXT NOT NULL DEFAULT '',
    blob_hashes     JSONB NOT NULL DEFAULT '[]'::JSONB, -- distinct hashes for cheap queries
    file_count      INT NOT NULL DEFAULT 0,
    total_bytes     BIGINT NOT NULL DEFAULT 0,
    -- Optional preview lines for fast `puppyone fs grep --ref local:`
    -- when the actual blob isn't yet on the server.
    previews        JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (project_id, user_id, machine_id, ref_name)
);

CREATE INDEX IF NOT EXISTS idx_shadow_snapshots_project_user
    ON public.local_shadow_snapshots (project_id, user_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_shadow_snapshots_machine
    ON public.local_shadow_snapshots (project_id, user_id, machine_id);

ALTER TABLE public.local_shadow_snapshots ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    -- Service-role owns ingest; users will read their own snapshots
    -- through an authenticated API route, not direct RLS. We keep the
    -- table service-role-only for the V1 cut.
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'local_shadow_snapshots'
          AND policyname = 'shadow_snapshots_service_role_all'
    ) THEN
        CREATE POLICY "shadow_snapshots_service_role_all"
            ON public.local_shadow_snapshots
            FOR ALL TO service_role
            USING (true) WITH CHECK (true);
    END IF;
END $$;


-- ── D8: fs_path_index ───────────────────────────────────────────────
-- One row per (project, scope, path) so ``puppyone fs find`` and
-- ``puppyone fs stat`` can answer in O(log n) instead of walking S3.
-- The index is *derived* from mut_scope_state + S3 trees: anything
-- here can be rebuilt by replaying the outbox.

CREATE TABLE IF NOT EXISTS public.fs_path_index (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,
    scope_path      TEXT NOT NULL DEFAULT '',
    full_path       TEXT NOT NULL,            -- repo-relative path
    blob_hash       TEXT NOT NULL DEFAULT '',
    size_bytes      BIGINT NOT NULL DEFAULT 0,
    mime_type       TEXT NOT NULL DEFAULT '',
    last_who        TEXT NOT NULL DEFAULT '',
    last_commit_id  TEXT NOT NULL DEFAULT '',
    last_updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (project_id, full_path)
);

CREATE INDEX IF NOT EXISTS idx_fs_path_index_project_scope
    ON public.fs_path_index (project_id, scope_path, full_path);

CREATE INDEX IF NOT EXISTS idx_fs_path_index_recent
    ON public.fs_path_index (project_id, last_updated_at DESC);

-- pg_trgm for cheap LIKE/ILIKE on path globs — used by the v1
-- ``puppyone fs find -name '*.md'`` translation.
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX IF NOT EXISTS idx_fs_path_index_path_trgm
    ON public.fs_path_index USING gin (full_path gin_trgm_ops);

ALTER TABLE public.fs_path_index ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'fs_path_index'
          AND policyname = 'fs_path_index_service_role_all'
    ) THEN
        CREATE POLICY "fs_path_index_service_role_all"
            ON public.fs_path_index
            FOR ALL TO service_role
            USING (true) WITH CHECK (true);
    END IF;
END $$;


-- ── J1: backfill audit_logs typed columns ───────────────────────────
-- The Round 2 migration added the columns but only newly-published
-- writes populate them. Historical rows keep the data in
-- ``metadata``; pull it out here so the activity feed view (J2) is
-- consistent across the whole project history.

UPDATE public.audit_logs
   SET source_channel = COALESCE(NULLIF(source_channel, ''),
                                 metadata->>'source_channel',
                                 -- Heuristic: legacy MUT writes pre-migration carried
                                 -- "mut_*" or "git_*" event names, so infer the channel
                                 -- from the action when metadata lacks it.
                                 CASE
                                     WHEN action LIKE 'git_%'    THEN 'git'
                                     WHEN action LIKE 'mut_%'    THEN 'mut'
                                     WHEN action LIKE 'papi_%'   THEN 'papi'
                                     WHEN action LIKE 'agent_%'  THEN 'agent'
                                     ELSE ''
                                 END)
 WHERE source_channel IS NULL OR source_channel = '';

UPDATE public.audit_logs
   SET scope_path = COALESCE(NULLIF(scope_path, ''),
                             metadata->>'scope_path',
                             metadata->>'scope')
 WHERE scope_path IS NULL OR scope_path = '';

UPDATE public.audit_logs
   SET canonical_commit_id = COALESCE(NULLIF(canonical_commit_id, ''),
                                      metadata->>'commit_id')
 WHERE canonical_commit_id IS NULL OR canonical_commit_id = '';

UPDATE public.audit_logs
   SET status = COALESCE(NULLIF(status, ''),
                         metadata->>'status',
                         -- Map action suffixes to a status when metadata is bare.
                         CASE
                             WHEN action LIKE '%_pending'    THEN 'pending'
                             WHEN action LIKE '%_rejected'   THEN 'rejected'
                             WHEN action LIKE '%_resolved'   THEN 'resolved'
                             ELSE 'committed'
                         END)
 WHERE (status IS NULL OR status = '')
   AND (action LIKE '%_push%' OR action LIKE '%_write%' OR action LIKE '%commit%');

UPDATE public.audit_logs
   SET policy = COALESCE(NULLIF(policy, ''),
                         metadata->>'policy')
 WHERE policy IS NULL OR policy = '';


-- ── J2: activity-feed join view ─────────────────────────────────────
-- One row per audit event, with the linked version_transactions and
-- mut_conflicts rows folded in. The admin UI's activity feed renders
-- directly off this view; the engine never writes to it.

CREATE OR REPLACE VIEW public.version_activity_feed AS
SELECT
    al.id                        AS audit_id,
    al.created_at                AS event_at,
    al.project_id,
    al.action                    AS event_type,
    al.operator_type,
    al.operator_id,
    al.scope_path,
    al.source_channel,
    al.policy,
    al.status,
    al.canonical_commit_id,
    al.original_commit_id,
    al.project_view_commit_id,
    al.scope_view_commit_id,
    al.metadata                  AS audit_metadata,
    vt.id                        AS transaction_id,
    vt.status                    AS transaction_status,
    vt.intent_type,
    vt.actor                     AS transaction_actor,
    vt.base_commit_id,
    vt.client_commit_id,
    vt.proposed_tree_id,
    vt.current_head_at_start,
    vt.committed_commit_id,
    vt.reason                    AS transaction_reason,
    mc.pending_conflict_id,
    mc.status                    AS conflict_status,
    mc.resolver_actor,
    mc.resolver_kind,
    mc.resolution_commit_id,
    mc.changed_paths             AS conflict_changed_paths
FROM public.audit_logs al
LEFT JOIN public.version_transactions vt
       ON vt.id = al.transaction_id
LEFT JOIN public.mut_conflicts mc
       ON mc.transaction_id = vt.id;

COMMENT ON VIEW public.version_activity_feed IS
    'V1 activity feed: one row per audit event, joined to the linked '
    'version_transactions row (if any) and mut_conflicts row (if any). '
    'Read-only; the engine writes to the three base tables, never here.';

GRANT SELECT ON public.version_activity_feed TO service_role;

COMMIT;
