-- Canonical Git remote contract (expand phase).
--
-- Git locators identify a Project/scope; credentials grant authority and are
-- deliberately stored separately.  Runtime Git authorization narrows, never
-- widens, scope/binding/human policy before Version Engine admission.

BEGIN;

ALTER TABLE public.access_surface_credentials
    ADD COLUMN IF NOT EXISTS grant_mode text;

ALTER TABLE public.access_surface_credentials
    ADD COLUMN IF NOT EXISTS credential_lifecycle text;

UPDATE public.access_surface_credentials c
SET grant_mode = COALESCE(
    (
        SELECT b.mode
        FROM public.project_workspace_bindings b
        WHERE b.id = c.workspace_binding_id
    ),
    (
        SELECT rs.mode
        FROM public.access_surfaces s
        JOIN public.repo_scopes rs
          ON rs.id = s.scope_id AND rs.project_id = s.project_id
        WHERE s.id = c.access_surface_id
    ),
    'r'
)
WHERE c.grant_mode IS NULL;

UPDATE public.access_surface_credentials
SET grant_mode = 'r'
WHERE grant_mode IS NULL;

-- Rotation domains are explicit data, not an inference made by a revocation
-- query.  The inference here is only the one-time expand-phase backfill for
-- credentials created before this column existed.
UPDATE public.access_surface_credentials
SET credential_lifecycle = CASE
    WHEN workspace_binding_id IS NOT NULL THEN 'binding'
    WHEN expires_at IS NOT NULL THEN 'session'
    ELSE 'shared'
END
WHERE credential_lifecycle IS NULL;

ALTER TABLE public.access_surface_credentials
    ALTER COLUMN grant_mode SET NOT NULL,
    DROP CONSTRAINT IF EXISTS access_surface_credentials_grant_mode_check;
ALTER TABLE public.access_surface_credentials
    ADD CONSTRAINT access_surface_credentials_grant_mode_check
      CHECK (grant_mode IN ('r', 'rw'));

ALTER TABLE public.access_surface_credentials
    ALTER COLUMN credential_lifecycle SET NOT NULL,
    DROP CONSTRAINT IF EXISTS access_surface_credentials_lifecycle_check,
    DROP CONSTRAINT IF EXISTS access_surface_credentials_lifecycle_shape_check;
ALTER TABLE public.access_surface_credentials
    ADD CONSTRAINT access_surface_credentials_lifecycle_check
      CHECK (credential_lifecycle IN ('shared', 'session', 'binding')),
    ADD CONSTRAINT access_surface_credentials_lifecycle_shape_check
      CHECK (
          (credential_lifecycle = 'binding' AND workspace_binding_id IS NOT NULL)
          OR (
              credential_lifecycle IN ('shared', 'session')
              AND workspace_binding_id IS NULL
              AND (credential_lifecycle <> 'session' OR expires_at IS NOT NULL)
          )
      );

COMMENT ON COLUMN public.access_surface_credentials.grant_mode IS
  'Credential capability ceiling. Effective runtime mode is the minimum of credential, scope, surface policy, binding, and current human grant.';

COMMENT ON COLUMN public.access_surface_credentials.credential_lifecycle IS
  'Independent revocation domain: shared manual slot, short-lived session, or per-workspace binding.';

CREATE OR REPLACE FUNCTION public._validate_access_surface_credential()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    selected_kind text;
    selected_scope_id text;
    selected_scope_mode text;
    selected_surface_mode text;
    selected_surface_status text;
    selected_binding public.project_workspace_bindings%ROWTYPE;
BEGIN
    SELECT
        s.kind,
        s.scope_id,
        rs.mode,
        COALESCE(s.config ->> 'mode', 'rw'),
        s.status
      INTO
        selected_kind,
        selected_scope_id,
        selected_scope_mode,
        selected_surface_mode,
        selected_surface_status
    FROM public.access_surfaces s
    JOIN public.repo_scopes rs
      ON rs.id = s.scope_id AND rs.project_id = s.project_id
    WHERE s.id = NEW.access_surface_id
      AND s.project_id = NEW.project_id
      AND s.org_id = NEW.org_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'credential surface/project/org mismatch';
    END IF;
    IF selected_surface_mode NOT IN ('r', 'rw') THEN
        RAISE EXCEPTION 'credential surface has an invalid mode';
    END IF;
    IF NEW.status = 'active' AND selected_surface_status <> 'active' THEN
        RAISE EXCEPTION 'active credential requires an active surface';
    END IF;

    -- Older application versions omit the expand-phase lifecycle column. The
    -- trigger derives it before NOT NULL/check constraints are evaluated.
    IF NEW.credential_lifecycle IS NULL THEN
        NEW.credential_lifecycle := CASE
            WHEN NEW.workspace_binding_id IS NOT NULL THEN 'binding'
            WHEN NEW.expires_at IS NOT NULL THEN 'session'
            ELSE 'shared'
        END;
    END IF;
    IF (NEW.workspace_binding_id IS NOT NULL)
       IS DISTINCT FROM (NEW.credential_lifecycle = 'binding') THEN
        RAISE EXCEPTION 'credential lifecycle/workspace binding mismatch';
    END IF;
    IF NEW.credential_lifecycle = 'session' AND NEW.expires_at IS NULL THEN
        RAISE EXCEPTION 'session credential requires an expiry';
    END IF;

    IF NEW.credential_type IN ('git_http_token', 'ssh_public_key')
       AND selected_kind <> 'git_remote' THEN
        RAISE EXCEPTION 'Git credential requires a git_remote surface';
    END IF;
    IF NEW.credential_type = 'bearer_token'
       AND selected_kind NOT IN ('cli', 'agent', 'mcp', 'sandbox') THEN
        RAISE EXCEPTION 'bearer credential is invalid for this surface kind';
    END IF;

    IF NEW.workspace_binding_id IS NOT NULL THEN
        SELECT * INTO selected_binding
        FROM public.project_workspace_bindings
        WHERE id = NEW.workspace_binding_id;

        IF NOT FOUND
           OR selected_binding.project_id IS DISTINCT FROM NEW.project_id
           OR selected_binding.org_id IS DISTINCT FROM NEW.org_id
           OR selected_binding.scope_id IS DISTINCT FROM selected_scope_id THEN
            RAISE EXCEPTION 'credential/workspace binding mismatch';
        END IF;
        IF NEW.status = 'active' AND selected_binding.status <> 'active' THEN
            RAISE EXCEPTION 'active credential requires an active workspace binding';
        END IF;
    END IF;

    -- Older application versions omit the new column. During the expand
    -- window derive the narrowest existing capability instead of breaking
    -- their inserts; new issuers always send it explicitly.
    IF NEW.grant_mode IS NULL THEN
        NEW.grant_mode := COALESCE(selected_binding.mode, selected_scope_mode, 'r');
    END IF;
    IF NEW.grant_mode = 'rw' AND selected_scope_mode <> 'rw' THEN
        RAISE EXCEPTION 'credential mode cannot exceed scope mode';
    END IF;
    IF NEW.workspace_binding_id IS NOT NULL
       AND NEW.grant_mode = 'rw'
       AND selected_binding.mode <> 'rw' THEN
        RAISE EXCEPTION 'credential mode cannot exceed workspace binding mode';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_validate_access_surface_credential
    ON public.access_surface_credentials;
CREATE TRIGGER trg_validate_access_surface_credential
    BEFORE INSERT OR UPDATE OF
      org_id, project_id, access_surface_id, workspace_binding_id,
      credential_type, grant_mode, credential_lifecycle, status, expires_at
    ON public.access_surface_credentials
    FOR EACH ROW EXECUTE FUNCTION public._validate_access_surface_credential();

-- Preserve short-lived bearer sessions when rotating the shared CLI/Agent/MCP
-- credential. This is the same lifecycle boundary used by canonical Git and
-- keeps older issuers safe during the expand rollout.
CREATE OR REPLACE FUNCTION public.rotate_access_surface_bearer_token(
    p_access_surface_id text,
    p_org_id text,
    p_project_id text,
    p_key_prefix text,
    p_key_last4 text,
    p_key_hash text,
    p_hash_alg text,
    p_created_by uuid DEFAULT NULL,
    p_expires_at timestamptz DEFAULT NULL
)
RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    new_id text;
BEGIN
    PERFORM 1
    FROM public.access_surfaces s
    WHERE s.id = p_access_surface_id
      AND s.project_id = p_project_id
      AND (
          p_org_id IS NULL
          OR COALESCE(
              s.org_id,
              (SELECT p.org_id FROM public.projects p WHERE p.id = s.project_id)
          ) IS NOT DISTINCT FROM p_org_id
      )
    FOR UPDATE OF s;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'access surface/project/org mismatch';
    END IF;

    UPDATE public.access_surface_credentials
       SET status = 'revoked', revoked_at = now()
     WHERE access_surface_id = p_access_surface_id
       AND credential_type = 'bearer_token'
       AND workspace_binding_id IS NULL
       AND credential_lifecycle = 'shared'
       AND status = 'active';

    INSERT INTO public.access_surface_credentials (
        org_id, project_id, access_surface_id, workspace_binding_id,
        credential_type, credential_lifecycle,
        key_prefix, key_last4, key_hash, hash_alg, status,
        created_by, expires_at
    ) VALUES (
        p_org_id, p_project_id, p_access_surface_id, NULL,
        'bearer_token', 'shared', p_key_prefix, p_key_last4,
        p_key_hash, p_hash_alg, 'active', p_created_by, p_expires_at
    ) RETURNING id INTO new_id;
    RETURN new_id;
END;
$$;

REVOKE ALL ON FUNCTION public.rotate_access_surface_bearer_token(
    text, text, text, text, text, text, text, uuid, timestamptz
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.rotate_access_surface_bearer_token(
    text, text, text, text, text, text, text, uuid, timestamptz
) TO service_role;

-- Atomic rotation for a shared Git HTTP credential.  Device-specific binding
-- credentials are intentionally excluded from this rotation domain.
CREATE OR REPLACE FUNCTION public.rotate_access_surface_git_http_token(
    p_access_surface_id text,
    p_org_id text,
    p_project_id text,
    p_grant_mode text,
    p_key_prefix text,
    p_key_last4 text,
    p_key_hash text,
    p_hash_alg text,
    p_created_by uuid DEFAULT NULL,
    p_expires_at timestamptz DEFAULT NULL
)
RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    new_id text;
BEGIN
    IF p_grant_mode NOT IN ('r', 'rw') THEN
        RAISE EXCEPTION 'invalid Git credential mode';
    END IF;
    -- Serialize manual/shared rotation per Access Surface. Session credentials
    -- use the non-rotating insert path and may coexist; this lock only prevents
    -- two concurrent rotations of the same r/rw slot from leaving duplicates.
    PERFORM 1
        FROM public.access_surfaces s
        JOIN public.repo_scopes rs
          ON rs.id = s.scope_id AND rs.project_id = s.project_id
        WHERE s.id = p_access_surface_id
          AND s.project_id = p_project_id
          AND s.org_id = p_org_id
          AND s.kind = 'git_remote'
          AND s.status = 'active'
          AND (p_grant_mode = 'r' OR rs.mode = 'rw')
        FOR UPDATE OF s;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Git credential surface or capability is invalid';
    END IF;

    -- A shared Surface has one independently rotatable r slot and one rw slot.
    -- Rotating one mode must not revoke the other mode or any expiring session
    -- credential issued with revoke_existing=false.
    UPDATE public.access_surface_credentials
       SET status = 'revoked', revoked_at = now()
     WHERE access_surface_id = p_access_surface_id
       AND credential_type = 'git_http_token'
       AND workspace_binding_id IS NULL
       AND credential_lifecycle = 'shared'
       AND grant_mode = p_grant_mode
       AND status = 'active';

    INSERT INTO public.access_surface_credentials (
        id, org_id, project_id, access_surface_id, workspace_binding_id,
        credential_type, grant_mode, credential_lifecycle,
        key_prefix, key_last4, key_hash, hash_alg, status, created_by, expires_at
    ) VALUES (
        gen_random_uuid()::text, p_org_id, p_project_id,
        p_access_surface_id, NULL, 'git_http_token', p_grant_mode, 'shared',
        p_key_prefix, p_key_last4, p_key_hash, p_hash_alg,
        'active', p_created_by, p_expires_at
    ) RETURNING id INTO new_id;
    RETURN new_id;
END;
$$;

REVOKE ALL ON FUNCTION public.rotate_access_surface_git_http_token(
    text, text, text, text, text, text, text, text, uuid, timestamptz
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.rotate_access_surface_git_http_token(
    text, text, text, text, text, text, text, text, uuid, timestamptz
) TO service_role;

-- Add a Git-specific RPC instead of changing the legacy CLI/bearer RPC in
-- place. This keeps the schema expand phase deployable before every backend
-- instance has moved to canonical remotes.
CREATE OR REPLACE FUNCTION public.create_project_workspace_git_binding(
    p_binding_id text,
    p_org_id text,
    p_project_id text,
    p_scope_id text,
    p_workspace_instance_id text,
    p_bound_user_id uuid,
    p_cloud_origin text,
    p_binding_kind text,
    p_mode text,
    p_access_surface_id text,
    p_credential_id text,
    p_key_prefix text,
    p_key_last4 text,
    p_key_hash text,
    p_hash_alg text
)
RETURNS SETOF public.project_workspace_bindings
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    resolved_org_id text;
    effective_role text;
    created_binding public.project_workspace_bindings%ROWTYPE;
BEGIN
    SELECT r.org_id, r.effective_role
      INTO resolved_org_id, effective_role
    FROM public.resolve_project_role(p_project_id, p_bound_user_id) r;

    IF NOT FOUND OR resolved_org_id IS DISTINCT FROM p_org_id THEN
        RAISE EXCEPTION 'project binding authorization denied'
            USING ERRCODE = '42501';
    END IF;
    IF effective_role IS NULL OR (p_mode = 'rw' AND effective_role = 'viewer') THEN
        RAISE EXCEPTION 'project binding capability denied'
            USING ERRCODE = '42501';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM public.access_surfaces
        WHERE id = p_access_surface_id
          AND project_id = p_project_id
          AND org_id = p_org_id
          AND scope_id = p_scope_id
          AND kind = 'git_remote'
          AND status = 'active'
    ) THEN
        RAISE EXCEPTION 'binding Git surface is invalid';
    END IF;

    INSERT INTO public.project_workspace_bindings (
        id, org_id, project_id, scope_id, workspace_instance_id,
        bound_user_id, cloud_origin, binding_kind, mode, created_by
    ) VALUES (
        p_binding_id, p_org_id, p_project_id, p_scope_id,
        p_workspace_instance_id, p_bound_user_id,
        lower(trim(trailing '/' FROM p_cloud_origin)),
        p_binding_kind, p_mode, p_bound_user_id
    ) RETURNING * INTO created_binding;

    INSERT INTO public.access_surface_credentials (
        id, org_id, project_id, access_surface_id, workspace_binding_id,
        credential_type, grant_mode, credential_lifecycle,
        key_prefix, key_last4, key_hash, hash_alg, status, created_by
    ) VALUES (
        p_credential_id, p_org_id, p_project_id, p_access_surface_id,
        p_binding_id, 'git_http_token', p_mode, 'binding', p_key_prefix, p_key_last4,
        p_key_hash, p_hash_alg, 'active', p_bound_user_id
    );

    INSERT INTO public.audit_logs (
        action, path, project_id, operator_type, operator_id, status, metadata
    ) VALUES (
        'workspace_binding.create', '', p_project_id, 'user',
        p_bound_user_id::text, 'success',
        jsonb_build_object(
            'binding_id', p_binding_id,
            'binding_kind', p_binding_kind,
            'scope_id', p_scope_id,
            'mode', p_mode,
            'credential_type', 'git_http_token'
        )
    );

    RETURN NEXT created_binding;
END;
$$;

REVOKE ALL ON FUNCTION public.create_project_workspace_git_binding(
    text, text, text, text, text, uuid, text, text, text, text, text,
    text, text, text, text
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.create_project_workspace_git_binding(
    text, text, text, text, text, uuid, text, text, text, text, text,
    text, text, text, text
) TO service_role;

CREATE OR REPLACE FUNCTION public.rotate_project_workspace_binding_git_credential(
    p_binding_id text,
    p_bound_user_id uuid,
    p_credential_id text,
    p_key_prefix text,
    p_key_last4 text,
    p_key_hash text,
    p_hash_alg text
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    selected_binding public.project_workspace_bindings%ROWTYPE;
    selected_surface_id text;
    effective_role text;
BEGIN
    SELECT * INTO selected_binding
    FROM public.project_workspace_bindings
    WHERE id = p_binding_id
      AND bound_user_id = p_bound_user_id
      AND status = 'active'
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN false;
    END IF;

    SELECT r.effective_role INTO effective_role
    FROM public.resolve_project_role(
        selected_binding.project_id, p_bound_user_id
    ) r;
    IF effective_role IS NULL
       OR (selected_binding.mode = 'rw' AND effective_role = 'viewer') THEN
        RAISE EXCEPTION 'workspace credential capability denied'
            USING ERRCODE = '42501';
    END IF;

    -- Resolve the surface from the binding geometry, not from the previous
    -- credential. Bindings created before this migration carry a bearer token
    -- on the CLI surface; their first rotation is the lazy, plaintext-free
    -- cutover to a Git HTTP token on the canonical Git surface.
    SELECT s.id INTO selected_surface_id
    FROM public.access_surfaces s
    JOIN public.repo_scopes rs
      ON rs.id = selected_binding.scope_id
     AND rs.project_id = selected_binding.project_id
    WHERE s.project_id = selected_binding.project_id
      AND s.org_id = selected_binding.org_id
      AND s.scope_id = selected_binding.scope_id
      AND s.kind = 'git_remote'
      AND s.status = 'active'
      AND (selected_binding.mode = 'r' OR rs.mode = 'rw')
    ORDER BY s.created_at ASC
    LIMIT 1;
    IF selected_surface_id IS NULL THEN
        RETURN false;
    END IF;

    UPDATE public.access_surface_credentials
    SET status = 'revoked', revoked_at = now()
    WHERE workspace_binding_id = p_binding_id AND status = 'active';

    INSERT INTO public.access_surface_credentials (
        id, org_id, project_id, access_surface_id, workspace_binding_id,
        credential_type, grant_mode, credential_lifecycle,
        key_prefix, key_last4, key_hash, hash_alg, status, created_by
    ) VALUES (
        p_credential_id, selected_binding.org_id, selected_binding.project_id,
        selected_surface_id, p_binding_id, 'git_http_token',
        selected_binding.mode, 'binding', p_key_prefix, p_key_last4, p_key_hash,
        p_hash_alg, 'active', p_bound_user_id
    );

    INSERT INTO public.audit_logs (
        action, path, project_id, operator_type, operator_id, status, metadata
    ) VALUES (
        'workspace_binding.credential.rotate', '', selected_binding.project_id,
        'user', p_bound_user_id::text, 'success',
        jsonb_build_object(
            'binding_id', p_binding_id,
            'credential_type', 'git_http_token'
        )
    );
    RETURN true;
END;
$$;

REVOKE ALL ON FUNCTION public.rotate_project_workspace_binding_git_credential(
    text, uuid, text, text, text, text, text
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.rotate_project_workspace_binding_git_credential(
    text, uuid, text, text, text, text, text
) TO service_role;

-- Failure compensation for Desktop/local configuration. Revoking a freshly
-- issued credential must not destroy an already-existing binding identity.
CREATE OR REPLACE FUNCTION public.revoke_project_workspace_binding_git_credential(
    p_binding_id text,
    p_bound_user_id uuid
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    selected_binding public.project_workspace_bindings%ROWTYPE;
    revoked_count integer;
BEGIN
    SELECT * INTO selected_binding
    FROM public.project_workspace_bindings
    WHERE id = p_binding_id
      AND bound_user_id = p_bound_user_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN false;
    END IF;

    UPDATE public.access_surface_credentials
       SET status = 'revoked', revoked_at = now()
     WHERE workspace_binding_id = p_binding_id
       AND credential_type = 'git_http_token'
       AND status = 'active';
    GET DIAGNOSTICS revoked_count = ROW_COUNT;

    INSERT INTO public.audit_logs (
        action, path, project_id, operator_type, operator_id, status, metadata
    ) VALUES (
        'workspace_binding.credential.revoke', '', selected_binding.project_id,
        'user', p_bound_user_id::text, 'success',
        jsonb_build_object(
            'binding_id', p_binding_id,
            'credential_type', 'git_http_token',
            'revoked_count', revoked_count,
            'reason', 'local_configuration_compensation'
        )
    );
    RETURN true;
END;
$$;

REVOKE ALL ON FUNCTION public.revoke_project_workspace_binding_git_credential(
    text, uuid
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.revoke_project_workspace_binding_git_credential(
    text, uuid
) TO service_role;

-- One credential lookup is the canonical machine-auth boundary.  It returns
-- authorization facts only; Version Engine object/ref/transaction code never
-- sees the secret or the persistence rows that produced the grant.
CREATE OR REPLACE FUNCTION public.resolve_git_runtime_credential(
    p_key_hash text
)
RETURNS TABLE(
    credential_id text,
    org_id text,
    project_id text,
    access_surface_id text,
    scope_id text,
    scope_path text,
    scope_exclude jsonb,
    scope_is_root boolean,
    workspace_binding_id text,
    bound_user_id uuid,
    effective_mode text
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
SELECT
    c.id,
    c.org_id,
    c.project_id,
    c.access_surface_id,
    rs.id,
    rs.path,
    rs.exclude,
    rs.is_root,
    c.workspace_binding_id,
    b.bound_user_id,
    CASE
        WHEN c.grant_mode <> 'rw'
          OR rs.mode <> 'rw'
          OR COALESCE(s.config ->> 'mode', 'rw') <> 'rw'
          OR COALESCE(b.mode, 'rw') <> 'rw'
          OR pr.effective_role = 'viewer'
        THEN 'r'
        ELSE 'rw'
    END AS effective_mode
FROM public.access_surface_credentials c
JOIN public.access_surfaces s
  ON s.id = c.access_surface_id
 AND s.project_id = c.project_id
 AND s.org_id = c.org_id
 AND s.kind = 'git_remote'
 AND s.status = 'active'
JOIN public.repo_scopes rs
  ON rs.id = s.scope_id
 AND rs.project_id = s.project_id
LEFT JOIN public.project_workspace_bindings b
  ON b.id = c.workspace_binding_id
LEFT JOIN LATERAL public.resolve_project_role(
    c.project_id, b.bound_user_id
) pr ON c.workspace_binding_id IS NOT NULL
WHERE c.key_hash = p_key_hash
  AND c.credential_type = 'git_http_token'
  AND c.status = 'active'
  AND (c.expires_at IS NULL OR c.expires_at > now())
  AND (
      c.workspace_binding_id IS NULL
      OR (
          b.id IS NOT NULL
          AND b.status = 'active'
          AND b.project_id = c.project_id
          AND b.org_id = c.org_id
          AND b.scope_id = s.scope_id
          AND pr.effective_role IS NOT NULL
          AND (b.mode = 'r' OR pr.effective_role <> 'viewer')
      )
  )
LIMIT 1;
$$;

REVOKE ALL ON FUNCTION public.resolve_git_runtime_credential(text)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.resolve_git_runtime_credential(text)
    TO service_role;

COMMIT;
