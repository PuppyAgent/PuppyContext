-- ISSUE-016 / ISSUE-003: remove plaintext and parallel scope credential storage.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM public.repo_scopes
        WHERE access_key IS NOT NULL AND access_key_hash IS NULL
    ) THEN
        RAISE EXCEPTION
            'scope credential migration blocked: legacy credential backfill is incomplete';
    END IF;
END;
$$;

-- Every scope owns a canonical CLI surface. Existing installations should
-- already have these rows; create any missing row before credential backfill.
INSERT INTO public.access_surfaces (
    id, org_id, project_id, scope_id, kind, name, status,
    principal_type, principal_id, config
)
SELECT
    gen_random_uuid()::text,
    p.org_id,
    s.project_id,
    s.id,
    'cli',
    'FS CLI',
    'active',
    'scope',
    s.id,
    jsonb_build_object('path', s.path, 'mode', s.mode, 'direction', 'bidirectional')
FROM public.repo_scopes s
JOIN public.projects p ON p.id = s.project_id
WHERE NOT EXISTS (
    SELECT 1 FROM public.access_surfaces a
    WHERE a.scope_id = s.id AND a.kind = 'cli'
);

-- Copy the already-HMACed key into the canonical credential table. The raw
-- value is used only for display metadata before being erased below.
INSERT INTO public.access_surface_credentials (
    org_id, project_id, access_surface_id, credential_type,
    key_prefix, key_last4, key_hash, hash_alg, status
)
SELECT
    a.org_id,
    a.project_id,
    a.id,
    'bearer_token',
    COALESCE(NULLIF(split_part(s.access_key, '_', 1), ''), 'cli'),
    COALESCE(NULLIF(right(s.access_key, 4), ''), 'migr'),
    s.access_key_hash,
    'hmac_sha256_v1',
    CASE WHEN s.access_key_revoked_at IS NULL THEN 'active' ELSE 'revoked' END
FROM public.repo_scopes s
JOIN public.access_surfaces a
  ON a.scope_id = s.id AND a.kind = 'cli'
WHERE s.access_key_hash IS NOT NULL
ON CONFLICT (key_hash) WHERE status = 'active' DO NOTHING;

UPDATE public.access_surfaces
SET config = config - 'access_key' - 'mcp_api_key' - 'api_key'
WHERE kind IN ('git_remote', 'cli', 'agent', 'mcp', 'sandbox');

ALTER TABLE public.repo_scopes
    DROP COLUMN IF EXISTS access_key,
    DROP COLUMN IF EXISTS access_key_hash,
    DROP COLUMN IF EXISTS access_key_revoked_at;

ALTER TABLE public.access_surfaces
    DROP CONSTRAINT IF EXISTS access_surfaces_config_no_runtime_secrets;
ALTER TABLE public.access_surfaces
    ADD CONSTRAINT access_surfaces_config_no_runtime_secrets
    CHECK (
        NOT (config ? 'api_key')
        AND NOT (config ? 'mcp_api_key')
        AND NOT (config ? 'access_key')
    );
