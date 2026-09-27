-- CAS (Compare-And-Swap) RPC functions for MUT concurrency control
-- These functions provide atomic operations that cannot be achieved
-- with standard REST API calls to Supabase.

-- 1. Atomic CAS update on mut_scope_state.scope_hash
-- Returns true if the update succeeded (old_hash matched), false otherwise.
CREATE OR REPLACE FUNCTION cas_update_scope_state(
    p_project_id UUID,
    p_scope_path TEXT,
    p_old_hash TEXT,
    p_new_hash TEXT
) RETURNS BOOLEAN
LANGUAGE plpgsql
AS $$
DECLARE
    rows_affected INT;
BEGIN
    -- First, try to insert if the row doesn't exist (first push to this scope)
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
            -- Fall through to UPDATE
            NULL;
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


-- 2. Atomic CAS update on projects.mut_root_hash
-- Returns true if the update succeeded (old_hash matched), false otherwise.
CREATE OR REPLACE FUNCTION cas_update_root_hash(
    p_project_id UUID,
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


-- 3. Atomic version increment on projects.mut_version
-- Returns the new version number.
CREATE OR REPLACE FUNCTION atomic_next_version(
    p_project_id UUID
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
