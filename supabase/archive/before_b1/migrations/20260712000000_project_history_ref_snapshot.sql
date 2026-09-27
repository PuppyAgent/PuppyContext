-- ISSUE-030 corrective pass: canonical, atomic ref snapshots for History.
--
-- The previous read path queried a compatibility "global head" and then
-- version_refs separately.  A scoped write or concurrent ref update could
-- therefore label the wrong commit as main or create a ref set that never
-- existed.  This function resolves the project-view head against the current
-- canonical root and returns root-scope named refs in one MVCC snapshot.

BEGIN;

CREATE OR REPLACE FUNCTION public.get_version_project_history_refs(
    p_project_id TEXT
)
RETURNS TABLE(ref_name TEXT, ref_type TEXT, commit_id TEXT)
LANGUAGE SQL
STABLE
SECURITY DEFINER
SET search_path = ''
AS $$
    WITH project_root AS (
        SELECT COALESCE(p.version_root_hash, '') AS root_hash
          FROM public.projects AS p
         WHERE p.id = p_project_id
         LIMIT 1
    ),
    resolved_head AS (
        SELECT COALESCE(
            (
                SELECT s.head_commit_id
                  FROM public.version_scope_state AS s
                  CROSS JOIN project_root AS p
                 WHERE s.project_id = p_project_id
                   AND s.scope_path = ''
                   AND s.scope_hash = p.root_hash
                   AND s.head_commit_id ~ '^[0-9a-f]{40}$'
                 LIMIT 1
            ),
            (
                SELECT v.project_view_commit_id
                  FROM public.version_view_commits AS v
                  CROSS JOIN project_root AS p
                 WHERE v.project_id = p_project_id
                   AND v.project_root_hash = p.root_hash
                   AND v.project_view_commit_id ~ '^[0-9a-f]{40}$'
                 ORDER BY v.created_at DESC, v.id DESC
                 LIMIT 1
            ),
            (
                SELECT s.head_commit_id
                  FROM public.version_scope_state AS s
                 WHERE s.project_id = p_project_id
                   AND s.scope_path = ''
                   AND s.head_commit_id ~ '^[0-9a-f]{40}$'
                 LIMIT 1
            ),
            (
                SELECT c.commit_id
                  FROM public.version_commits AS c
                 WHERE c.project_id = p_project_id
                   AND c.commit_id ~ '^[0-9a-f]{40}$'
                 ORDER BY c.created_at DESC, c.commit_id DESC
                 LIMIT 1
            ),
            ''
        ) AS commit_id
    ),
    snapshot_refs AS (
        SELECT 'refs/heads/main'::TEXT AS ref_name,
               'branch'::TEXT AS ref_type,
               h.commit_id
          FROM resolved_head AS h
         WHERE h.commit_id ~ '^[0-9a-f]{40}$'
        UNION ALL
        SELECT r.ref_name::TEXT,
               r.ref_type::TEXT,
               r.commit_id::TEXT
          FROM public.version_refs AS r
         WHERE r.project_id = p_project_id
           AND r.scope_path = ''
           AND r.ref_name <> 'refs/heads/main'
           AND r.ref_type IN ('branch', 'tag')
           AND r.commit_id ~ '^[0-9a-f]{40}$'
    )
    SELECT s.ref_name, s.ref_type, s.commit_id
      FROM snapshot_refs AS s
     ORDER BY
        CASE WHEN s.ref_name = 'refs/heads/main' THEN 0 ELSE 1 END,
        CASE WHEN s.ref_type = 'branch' THEN 0 ELSE 1 END,
        s.ref_name;
$$;

REVOKE ALL ON FUNCTION public.get_version_project_history_refs(TEXT)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.get_version_project_history_refs(TEXT)
    TO service_role;

NOTIFY pgrst, 'reload schema';

COMMIT;
