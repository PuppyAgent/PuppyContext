DO $$
DECLARE
    pending_count bigint;
BEGIN
    SELECT count(*) INTO pending_count
    FROM public.access_surfaces
    WHERE kind IN ('agent', 'sandbox')
      AND (
          config ? 'api_key'
          OR config ? 'mcp_api_key'
          OR config ? 'access_key'
      );
    IF pending_count > 0 THEN
        RAISE EXCEPTION
          'surface credential backfill incomplete: % config secret(s) remain',
          pending_count;
    END IF;
END;
$$;
