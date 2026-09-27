-- ISSUE-003: prevent new agent/sandbox machine credentials from entering config.
-- Existing rows are moved into the credential table by the deployment's
-- pre-migration data-backfill phase; this constraint is validated later.

BEGIN;

ALTER TABLE public.access_surfaces
    DROP CONSTRAINT IF EXISTS access_surfaces_config_no_runtime_secrets;

ALTER TABLE public.access_surfaces
    ADD CONSTRAINT access_surfaces_config_no_runtime_secrets
    CHECK (
        kind NOT IN ('mcp', 'agent', 'sandbox')
        OR (
            NOT (config ? 'api_key')
            AND NOT (config ? 'mcp_api_key')
            AND NOT (config ? 'access_key')
        )
    )
    NOT VALID;

COMMENT ON CONSTRAINT access_surfaces_config_no_runtime_secrets
    ON public.access_surfaces IS
    'Bearer credentials for MCP, agent, and sandbox surfaces live hash-only in access_surface_credentials.';

COMMIT;

