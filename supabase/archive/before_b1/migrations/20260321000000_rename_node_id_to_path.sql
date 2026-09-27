-- Rename node_id → path across all active tables.
-- In MUT-native architecture, these columns hold MUT tree paths (e.g. "docs/readme.md"),
-- not content_nodes UUIDs. The name `path` reflects this accurately.
--
-- Also makes tables.project_id NOT NULL (orphan tables removed;
-- all tables must belong to a project and go through MUT).

BEGIN;

-- ============================================================
-- 1. Rename node_id columns to path
-- ============================================================

-- connections: the MUT path this connection is bound to
ALTER TABLE connections RENAME COLUMN node_id TO path;

-- tools: the MUT path the tool is bound to
ALTER TABLE tools RENAME COLUMN node_id TO path;

-- chunks: the MUT path of the file that was chunked
ALTER TABLE chunks RENAME COLUMN node_id TO path;

-- audit_logs: the MUT path of the audited file
ALTER TABLE audit_logs RENAME COLUMN node_id TO path;

-- uploads: target path and result path
ALTER TABLE uploads RENAME COLUMN node_id TO path;
ALTER TABLE uploads RENAME COLUMN result_node_id TO result_path;

-- ============================================================
-- 2. Update indexes that reference old column names
-- ============================================================

-- Recreate any indexes that used node_id (if they exist)
DROP INDEX IF EXISTS idx_chunks_node_pointer;
CREATE INDEX IF NOT EXISTS idx_chunks_path_pointer
    ON chunks (path, json_pointer);

DROP INDEX IF EXISTS idx_tools_node_id;
CREATE INDEX IF NOT EXISTS idx_tools_path
    ON tools (path);

-- ============================================================
-- 3. Orphan tables: make project_id NOT NULL
--    All tables must belong to a project (go through MUT).
-- ============================================================

-- Delete any orphan tables (project_id IS NULL)
DELETE FROM tables WHERE project_id IS NULL;

-- Make project_id NOT NULL
ALTER TABLE tables ALTER COLUMN project_id SET NOT NULL;

COMMIT;
