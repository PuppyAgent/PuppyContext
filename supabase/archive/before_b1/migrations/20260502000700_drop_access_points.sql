-- ============================================================================
-- DROP access_points (after data migration verified)
-- ============================================================================
-- This migration is GATED on the python data-migration script having run
-- successfully — it drops access_points only if a sentinel row exists in
-- migration_log proving the data was carried into repo_scopes / connectors.
--
--   See backend/scripts/migrate_access_points_to_v2.py and
--       docs/design/access-point-redesign-2026-05-02.md (section 5.7).
--
-- If you're applying migrations on a fresh DB (where access_points was never
-- populated), this still drops the empty table cleanly.
--
-- Idempotency
--   DROP IF EXISTS, plus the sentinel check skips when the table is already
--   gone.
-- ============================================================================

BEGIN;

DO $$
DECLARE
    has_table BOOLEAN;
    has_rows  BOOLEAN;
    sentinel_present BOOLEAN;
    row_count INTEGER;
BEGIN
    SELECT EXISTS (
        SELECT 1 FROM information_schema.tables
         WHERE table_schema = 'public' AND table_name = 'access_points'
    ) INTO has_table;

    IF NOT has_table THEN
        RAISE NOTICE 'access_points already absent; nothing to drop.';
        RETURN;
    END IF;

    EXECUTE 'SELECT COUNT(*) FROM public.access_points' INTO row_count;
    has_rows := row_count > 0;

    IF has_rows THEN
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
             WHERE table_schema = 'public' AND table_name = 'migration_log'
        ) INTO sentinel_present;

        IF sentinel_present THEN
            SELECT EXISTS (
                SELECT 1 FROM public.migration_log
                 WHERE name = '20260502_split_access_points_to_v2'
            ) INTO sentinel_present;
        END IF;

        IF NOT sentinel_present THEN
            RAISE EXCEPTION
                'access_points has % rows but migration_log sentinel '
                '"20260502_split_access_points_to_v2" is missing. Run '
                'backend/scripts/migrate_access_points_to_v2.py --apply '
                'before applying this migration.',
                row_count;
        END IF;
    END IF;

    DROP TABLE IF EXISTS public.access_points CASCADE;
    RAISE NOTICE 'access_points dropped (had % rows pre-drop).', row_count;
END $$;

COMMIT;
