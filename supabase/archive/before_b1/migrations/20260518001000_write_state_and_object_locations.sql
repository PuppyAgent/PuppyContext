-- ============================================================================
-- Write-state read coalescing + packed object locations.
-- ============================================================================
-- Product/Web writes need one authoritative snapshot before building the Git
-- commit candidate:
--   - authorization role / write permission
--   - project metadata needed by repo facades
--   - current project root hash and root-scope head commit
--
-- Keeping this as one RPC avoids a per-save chain of REST reads across
-- projects, org_members, project_members, and mut_scope_state. The publish
-- RPC remains the CAS linearization point.
--
-- Packed object locations let the object store write multiple Git loose
-- objects into one immutable bundle while preserving lookup by object id.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.mut_object_locations (
    project_id   TEXT NOT NULL,
    object_id    TEXT NOT NULL,
    pack_key     TEXT NOT NULL,
    offset_bytes BIGINT NOT NULL,
    size_bytes   BIGINT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (project_id, object_id)
);

CREATE INDEX IF NOT EXISTS idx_mut_object_locations_pack
    ON public.mut_object_locations (project_id, pack_key);

CREATE OR REPLACE FUNCTION public.get_mut_project_write_state(
    p_project_id TEXT,
    p_user_id    TEXT
) RETURNS TABLE (
    project_id      TEXT,
    project_name    TEXT,
    org_id          TEXT,
    visibility      TEXT,
    role            TEXT,
    can_write       BOOLEAN,
    root_hash       TEXT,
    head_commit_id  TEXT
)
LANGUAGE plpgsql
AS $$
BEGIN
    RETURN QUERY
    WITH project_row AS (
        SELECT
            p.id::TEXT AS id,
            p.name::TEXT AS name,
            p.org_id::TEXT AS org_id,
            COALESCE(p.visibility, 'org')::TEXT AS visibility,
            COALESCE(p.mut_root_hash, '')::TEXT AS root_hash
        FROM public.projects p
        WHERE p.id::TEXT = p_project_id
        LIMIT 1
    ),
    membership AS (
        SELECT
            pr.*,
            om.role::TEXT AS org_role,
            pm.role::TEXT AS project_role
        FROM project_row pr
        LEFT JOIN public.org_members om
          ON om.org_id::TEXT = pr.org_id
         AND om.user_id::TEXT = p_user_id
        LEFT JOIN public.project_members pm
          ON pm.project_id::TEXT = pr.id
         AND pm.user_id::TEXT = p_user_id
    ),
    effective AS (
        SELECT
            m.*,
            CASE
                WHEN m.visibility = 'org' THEN COALESCE(m.org_role, '')
                WHEN m.org_role = 'owner' THEN 'owner'
                ELSE COALESCE(m.project_role, '')
            END::TEXT AS effective_role
        FROM membership m
    )
    SELECT
        e.id,
        e.name,
        e.org_id,
        e.visibility,
        e.effective_role,
        (e.effective_role = ANY (ARRAY['owner', 'admin', 'editor'])) AS can_write,
        e.root_hash,
        COALESCE(s.head_commit_id, '')::TEXT AS head_commit_id
    FROM effective e
    LEFT JOIN public.mut_scope_state s
      ON s.project_id = e.id
     AND s.scope_path = '';
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_mut_project_write_state(TEXT, TEXT)
    TO anon, authenticated, service_role;

GRANT SELECT, INSERT, UPDATE ON TABLE public.mut_object_locations
    TO service_role;

COMMIT;
