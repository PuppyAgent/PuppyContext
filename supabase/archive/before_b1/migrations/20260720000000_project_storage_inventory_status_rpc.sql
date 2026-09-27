-- The storage inventory tables are internal control-plane state.  Expose one
-- service-role-only status RPC so the immutable S3 data migration does not
-- depend on PostgREST's relation cache for direct table reads.

CREATE OR REPLACE FUNCTION public.project_storage_inventory_status()
RETURNS jsonb
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
    SELECT jsonb_build_object(
        'inventory_complete', state.inventory_complete,
        'checkpoint', state.checkpoint,
        'pending_orphans', COALESCE(
            (
                SELECT jsonb_agg(
                    jsonb_build_object(
                        'project_id', orphan.project_id,
                        'principal', orphan.principal
                    )
                    ORDER BY orphan.project_id, orphan.principal
                )
                FROM public.project_storage_orphan_prefixes orphan
                WHERE orphan.status = 'pending'
            ),
            '[]'::jsonb
        )
    )
    FROM public.project_storage_inventory_state state
    WHERE state.singleton;
$$;

REVOKE ALL ON FUNCTION public.project_storage_inventory_status()
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.project_storage_inventory_status()
    TO service_role;
