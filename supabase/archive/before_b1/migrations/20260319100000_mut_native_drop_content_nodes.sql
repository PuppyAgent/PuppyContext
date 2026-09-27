-- Mut-Native Architecture Migration
-- Drops content_nodes and connection_accesses tables.
-- Mut tree (S3) is now the sole source of truth for content.
-- Permissions are managed by Mut scope (connections.config.scope).

BEGIN;

-- 1. Drop indexes first
DROP INDEX IF EXISTS idx_content_nodes_project_parent;
DROP INDEX IF EXISTS idx_content_nodes_project_type;
DROP INDEX IF EXISTS idx_content_nodes_mut_path;
DROP INDEX IF EXISTS idx_cn_unique_name_mut;
DROP INDEX IF EXISTS idx_content_nodes_project_id;
DROP INDEX IF EXISTS idx_content_nodes_parent_id;

-- 2. Drop content_nodes table
DROP TABLE IF EXISTS content_nodes CASCADE;

-- 3. Drop connection_accesses table (replaced by Mut scope)
DROP TABLE IF EXISTS connection_accesses CASCADE;

-- 4. Drop helper functions that operated on content_nodes
DROP FUNCTION IF EXISTS parent_mut_path(text) CASCADE;
DROP FUNCTION IF EXISTS move_node_by_mut_path(text, text, text) CASCADE;

-- 5. Create lightweight bookmarks table for stable external references
CREATE TABLE IF NOT EXISTS bookmarks (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    path       TEXT NOT NULL,
    label      TEXT,
    type       TEXT NOT NULL DEFAULT 'pin',
    created_by TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (project_id, path, type)
);

COMMENT ON TABLE bookmarks IS 'Lightweight stable handles for shared/pinned paths. Not every file has one.';

-- 6. Create standalone tables table (previously stored inside content_nodes)
CREATE TABLE IF NOT EXISTS tables (
    id          TEXT PRIMARY KEY,
    name        TEXT,
    project_id  TEXT REFERENCES projects(id) ON DELETE CASCADE,
    created_by  TEXT,
    description TEXT,
    data        JSONB,
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tables_project_id ON tables(project_id);
CREATE INDEX IF NOT EXISTS idx_tables_created_by ON tables(created_by);

COMMENT ON TABLE tables IS 'Structured data tables (JSON Pointer). Previously stored as content_nodes rows.';

-- 7. Add Mut version tracking columns to projects table
ALTER TABLE projects ADD COLUMN IF NOT EXISTS mut_root_hash TEXT DEFAULT '';
ALTER TABLE projects ADD COLUMN IF NOT EXISTS mut_version INT DEFAULT 0;

-- 8. Create mut_commits table (Mut version history)
CREATE TABLE IF NOT EXISTS mut_commits (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    version         INT NOT NULL,
    root_hash       TEXT NOT NULL DEFAULT '',
    scope_path      TEXT NOT NULL DEFAULT '',
    who             TEXT NOT NULL,
    message         TEXT NOT NULL DEFAULT '',
    changes         JSONB NOT NULL DEFAULT '[]'::jsonb,
    conflicts       JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(project_id, version)
);

CREATE INDEX IF NOT EXISTS idx_mut_commits_project_version
    ON mut_commits (project_id, version DESC);
CREATE INDEX IF NOT EXISTS idx_mut_commits_created_at
    ON mut_commits (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_mut_commits_who
    ON mut_commits (who, created_at DESC);

ALTER TABLE mut_commits ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS service_role_all_mut_commits ON mut_commits;
CREATE POLICY service_role_all_mut_commits
    ON mut_commits FOR ALL TO service_role
    USING (true) WITH CHECK (true);

-- 9. Ensure audit_logs supports Mut events
ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS project_id TEXT;
ALTER TABLE audit_logs ALTER COLUMN node_id DROP NOT NULL;
CREATE INDEX IF NOT EXISTS idx_audit_logs_project_id
    ON audit_logs (project_id, created_at DESC)
    WHERE project_id IS NOT NULL;

-- 10. Migrate existing table data from content_nodes to tables (if content_nodes still exists at migration time)
-- This is a no-op if content_nodes was already dropped in step 2 above.

COMMIT;
