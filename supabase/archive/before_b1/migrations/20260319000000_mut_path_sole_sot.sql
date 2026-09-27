-- =============================================================
-- Migration: mut_path as sole tree structure Source of Truth
-- 
-- Replaces id_path with mut_path for all tree operations.
-- UUID (id) remains as stable identifier for external references.
-- =============================================================

BEGIN;

-- 0. Ensure mut_path column exists (may have been added by earlier backend migration)
ALTER TABLE content_nodes ADD COLUMN IF NOT EXISTS mut_path TEXT;

-- 1. Helper function: extract parent path from mut_path
CREATE OR REPLACE FUNCTION parent_mut_path(p_mut_path TEXT)
RETURNS TEXT
LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
    IF p_mut_path IS NULL OR p_mut_path = '' THEN
        RETURN '__root__';
    END IF;
    IF position('/' in p_mut_path) = 0 THEN
        RETURN '__root__';
    END IF;
    RETURN regexp_replace(p_mut_path, '/[^/]+$', '');
END;
$$;

-- 2. Atomic move by mut_path prefix replacement
CREATE OR REPLACE FUNCTION move_node_by_mut_path(
    p_project_id TEXT,
    p_old_prefix TEXT,
    p_new_prefix TEXT
) RETURNS VOID
LANGUAGE plpgsql AS $$
BEGIN
    UPDATE content_nodes
    SET mut_path = p_new_prefix,
        updated_at = now()
    WHERE project_id = p_project_id AND mut_path = p_old_prefix;

    UPDATE content_nodes
    SET mut_path = p_new_prefix || substring(mut_path from length(p_old_prefix) + 1),
        updated_at = now()
    WHERE project_id = p_project_id
      AND mut_path LIKE p_old_prefix || '/%';
END;
$$;

-- 3. Backfill: ensure all nodes have mut_path (derived from id_path + name)
-- Safety net for nodes created before IndexSync populated mut_path.
DO $$
DECLARE
    r RECORD;
    v_parent_mut TEXT;
    v_parent_id TEXT;
    v_path_parts TEXT[];
BEGIN
    FOR r IN
        SELECT id, name, type, id_path, project_id
        FROM content_nodes
        WHERE mut_path IS NULL AND id_path IS NOT NULL
        ORDER BY array_length(string_to_array(trim(both '/' from id_path), '/'), 1) ASC NULLS FIRST
    LOOP
        v_path_parts := string_to_array(trim(both '/' from r.id_path), '/');

        IF array_length(v_path_parts, 1) >= 2 THEN
            v_parent_id := v_path_parts[array_length(v_path_parts, 1) - 1];

            SELECT cn.mut_path INTO v_parent_mut
            FROM content_nodes cn
            WHERE cn.id = v_parent_id
            LIMIT 1;

            IF v_parent_mut IS NOT NULL AND v_parent_mut != '' THEN
                UPDATE content_nodes SET mut_path = v_parent_mut || '/' || r.name WHERE id = r.id;
            ELSE
                UPDATE content_nodes SET mut_path = r.name WHERE id = r.id;
            END IF;
        ELSE
            UPDATE content_nodes SET mut_path = r.name WHERE id = r.id;
        END IF;
    END LOOP;

    -- Catch-all: any remaining NULL mut_path gets name as fallback
    UPDATE content_nodes SET mut_path = name WHERE mut_path IS NULL;
END;
$$;

-- 4. Set mut_path NOT NULL
ALTER TABLE content_nodes ALTER COLUMN mut_path SET NOT NULL;

-- 5. New indexes for mut_path-based tree queries
CREATE INDEX IF NOT EXISTS idx_cn_mut_path_lookup
    ON content_nodes (project_id, mut_path text_pattern_ops);

CREATE INDEX IF NOT EXISTS idx_cn_children_mut
    ON content_nodes (project_id, depth, mut_path text_pattern_ops);

-- 6. Deduplicate sibling nodes with identical names before adding unique constraint.
--    Keeps the most recently updated node; renames older duplicates with a numeric suffix.
DO $$
DECLARE
    dup RECORD;
    sib RECORD;
    v_seq INT;
    v_new_name TEXT;
    v_parent TEXT;
BEGIN
    FOR dup IN
        SELECT project_id, parent_mut_path(mut_path) AS parent_mp, name, count(*) AS cnt
        FROM content_nodes
        GROUP BY project_id, parent_mut_path(mut_path), name
        HAVING count(*) > 1
    LOOP
        v_seq := 1;
        FOR sib IN
            SELECT id, name, mut_path
            FROM content_nodes
            WHERE project_id = dup.project_id
              AND parent_mut_path(mut_path) = dup.parent_mp
              AND name = dup.name
            ORDER BY updated_at DESC
            OFFSET 1  -- skip the newest one (keep it as-is)
        LOOP
            v_new_name := dup.name || '_dup' || v_seq;
            v_parent := CASE WHEN dup.parent_mp = '__root__' THEN '' ELSE dup.parent_mp || '/' END;

            UPDATE content_nodes
            SET name = v_new_name,
                mut_path = v_parent || v_new_name
            WHERE id = sib.id;

            v_seq := v_seq + 1;
        END LOOP;
    END LOOP;
END;
$$;

DROP INDEX IF EXISTS idx_content_nodes_unique_name_v2;

CREATE UNIQUE INDEX IF NOT EXISTS idx_cn_unique_name_mut
    ON content_nodes (project_id, parent_mut_path(mut_path), name);

-- 7a. Drop old id_path-based indexes
DROP INDEX IF EXISTS idx_content_nodes_id_path;

-- 7b.
DROP INDEX IF EXISTS idx_content_nodes_children_lookup;

-- 7c.
DROP INDEX IF EXISTS idx_content_nodes_mut_path;

-- 8-12. Drop legacy functions, triggers, indexes; redefine depth column
DO $cleanup$
BEGIN
    EXECUTE 'DROP FUNCTION IF EXISTS parent_path(TEXT)';
    EXECUTE 'DROP FUNCTION IF EXISTS move_node_atomic(TEXT, TEXT, TEXT)';
    EXECUTE 'DROP FUNCTION IF EXISTS move_node_atomic(TEXT, TEXT, TEXT, TEXT)';
    EXECUTE 'DROP TRIGGER IF EXISTS trg_check_no_cycle ON content_nodes';
    EXECUTE 'DROP FUNCTION IF EXISTS check_no_cycle()';
    EXECUTE 'ALTER TABLE content_nodes DROP COLUMN IF EXISTS depth';
    EXECUTE 'ALTER TABLE content_nodes ADD COLUMN depth INT GENERATED ALWAYS AS (
        CASE
            WHEN mut_path = '''' THEN 0
            ELSE array_length(string_to_array(mut_path, ''/''), 1)
        END
    ) STORED';
    EXECUTE 'CREATE INDEX IF NOT EXISTS idx_cn_children_mut_v2
        ON content_nodes (project_id, depth, mut_path text_pattern_ops)';
    EXECUTE 'DROP INDEX IF EXISTS idx_cn_children_mut';
    EXECUTE 'ALTER TABLE content_nodes DROP COLUMN IF EXISTS id_path';
END;
$cleanup$;

-- 13. count_children_batch using mut_path (LEFT JOIN to include empty folders)
CREATE OR REPLACE FUNCTION count_children_batch(p_parent_ids TEXT[])
RETURNS TABLE(parent_id TEXT, child_count BIGINT)
LANGUAGE plpgsql STABLE AS $$
BEGIN
    RETURN QUERY
    SELECT p.id AS parent_id, COALESCE(count(c.id), 0)::BIGINT AS child_count
    FROM content_nodes p
    LEFT JOIN content_nodes c
      ON c.project_id = p.project_id
      AND c.mut_path LIKE p.mut_path || '/%'
      AND c.depth = p.depth + 1
    WHERE p.id = ANY(p_parent_ids)
    GROUP BY p.id;
END;
$$;

COMMIT;
