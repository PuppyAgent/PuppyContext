-- ============================================================================
-- sync_runs → connector_runs (rename, preserve data)
-- ============================================================================
-- Why
--   `sync_runs` was always run-history for what we now call connectors.
--   Renaming brings the table in line with the new vocabulary so SQL queries
--   read naturally ("which connector run failed last week" rather than
--   "which sync run").
--
--   See docs/design/access-point-redesign-2026-05-02.md (Q3, section 5.5).
--
-- Behavior
--   Pure rename — no row deletes, no column type changes. PostgreSQL's
--   RENAME TABLE preserves indexes, FKs, RLS policies, sequences.
--
--   The FK column `access_point_id` becomes `connector_id`. Same shape, same
--   contents (the underlying ID format is unchanged because connectors and
--   the old access_points both use TEXT UUIDs from `gen_random_uuid()::TEXT`).
--   The column will start pointing at `connectors.id` once the data
--   migration in 20260502000600_split_access_points.py runs and rewrites
--   each row to the new connector_id; until then it still points at
--   access_points.id and the FK constraint is left absent (it was already
--   loose; cf. 20260401000000:103 which renamed without re-adding FK).
--
-- Idempotency
--   Wrapped in DO blocks that check current state before renaming. Safe to
--   re-run.
-- ============================================================================

BEGIN;

-- ── Rename table ───────────────────────────────────────────────────────────
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'sync_runs'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'connector_runs'
    ) THEN
        ALTER TABLE public.sync_runs RENAME TO connector_runs;
    END IF;
END $$;

-- ── Rename FK column ───────────────────────────────────────────────────────
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'connector_runs'
          AND column_name = 'access_point_id'
    ) THEN
        ALTER TABLE public.connector_runs RENAME COLUMN access_point_id TO connector_id;
    END IF;
END $$;

-- ── Rename indexes for clarity (purely cosmetic; not safety-critical) ──────
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public' AND indexname = 'idx_sync_runs_access_point_id'
    ) THEN
        ALTER INDEX public.idx_sync_runs_access_point_id
            RENAME TO idx_connector_runs_connector_id;
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public' AND indexname = 'idx_sync_runs_started_at'
    ) THEN
        ALTER INDEX public.idx_sync_runs_started_at
            RENAME TO idx_connector_runs_started_at;
    END IF;
END $$;

-- ── Refresh RLS policy name (avoid stale name confusing pg_policies dumps) ─
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'connector_runs'
          AND policyname = 'service_role_all_sync_runs'
    ) THEN
        DROP POLICY "service_role_all_sync_runs" ON public.connector_runs;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'connector_runs'
          AND policyname = 'connector_runs_service_role_all'
    ) THEN
        CREATE POLICY "connector_runs_service_role_all"
            ON public.connector_runs
            FOR ALL TO service_role
            USING (true) WITH CHECK (true);
    END IF;
END $$;

COMMIT;
