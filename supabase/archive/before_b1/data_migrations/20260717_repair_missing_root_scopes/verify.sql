DO $$
DECLARE
    zero_roots bigint;
    surplus_roots bigint;
BEGIN
    SELECT count(*) INTO zero_roots
    FROM public.projects p
    WHERE NOT EXISTS (
        SELECT 1 FROM public.repo_scopes s
        WHERE s.project_id = p.id AND s.is_root = true
    );

    SELECT count(*) INTO surplus_roots
    FROM (
        SELECT s.project_id
        FROM public.repo_scopes s
        WHERE s.is_root = true
        GROUP BY s.project_id
        HAVING count(*) > 1
    ) surplus;

    IF zero_roots > 0 OR surplus_roots > 0 THEN
        RAISE EXCEPTION
          'root Scope repair verification failed: % Projects without a root Scope, % Projects with surplus root Scopes',
          zero_roots, surplus_roots;
    END IF;
END;
$$;
