-- ISSUE-012 regression: the OUT parameter ``object_id`` in the original
-- sync RPC made its column-list conflict target ambiguous in PL/pgSQL.
--
-- The original 20260711080000 migration is already applied, so repair the
-- function in a forward-only migration.  Naming the primary-key constraint
-- avoids resolving ``object_id`` as the function's OUT variable while
-- preserving the RPC signature and its JSON response shape.

CREATE OR REPLACE FUNCTION public.sync_version_object_gc_candidates(
    p_project_id text,
    p_object_ids text[],
    p_now timestamptz,
    p_quarantine_seconds integer
) RETURNS TABLE(object_id text)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
    IF p_quarantine_seconds < 0 THEN
        RAISE EXCEPTION 'p_quarantine_seconds must be non-negative';
    END IF;

    DELETE FROM public.version_object_gc_candidates c
    WHERE c.project_id = p_project_id
      AND NOT (c.object_id = ANY(COALESCE(p_object_ids, ARRAY[]::text[])));

    INSERT INTO public.version_object_gc_candidates (
        project_id, object_id, first_seen_at, last_seen_at
    )
    SELECT p_project_id, candidate, p_now, p_now
    FROM unnest(COALESCE(p_object_ids, ARRAY[]::text[])) candidate
    ON CONFLICT ON CONSTRAINT version_object_gc_candidates_pkey DO UPDATE
    SET last_seen_at = EXCLUDED.last_seen_at;

    RETURN QUERY
    SELECT c.object_id
    FROM public.version_object_gc_candidates c
    WHERE c.project_id = p_project_id
      AND c.object_id = ANY(COALESCE(p_object_ids, ARRAY[]::text[]))
      AND c.first_seen_at <= p_now - make_interval(secs => p_quarantine_seconds);
END;
$$;

REVOKE ALL ON FUNCTION public.sync_version_object_gc_candidates(
    text, text[], timestamptz, integer
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.sync_version_object_gc_candidates(
    text, text[], timestamptz, integer
) TO service_role;
