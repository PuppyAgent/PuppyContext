-- ============================================================
-- Mut Scope Versioning Migration
--
-- Adds per-scope version tracking to support Mut v4's lock-free
-- architecture. Each scope now independently tracks its own
-- version number and Merkle tree hash.
--
-- Changes:
--   1. ALTER mut_commits — add scope_hash and scope_version columns
--   2. CREATE mut_scope_state — per-scope version + hash tracking
--
-- Design: docs/design_mut_puppyone_v4.md
-- ============================================================


BEGIN;

-- ============================================================
-- PART 1: ALTER mut_commits — add scope-level fields
-- ============================================================
--
-- scope_hash: the Merkle tree hash for this scope at this version
--   (replaces the global root_hash for merge-base resolution)
-- scope_version: scope-prefixed version ID like "docs/3", "src/5"
--   (independent per-scope counter, global version still in `version` column)

ALTER TABLE mut_commits
    ADD COLUMN IF NOT EXISTS scope_hash TEXT DEFAULT '';

ALTER TABLE mut_commits
    ADD COLUMN IF NOT EXISTS scope_version TEXT DEFAULT '';


-- ============================================================
-- PART 2: CREATE mut_scope_state — per-scope tracking
-- ============================================================
--
-- Each scope within a project independently tracks:
--   - latest scope-level version number (not global)
--   - current Merkle tree hash for the scope
--
-- This replaces the global mut_root_hash on the projects table
-- as the primary mechanism for merge-base resolution.
--
-- The projects.mut_root_hash and projects.mut_version columns
-- are kept for backwards compatibility but are no longer the
-- source of truth for per-scope operations.

CREATE TABLE IF NOT EXISTS mut_scope_state (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,

    -- Normalized scope path (e.g. "docs", "src/frontend", "" for root)
    scope_path      TEXT NOT NULL DEFAULT '',

    -- Scope-level version counter (independent per scope)
    version         INT NOT NULL DEFAULT 0,

    -- Current Merkle tree hash for this scope
    scope_hash      TEXT NOT NULL DEFAULT '',

    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- One row per (project, scope)
    UNIQUE(project_id, scope_path)
);

-- Primary lookup: by project + scope
CREATE INDEX IF NOT EXISTS idx_mut_scope_state_project_scope
    ON mut_scope_state (project_id, scope_path);

-- RLS
ALTER TABLE mut_scope_state ENABLE ROW LEVEL SECURITY;

CREATE POLICY service_role_all_mut_scope_state
    ON mut_scope_state FOR ALL TO service_role
    USING (true) WITH CHECK (true);


-- ============================================================
-- Done!
--
-- Summary:
--   [ALTER]  mut_commits      + scope_hash, scope_version
--   [CREATE] mut_scope_state  — per-scope version + hash tracking
--
-- The global projects.mut_version and projects.mut_root_hash
-- columns remain for backwards compatibility. The global version
-- counter (projects.mut_version) continues to be incremented for
-- cross-scope ordering.
-- ============================================================

COMMIT;
