-- ============================================================
-- Migration: Switch MUT commit identity from integer version → hash-based commit_id
--
-- Design: docs/design/mut-commit-id-migration.md
--
-- WHY
--   Integer versions assume a single linear monotonic counter per
--   project. That works for today's centralized fast-forward-only
--   push, but it locks out future features (client-side short
--   branches, idempotent retries by content, out-of-order replay).
--   A content-/metadata-derived commit_id (Git-style hash) gives
--   each commit a stable, globally unique identity while keeping
--   the current linear-history product semantics unchanged.
--
-- WHAT CHANGES
--   mut_commits:
--     - DROP  version INT              (linear counter)
--     - DROP  scope_version TEXT       (legacy "docs/3" compound id)
--     - ADD   commit_id TEXT NOT NULL  (16-hex SHA256 of metadata)
--     - UNIQUE(project_id, commit_id)
--     - Reordering key is now (created_at ASC, commit_id ASC)
--
--   mut_scope_state:
--     - DROP  version INT
--     - ADD   head_commit_id TEXT NOT NULL DEFAULT ''
--     - head_commit_id is the *only* per-scope head pointer; we do
--       NOT also duplicate it onto projects (avoid global contention)
--
--   projects:
--     - DROP  mut_version INT          (no global counter anymore)
--     - KEEP  mut_root_hash TEXT       (still used by root-hash CAS)
--
--   RPCs:
--     - DROP atomic_next_version(text) (no linear counter anymore)
--     - KEEP cas_update_scope_state    (already hash-based)
--     - KEEP cas_update_root_hash      (already hash-based)
--
-- DATA HANDLING
--   Per explicit product decision, we do NOT attempt to backfill
--   commit_id for existing rows. The history tables are truncated
--   so the schema is clean and reflects the new identity model.
--   User base is small; the forward-only cost is acceptable.
--
-- COMPAT
--   Backend code that still reads `version` / `scope_version` /
--   `mut_version` / `atomic_next_version` will fail loudly after
--   this migration. That is intentional — we want one-step
--   cut-over, no dual-write shim.
-- ============================================================

BEGIN;

-- ------------------------------------------------------------
-- 1. Wipe legacy history (acceptable per product decision)
-- ------------------------------------------------------------
TRUNCATE TABLE mut_commits;
TRUNCATE TABLE mut_scope_state;

-- ------------------------------------------------------------
-- 2. mut_commits — drop integer columns, add commit_id
-- ------------------------------------------------------------

DROP INDEX IF EXISTS idx_mut_commits_project_version;

ALTER TABLE mut_commits
    DROP COLUMN IF EXISTS version,
    DROP COLUMN IF EXISTS scope_version;

ALTER TABLE mut_commits
    ADD COLUMN IF NOT EXISTS commit_id TEXT NOT NULL;

ALTER TABLE mut_commits
    DROP CONSTRAINT IF EXISTS mut_commits_project_id_version_key;

-- Idempotent ADD CONSTRAINT: PostgreSQL has no ``IF NOT EXISTS`` form
-- for constraints, so we drop-then-add. Required so this migration
-- can be re-applied cleanly against an environment where it already
-- ran (CI test resets, partial re-runs, etc.).
ALTER TABLE mut_commits
    DROP CONSTRAINT IF EXISTS mut_commits_project_commit_unique;

ALTER TABLE mut_commits
    ADD CONSTRAINT mut_commits_project_commit_unique
        UNIQUE (project_id, commit_id);

-- Linear ordering key: server derives history order by
-- (created_at, commit_id) ASC. commit_id is a lex-deterministic
-- tie-breaker so two commits in the same microsecond still have
-- a stable order.
CREATE INDEX IF NOT EXISTS idx_mut_commits_project_linear
    ON mut_commits (project_id, created_at DESC, commit_id DESC);

-- Per-scope linear history (used by clone/pull bootstrapping)
CREATE INDEX IF NOT EXISTS idx_mut_commits_project_scope_linear
    ON mut_commits (project_id, scope_path, created_at DESC, commit_id DESC);


-- ------------------------------------------------------------
-- 3. mut_scope_state — drop version, add head_commit_id
-- ------------------------------------------------------------

ALTER TABLE mut_scope_state
    DROP COLUMN IF EXISTS version;

ALTER TABLE mut_scope_state
    ADD COLUMN IF NOT EXISTS head_commit_id TEXT NOT NULL DEFAULT '';


-- ------------------------------------------------------------
-- 4. projects — drop mut_version (keep mut_root_hash)
-- ------------------------------------------------------------

ALTER TABLE projects
    DROP COLUMN IF EXISTS mut_version;


-- ------------------------------------------------------------
-- 4b. audit_logs — drop integer old_version / new_version
--     columns. The commit identity (old/new commit_id) is already
--     captured in the ``metadata`` JSONB under keys like
--     ``commit_id`` / ``target_commit_id`` / ``new_commit_id``, so
--     we don't need dedicated columns. No code writes these
--     integer fields today; frontends never consumed them either.
-- ------------------------------------------------------------

ALTER TABLE audit_logs
    DROP COLUMN IF EXISTS old_version,
    DROP COLUMN IF EXISTS new_version;


-- ------------------------------------------------------------
-- 4c. sync_state / access_points — rename last_sync_version (INT)
--     to last_sync_commit_id (TEXT). This marks the MUT commit_id
--     at which the data source was last synced; it has to flip to
--     TEXT so it lines up with the new hash identity.
--
--     The sync_state table is the canonical satellite, but the
--     denormalized column on access_points (kept for legacy reads
--     by SyncRepository) also needs the same swap — otherwise the
--     backend would read an integer and mix types.
-- ------------------------------------------------------------

ALTER TABLE sync_state
    DROP COLUMN IF EXISTS last_sync_version;

ALTER TABLE sync_state
    ADD COLUMN IF NOT EXISTS last_sync_commit_id TEXT NOT NULL DEFAULT '';

ALTER TABLE access_points
    DROP COLUMN IF EXISTS last_sync_version;

ALTER TABLE access_points
    ADD COLUMN IF NOT EXISTS last_sync_commit_id TEXT NOT NULL DEFAULT '';


-- ------------------------------------------------------------
-- 5. RPCs — drop the integer counter, rewrite scope-state CAS so
--    it no longer references the dropped ``version`` column and
--    atomically updates both ``scope_hash`` and ``head_commit_id``.
-- ------------------------------------------------------------
DO $do$ BEGIN
  DROP FUNCTION IF EXISTS atomic_next_version(text);
  DROP FUNCTION IF EXISTS atomic_next_version(uuid);
  -- Drop the old 4-arg signature before redefining with 5 args —
  -- PostgreSQL treats different arities as distinct overloads, so
  -- CREATE OR REPLACE alone would leave the old one behind.
  DROP FUNCTION IF EXISTS cas_update_scope_state(text, text, text, text);
  DROP FUNCTION IF EXISTS cas_update_scope_state(uuid, text, text, text);
END $do$;

CREATE OR REPLACE FUNCTION cas_update_scope_state(
    p_project_id      TEXT,
    p_scope_path      TEXT,
    p_old_hash        TEXT,
    p_new_hash        TEXT,
    p_head_commit_id  TEXT DEFAULT ''
) RETURNS BOOLEAN
LANGUAGE plpgsql
AS $$
DECLARE
    rows_affected INT;
BEGIN
    -- First-push fast path: insert a new scope-state row if none exists.
    IF p_old_hash = '' OR p_old_hash IS NULL THEN
        BEGIN
            INSERT INTO mut_scope_state
                (project_id, scope_path, scope_hash, head_commit_id)
            VALUES
                (p_project_id, p_scope_path, p_new_hash, p_head_commit_id)
            ON CONFLICT (project_id, scope_path) DO NOTHING;

            GET DIAGNOSTICS rows_affected = ROW_COUNT;
            IF rows_affected > 0 THEN
                RETURN TRUE;
            END IF;
        EXCEPTION WHEN OTHERS THEN
            NULL;  -- fall through to UPDATE branch
        END;
    END IF;

    -- CAS update: succeed only if current scope_hash matches p_old_hash.
    -- head_commit_id is updated in the same statement for atomicity.
    --
    -- Defensive: when the caller forgets to pass a head_commit_id
    -- (empty string / NULL) we keep whatever is already there instead
    -- of blanking it out. Push/rollback always derive a fresh
    -- head_commit_id before CAS, so in practice this branch is only
    -- a safety net for legacy callers and for the set_scope_hash
    -- fast path that only wants to bump the content fingerprint.
    UPDATE mut_scope_state
    SET scope_hash     = p_new_hash,
        head_commit_id = CASE
            WHEN p_head_commit_id IS NULL OR p_head_commit_id = ''
                THEN head_commit_id
            ELSE p_head_commit_id
        END,
        updated_at     = NOW()
    WHERE project_id = p_project_id
      AND scope_path = p_scope_path
      AND (scope_hash = p_old_hash OR (scope_hash IS NULL AND p_old_hash = ''));

    GET DIAGNOSTICS rows_affected = ROW_COUNT;
    RETURN rows_affected > 0;
END;
$$;
