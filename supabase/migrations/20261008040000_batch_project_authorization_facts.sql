-- Backend-only, set-based counterpart of authorization_project_facts.
-- Reads current facts; role interpretation remains in AuthorizationService.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '2min';

CREATE FUNCTION public.authorization_project_facts_batch(p_projects text[], p_user uuid)
RETURNS SETOF jsonb LANGUAGE plpgsql STABLE
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF p_projects IS NULL OR cardinality(p_projects) > 100 OR p_user IS NULL THEN
        RAISE EXCEPTION 'invalid_authorization_batch' USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
    SELECT jsonb_build_object(
        'project_id', p.id, 'org_id', p.org_id, 'visibility', p.visibility,
        'org_role', o.role, 'project_role', m.role, 'project_member_org_id', m.org_id
    )
    FROM public.projects p
    LEFT JOIN public.org_members o ON o.org_id = p.org_id AND o.user_id = p_user
    LEFT JOIN public.project_members m ON m.project_id = p.id AND m.user_id = p_user
    WHERE p.id = ANY(p_projects) AND p.lifecycle_status = 'ready'
    ORDER BY p.id;
END $$;

REVOKE ALL ON FUNCTION public.authorization_project_facts_batch(text[], uuid)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.authorization_project_facts_batch(text[], uuid) TO service_role;
NOTIFY pgrst, 'reload schema';
COMMIT;
