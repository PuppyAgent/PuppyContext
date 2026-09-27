DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM public.access_surfaces AS surface
        CROSS JOIN LATERAL jsonb_each(surface.config) AS field
        WHERE surface.kind IN ('agent', 'sandbox')
          AND field.key IN ('api_key', 'mcp_api_key', 'access_key')
          AND field.value IN ('null'::jsonb, '""'::jsonb)
    ) THEN
        RAISE EXCEPTION 'empty runtime credential placeholder cleanup is incomplete';
    END IF;
END;
$$;
