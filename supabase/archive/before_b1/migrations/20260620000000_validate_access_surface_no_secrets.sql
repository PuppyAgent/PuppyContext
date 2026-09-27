-- Promote the access_surfaces "no runtime secrets in config" CHECK from NOT VALID
-- to fully validated.
--
-- The constraint access_surfaces_config_no_runtime_secrets was added NOT VALID in
-- 20260616003000, so it only guards NEW/updated rows — pre-existing rows were never
-- scanned. The current code never writes api_key/mcp_api_key/access_key/tools_config/
-- accesses into access_surfaces.config (credentials live hashed in
-- access_surface_credentials; the connector repo strips these keys defensively), but
-- rows created before the CHECK could still carry them.
--
-- Step 1 strips any such keys from existing mcp rows (safe: these keys are never read
-- from config), making the table satisfy the CHECK; Step 2 validates it so the
-- guarantee covers all rows, not just future writes.

BEGIN;

UPDATE public.access_surfaces
SET config = config - 'api_key' - 'mcp_api_key' - 'access_key' - 'tools_config' - 'accesses'
WHERE kind = 'mcp'
  AND (
        config ? 'api_key'
     OR config ? 'mcp_api_key'
     OR config ? 'access_key'
     OR config ? 'tools_config'
     OR config ? 'accesses'
  );

ALTER TABLE public.access_surfaces
    VALIDATE CONSTRAINT access_surfaces_config_no_runtime_secrets;

COMMIT;
