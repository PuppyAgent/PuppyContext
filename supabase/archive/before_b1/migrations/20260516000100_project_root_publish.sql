-- Compatibility marker for a migration version that briefly existed on qubits.
--
-- The original 20260516000000_project_root_publish.sql conflicted with
-- 20260516000000_drop_protocol_mode.sql. It was temporarily renamed to this
-- version, and staging may already have recorded 20260516000100 in
-- supabase_migrations.schema_migrations.
--
-- Keep this file as a no-op so remote migration history stays reconcilable.
-- The real current project-root publish RPC is defined by
-- 20260518000000_project_root_publish_v2.sql.

DO $$
BEGIN
    NULL;
END $$;
