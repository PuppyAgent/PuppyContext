-- ============================================================================
-- Access surface credentials and policies
-- ============================================================================
-- access_surfaces stays the canonical center for all workspace entry points:
-- git_remote, cli, agent, mcp, sandbox.
--
-- Secrets and permission policy do not belong in access_surfaces.config.
-- This migration adds provider-neutral extension tables:
--   - access_surface_credentials: hashed bearer/git/ssh credentials
--   - access_surface_policies: server-side filesystem/tool/shell/network policy
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.access_surface_credentials (
    id                  text PRIMARY KEY DEFAULT gen_random_uuid()::text,
    org_id              text REFERENCES public.organizations(id) ON DELETE CASCADE,
    project_id          text NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,
    access_surface_id   text NOT NULL REFERENCES public.access_surfaces(id) ON DELETE CASCADE,

    credential_type     text NOT NULL
                            CHECK (credential_type IN ('bearer_token', 'git_http_token', 'ssh_public_key')),
    key_prefix          text NOT NULL,
    key_last4           text NOT NULL,
    key_hash            text NOT NULL,
    hash_alg            text NOT NULL DEFAULT 'hmac_sha256_v1',

    status              text NOT NULL DEFAULT 'active'
                            CHECK (status IN ('active', 'revoked')),
    expires_at          timestamptz,
    last_used_at        timestamptz,
    created_by          uuid REFERENCES auth.users(id) ON DELETE SET NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    revoked_at          timestamptz,

    CONSTRAINT access_surface_credentials_prefix_check CHECK (key_prefix <> ''),
    CONSTRAINT access_surface_credentials_last4_check CHECK (key_last4 <> ''),
    CONSTRAINT access_surface_credentials_hash_check CHECK (key_hash <> '')
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_access_surface_credentials_active_hash
    ON public.access_surface_credentials(key_hash)
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_access_surface_credentials_surface
    ON public.access_surface_credentials(access_surface_id, status);

CREATE INDEX IF NOT EXISTS idx_access_surface_credentials_project
    ON public.access_surface_credentials(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS public.access_surface_policies (
    access_surface_id   text PRIMARY KEY REFERENCES public.access_surfaces(id) ON DELETE CASCADE,
    version             integer NOT NULL DEFAULT 1,

    fs_policy           jsonb NOT NULL DEFAULT '{}'::jsonb,
    tools_policy        jsonb NOT NULL DEFAULT '{}'::jsonb,
    shell_policy        jsonb NOT NULL DEFAULT '{}'::jsonb,
    network_policy      jsonb NOT NULL DEFAULT '{}'::jsonb,

    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT access_surface_policies_version_check CHECK (version >= 1)
);

DROP TRIGGER IF EXISTS trg_access_surface_policies_updated_at ON public.access_surface_policies;
CREATE TRIGGER trg_access_surface_policies_updated_at
    BEFORE UPDATE ON public.access_surface_policies
    FOR EACH ROW
    EXECUTE FUNCTION public._context_entrypoint_bump_updated_at();

ALTER TABLE public.access_surfaces
    DROP CONSTRAINT IF EXISTS access_surfaces_config_no_runtime_secrets;

ALTER TABLE public.access_surfaces
    ADD CONSTRAINT access_surfaces_config_no_runtime_secrets
    CHECK (
        kind <> 'mcp'
        OR (
            NOT (config ? 'api_key')
            AND NOT (config ? 'mcp_api_key')
            AND NOT (config ? 'access_key')
            AND NOT (config ? 'tools_config')
            AND NOT (config ? 'accesses')
        )
    )
    NOT VALID;

ALTER TABLE public.access_surface_credentials ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.access_surface_policies ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'access_surface_credentials'
          AND policyname = 'access_surface_credentials_service_role_all'
    ) THEN
        CREATE POLICY "access_surface_credentials_service_role_all"
            ON public.access_surface_credentials
            FOR ALL
            TO service_role
            USING (true)
            WITH CHECK (true);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'access_surface_policies'
          AND policyname = 'access_surface_policies_service_role_all'
    ) THEN
        CREATE POLICY "access_surface_policies_service_role_all"
            ON public.access_surface_policies
            FOR ALL
            TO service_role
            USING (true)
            WITH CHECK (true);
    END IF;
END $$;

COMMIT;
