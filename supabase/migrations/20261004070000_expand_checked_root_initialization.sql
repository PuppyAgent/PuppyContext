-- ISSUE-062: initialization is not repair. Never replace an acknowledged root
-- because an object probe failed, nor overwrite a publication after a stale read.
-- Expand only: no initialization, enrollment, activation or external I/O in DDL.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='2min';
CREATE FUNCTION public.initialize_legacy_version_project_root(p_project_id text,p_lease_id uuid,p_holder_id text)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE project public.projects%ROWTYPE; org text; repo public.version_repositories%ROWTYPE;
    empty_tree constant text := '4b825dc642cb6eb9a060e54bf8d69288fbee4904';
BEGIN
    SELECT org_id INTO org FROM public.projects WHERE id=p_project_id;
    IF org IS NULL THEN RAISE EXCEPTION 'repository_unavailable'; END IF;
    PERFORM 1 FROM public.organizations WHERE id=org FOR UPDATE;
    SELECT * INTO project FROM public.projects WHERE id=p_project_id FOR UPDATE;
    IF NOT FOUND OR project.org_id IS DISTINCT FROM org OR project.lifecycle_status NOT IN ('initializing','ready') THEN
        RAISE EXCEPTION 'repository_unavailable';
    END IF;
    SELECT * INTO repo FROM public.version_repositories WHERE project_id=p_project_id FOR SHARE;
    IF FOUND AND (repo.authority='native' OR repo.object_format<>'sha1') THEN
        RAISE EXCEPTION 'native_repository_initialization_required';
    END IF;
    IF NULLIF(project.version_root_hash,'') IS NOT NULL THEN
        IF public._version_oid_valid(project.version_root_hash,'sha1') IS DISTINCT FROM true THEN
            RAISE EXCEPTION 'legacy_repository_root_corrupt';
        END IF;
        RETURN project.version_root_hash; -- No storage probe or metadata rewrite.
    END IF;
    -- A missing root alongside accepted history/refs is corruption, not an
    -- unborn repository. Repair requires an explicit, evidence-bound operation.
    IF EXISTS(SELECT 1 FROM public.version_commits WHERE project_id=p_project_id)
       OR EXISTS(SELECT 1 FROM public.version_scope_state WHERE project_id=p_project_id)
       OR EXISTS(SELECT 1 FROM public.version_refs WHERE project_id=p_project_id) THEN
        RAISE EXCEPTION 'legacy_repository_root_corrupt';
    END IF;
    PERFORM public._version_assert_write_lease(p_project_id,p_lease_id,p_holder_id);
    UPDATE public.projects SET version_root_hash=empty_tree WHERE id=p_project_id;
    PERFORM public._version_assert_write_lease(p_project_id,p_lease_id,p_holder_id);
    RETURN empty_tree;
END $$;
REVOKE ALL ON FUNCTION public.initialize_legacy_version_project_root(text,uuid,text)
    FROM PUBLIC,anon,authenticated,service_role;
GRANT EXECUTE ON FUNCTION public.initialize_legacy_version_project_root(text,uuid,text) TO service_role;
NOTIFY pgrst,'reload schema';
COMMIT;
