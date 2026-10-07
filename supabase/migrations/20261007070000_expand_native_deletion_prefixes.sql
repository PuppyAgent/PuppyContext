-- Native-only consumer release: canonical deletion inventories.
-- Apply while old writers/deletion workers are drained; complete source archive
-- before native traffic resumes. Historical namespace bytes are handled only
-- by the portable migration artifacts, not by runtime deletion.
BEGIN;
CREATE OR REPLACE FUNCTION "public"."_project_deletion_object_prefixes"("p_project_id" "text", "p_storage_principals" "jsonb") RETURNS "jsonb"
    LANGUAGE "sql" IMMUTABLE
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
    WITH principals AS (
        SELECT DISTINCT value AS principal
        FROM jsonb_array_elements_text(
            CASE
                WHEN jsonb_typeof(p_storage_principals) = 'array'
                    THEN p_storage_principals
                ELSE '[]'::jsonb
            END
        )
    ), prefixes AS (
        SELECT fixed.ordinal, ''::text AS principal, fixed.prefix
        FROM (VALUES
            (1, 'version/' || p_project_id || '/'),
            (3, 'projects/' || p_project_id || '/'),
            (4, 'shadow-snapshots/' || p_project_id || '/')
        ) AS fixed(ordinal, prefix)
        UNION ALL
        SELECT 10 + namespace.ordinal, principal.principal,
               'users/' || principal.principal || '/' || namespace.name ||
               '/' || p_project_id || '/'
        FROM principals principal
        CROSS JOIN (VALUES
            (1, 'etl_artifacts'),
            (2, 'processed'),
            (3, 'raw')
        ) AS namespace(ordinal, name)
    )
    SELECT jsonb_agg(prefix ORDER BY ordinal, principal)
    FROM prefixes;
$$;

-- Earlier lifecycle functions seed a minimal array before its canonicalizing
-- trigger runs. Remove the retired prefix there too, preserving owner and ACL.
DO $retire$ DECLARE item record; definition text; BEGIN
    FOR item IN SELECT p.oid,pg_get_functiondef(p.oid) AS definition
      FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
      WHERE n.nspname='public' AND p.proname IN
        ('abandon_project_initialization','abort_deferred_project_publication','delete_project_control_plane')
      AND p.prosrc LIKE '%mut/%' LOOP
        definition:=regexp_replace(item.definition,
          $pattern$\s*'mut/'\s*\|\|\s*p_project_id\s*\|\|\s*'/',\s*$pattern$, E'\n', 'g');
        IF definition=item.definition OR definition LIKE '%mut/%' THEN
            RAISE EXCEPTION 'unexpected_legacy_deletion_prefix_definition';
        END IF;
        EXECUTE definition;
    END LOOP;
END $retire$;
COMMIT;
