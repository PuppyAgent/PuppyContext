-- ============================================================
-- Migration: Fix CAS RPC functions — p_project_id type mismatch
--
-- Problem: The three CAS RPC functions from
-- 20260415000000_mut_cas_rpc_functions.sql declared
-- ``p_project_id UUID``. But the mut_commits / mut_scope_state /
-- projects tables store project_id (and projects.id) as TEXT.
-- Inside the functions, ``WHERE project_id = p_project_id`` became
-- ``text = uuid`` — an operator PostgreSQL does not provide —
-- producing error 42883 on every call:
--
--     operator does not exist: text = uuid
--
-- Consequence: every push / rollback fails with HTTP 500 once it
-- reaches the CAS stage. Writes are effectively blocked.
--
-- Discovered during end-to-end test on 2026-04-17 via `mut push`.
-- Fix: redeclare ``p_project_id`` as TEXT, matching the column type.
-- ============================================================

-- Drop the UUID overloads first. PostgreSQL treats (uuid) and (text)
-- as distinct signatures, so CREATE OR REPLACE alone would leave the
-- old overloads behind and PostgREST could resolve to the wrong one.
DO $do$ BEGIN
  DROP FUNCTION IF EXISTS cas_update_scope_state(uuid, text, text, text);
  DROP FUNCTION IF EXISTS cas_update_root_hash(uuid, text, text);
  DROP FUNCTION IF EXISTS atomic_next_version(uuid);
END $do$;

CREATE OR REPLACE FUNCTION cas_update_scope_state(
    p_project_id TEXT,
    p_scope_path TEXT,
    p_old_hash TEXT,
    p_new_hash TEXT
) RETURNS BOOLEAN
LANGUAGE plpgsql
AS $$
DECLARE
    rows_affected INT;
BEGIN
    -- First-push fast path: try to INSERT if no row exists yet.
    IF p_old_hash = '' OR p_old_hash IS NULL THEN
        BEGIN
            INSERT INTO mut_scope_state (project_id, scope_path, scope_hash, version)
            VALUES (p_project_id, p_scope_path, p_new_hash, 0)
            ON CONFLICT (project_id, scope_path) DO NOTHING;

            GET DIAGNOSTICS rows_affected = ROW_COUNT;
            IF rows_affected > 0 THEN
                RETURN TRUE;
            END IF;
        EXCEPTION WHEN OTHERS THEN
            NULL;  -- fall through to UPDATE branch
        END;
    END IF;

    -- CAS update: only succeeds if current scope_hash matches p_old_hash
    UPDATE mut_scope_state
    SET scope_hash = p_new_hash
    WHERE project_id = p_project_id
      AND scope_path = p_scope_path
      AND (scope_hash = p_old_hash OR (scope_hash IS NULL AND p_old_hash = ''));

    GET DIAGNOSTICS rows_affected = ROW_COUNT;
    RETURN rows_affected > 0;
END;
$$;

CREATE OR REPLACE FUNCTION cas_update_root_hash(
    p_project_id TEXT,
    p_old_hash TEXT,
    p_new_hash TEXT
) RETURNS BOOLEAN
LANGUAGE plpgsql
AS $$
DECLARE
    rows_affected INT;
BEGIN
    UPDATE projects
    SET mut_root_hash = p_new_hash
    WHERE id = p_project_id
      AND (mut_root_hash = p_old_hash
           OR (mut_root_hash IS NULL AND (p_old_hash = '' OR p_old_hash IS NULL)));

    GET DIAGNOSTICS rows_affected = ROW_COUNT;
    RETURN rows_affected > 0;
END;
$$;

CREATE OR REPLACE FUNCTION atomic_next_version(
    p_project_id TEXT
) RETURNS INT
LANGUAGE plpgsql
AS $$
DECLARE
    new_version INT;
BEGIN
    UPDATE projects
    SET mut_version = COALESCE(mut_version, 0) + 1
    WHERE id = p_project_id
    RETURNING mut_version INTO new_version;

    IF new_version IS NULL THEN
        RAISE EXCEPTION 'project % not found', p_project_id;
    END IF;

    RETURN new_version;
END;
$$;
