-- ISSUE-012: register every flushed write batch before its publishing CAS.
CREATE OR REPLACE FUNCTION public.register_version_object_gc_candidates(
    p_project_id text,
    p_object_ids text[],
    p_now timestamptz
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
    INSERT INTO public.version_object_gc_candidates (
        project_id, object_id, first_seen_at, last_seen_at
    )
    SELECT p_project_id, object_id, p_now, p_now
    FROM unnest(COALESCE(p_object_ids, ARRAY[]::text[])) AS object_id
    ON CONFLICT (project_id, object_id) DO UPDATE
    SET last_seen_at = EXCLUDED.last_seen_at;
END;
$$;

REVOKE ALL ON FUNCTION public.register_version_object_gc_candidates(
    text, text[], timestamptz
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.register_version_object_gc_candidates(
    text, text[], timestamptz
) TO service_role;
