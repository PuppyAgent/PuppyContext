-- ============================================================================
-- Fix audit_logs.path column type: uuid -> text.
--
-- Migration 20260321000000_rename_node_id_to_path RENAMED audit_logs.node_id
-- to "path" to match the path-based architecture, but left the column TYPE as
-- uuid. The application (audit_repository.list_by_path / create) treats path as
-- a human file path string (e.g. "docs/readme.md"), so querying or inserting a
-- path raised:
--   invalid input syntax for type uuid: "a.txt" (SQLSTATE 22P02)
-- which surfaced as a 500 on GET /api/v1/nodes/{path}/audit-logs.
--
-- uuid -> text is a safe widening: existing uuid values become their canonical
-- string form, and the column then accepts real file paths. No FK references
-- this column (the node_id FK was not preserved through the rename).
-- ============================================================================

BEGIN;

ALTER TABLE public.audit_logs
    ALTER COLUMN path TYPE text USING path::text;

COMMIT;
