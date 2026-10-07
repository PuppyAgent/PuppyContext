-- Derived product readiness uses the native HEAD and accepted Git ref ledger.
BEGIN;
CREATE FUNCTION public.get_native_project_readiness(p_project_id text)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
WITH RECURSIVE head_path AS (
  SELECT r.name,r.target_oid,r.symbolic_target,ARRAY[r.name] AS visited
  FROM public.version_repository_refs r JOIN public.version_repositories v USING(project_id)
  WHERE r.project_id=p_project_id AND r.name=convert_to('HEAD','UTF8') AND v.authority='native'
  UNION ALL
  SELECT r.name,r.target_oid,r.symbolic_target,p.visited||r.name
  FROM head_path p JOIN public.version_repository_refs r
    ON r.project_id=p_project_id AND r.name=p.symbolic_target
  WHERE cardinality(p.visited)<32 AND NOT r.name=ANY(p.visited)
), selected AS (SELECT * FROM head_path ORDER BY cardinality(visited) DESC LIMIT 1)
SELECT jsonb_build_object(
  'default_branch_b64', replace(encode(coalesce(
    (SELECT symbolic_target FROM selected WHERE symbolic_target IS NOT NULL),
    (SELECT name FROM selected WHERE name<>convert_to('HEAD','UTF8')),
    convert_to('refs/heads/main','UTF8')), 'base64'), E'\n',''),
  'project_git_surface_exists', EXISTS(SELECT 1 FROM public.access_surfaces s
    WHERE s.project_id=p_project_id AND s.scope_id IS NULL AND s.kind='git_remote' AND s.status='active'),
  'project_head_commit_id',coalesce((SELECT target_oid FROM selected),''),
  'project_git_push_accepted',EXISTS(
    SELECT 1 FROM public.version_ref_transactions t
    JOIN public.version_reflog_entries l ON l.transaction_id=t.id AND l.project_id=t.project_id
    WHERE t.project_id=p_project_id AND t.actor LIKE 'runtime:%'
      AND t.result->>'status'='committed' AND l.new_state->>'kind'='oid'
      AND l.name=coalesce((SELECT symbolic_target FROM selected),(SELECT name FROM selected))
      AND EXISTS(SELECT 1 FROM public.audit_logs a WHERE a.project_id=t.project_id
        AND a.action='repository_refs_changed' AND a.metadata->>'ref_transaction_id'=t.id::text
        AND a.metadata->>'message'='git push')
      AND NOT EXISTS(SELECT 1 FROM public.version_product_operations o
        WHERE o.project_id=t.project_id AND o.actor=t.actor AND o.request_key=t.request_key)
  )
)
$$;
REVOKE ALL ON FUNCTION public.get_native_project_readiness(text) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.get_native_project_readiness(text) TO service_role;
COMMIT;
