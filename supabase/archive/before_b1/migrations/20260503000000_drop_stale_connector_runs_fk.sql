-- ============================================================================
-- Data-migration prep: drop stale connector_runs FK + create migration_log
-- ============================================================================
-- Two small fixes the Python data-migration script
-- (backend/scripts/migrate_access_points_to_v2.py) needs to complete cleanly.
--
-- 1. Drop stale FK on connector_runs.connector_id → access_points.id
-- ----------------------------------------------------------------------------
-- 20260502000400_sync_runs_rename.sql renamed sync_runs → connector_runs and
-- access_point_id → connector_id, but the FK constraint
-- `sync_runs_sync_id_fkey` (which pointed at access_points(id)) survived the
-- rename — even though its column is now called `connector_id`, the FK still
-- enforces against access_points. That blocks the data migration's rewire
-- step from connector_runs.connector_id (old AP id) → connectors.id (new
-- BIGINT-not-applicable, but TEXT id outside access_points). The rewire now
-- raises 23503 instead of completing.
--
-- 2. Create migration_log table
-- ----------------------------------------------------------------------------
-- The data migration writes a sentinel row to migration_log so
-- 20260502000700_drop_access_points.sql can verify it ran. The Python script
-- creates the table lazily, but only via supabase RPCs (exec_ddl /
-- ensure_migration_log_table) that don't exist by default. Creating the
-- table here removes the lazy-create dependency.
--
-- Idempotency: IF EXISTS / CREATE TABLE IF NOT EXISTS — safe to re-run.
-- ============================================================================

BEGIN;

-- ── 1. Drop stale FK ─────────────────────────────────────────────────────
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'sync_runs_sync_id_fkey'
           AND conrelid = 'public.connector_runs'::regclass
    ) THEN
        ALTER TABLE public.connector_runs
            DROP CONSTRAINT sync_runs_sync_id_fkey;
        RAISE NOTICE 'dropped stale FK sync_runs_sync_id_fkey';
    END IF;
END $$;

-- ── 2. Create migration_log table ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.migration_log (
    name        TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    summary     JSONB
);

ALTER TABLE public.migration_log ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
         WHERE schemaname = 'public'
           AND tablename = 'migration_log'
           AND policyname = 'migration_log_service_role_all'
    ) THEN
        CREATE POLICY "migration_log_service_role_all"
            ON public.migration_log
            FOR ALL TO service_role
            USING (true) WITH CHECK (true);
    END IF;
END $$;

COMMIT;
