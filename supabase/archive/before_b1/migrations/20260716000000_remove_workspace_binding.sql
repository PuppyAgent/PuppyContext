-- Remove local Workspace Binding identity.
--
-- A canonical Git remote is the only local-to-Cloud Project locator. Human
-- UI access is authorized by ProjectGrant; Git data-plane access is authorized
-- by a separate user-owned or service credential. The Cloud never registers,
-- attests, heartbeats, or inventories local checkouts.

BEGIN;

-- Stop legacy reconciliation before changing credential ownership.
DROP TRIGGER IF EXISTS trg_project_member_reconcile_binding_credentials
    ON public.project_members;
DROP TRIGGER IF EXISTS trg_project_visibility_reconcile_binding_credentials
    ON public.projects;
DROP TRIGGER IF EXISTS trg_org_role_reconcile_binding_credentials
    ON public.org_members;
DROP TRIGGER IF EXISTS trg_repository_scope_reconcile_workspace_bindings
    ON public.repository_scopes;
DROP TRIGGER IF EXISTS trg_validate_access_surface_credential
    ON public.access_surface_credentials;

ALTER TABLE public.access_surface_credentials
    ADD COLUMN IF NOT EXISTS user_id uuid;

ALTER TABLE public.access_surface_credentials
    DROP CONSTRAINT IF EXISTS access_surface_credentials_lifecycle_check,
    DROP CONSTRAINT IF EXISTS access_surface_credentials_lifecycle_shape_check;

-- Preserve usable Git credentials without preserving checkout identity. The
-- credential belongs to the human who created the former binding and remains
-- bounded by its exact Access Surface and grant_mode.
UPDATE public.access_surface_credentials c
SET user_id = b.bound_user_id,
    credential_lifecycle = 'user',
    status = CASE WHEN b.status = 'active' THEN c.status ELSE 'revoked' END,
    revoked_at = CASE
        WHEN b.status = 'active' THEN c.revoked_at
        ELSE COALESCE(c.revoked_at, b.revoked_at, now())
    END
FROM public.project_workspace_bindings b
WHERE c.workspace_binding_id = b.id
  AND c.credential_type = 'git_http_token';

-- The first registration implementation issued CLI bearer tokens. They cannot
-- be mapped to the final user-Git credential model without silently widening
-- them into shared service credentials. Retire those obsolete principals
-- instead of carrying a compatibility lifecycle into the final schema.
DELETE FROM public.access_surface_credentials
WHERE workspace_binding_id IS NOT NULL
  AND credential_type <> 'git_http_token';

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM public.access_surface_credentials
        WHERE credential_lifecycle = 'binding' OR (
            workspace_binding_id IS NOT NULL AND user_id IS NULL
        )
    ) THEN
        RAISE EXCEPTION 'Workspace Binding credential ownership backfill failed';
    END IF;
END;
$$;

-- Current membership is authoritative. Membership loss revokes the credential
-- during cutover. A role downgrade does not rewrite credential identity: the
-- runtime resolver dynamically caps an existing rw credential to read-only.
UPDATE public.access_surface_credentials c
SET status = 'revoked',
    revoked_at = COALESCE(c.revoked_at, now())
WHERE c.credential_lifecycle = 'user'
  AND c.status = 'active'
  AND NOT EXISTS (
      SELECT 1
      FROM public.resolve_project_role(c.project_id, c.user_id) role
      WHERE role.effective_role IS NOT NULL
  );

DROP INDEX IF EXISTS public.uq_access_surface_credentials_active_binding_type;
DROP INDEX IF EXISTS public.idx_access_surface_credentials_binding;

-- Drop every routine whose compiled body refers to the legacy table/column
-- before the physical schema cut. They are either recreated below or retired.
DROP FUNCTION IF EXISTS public.resolve_git_runtime_credential CASCADE;
DROP FUNCTION IF EXISTS public.rotate_access_surface_bearer_token CASCADE;
DROP FUNCTION IF EXISTS public.rotate_access_surface_git_http_token CASCADE;
DROP FUNCTION IF EXISTS public.unified_authorization_preflight CASCADE;
DROP FUNCTION IF EXISTS public.repository_target_integrity_report CASCADE;
DROP FUNCTION IF EXISTS public._validate_access_surface_credential CASCADE;
DROP FUNCTION IF EXISTS public.create_project_workspace_binding CASCADE;
DROP FUNCTION IF EXISTS public.create_project_workspace_git_binding CASCADE;
DROP FUNCTION IF EXISTS public.revoke_project_workspace_binding CASCADE;
DROP FUNCTION IF EXISTS public.revoke_project_workspace_binding_admin CASCADE;
DROP FUNCTION IF EXISTS public.rotate_project_workspace_binding_credential CASCADE;
DROP FUNCTION IF EXISTS public.rotate_project_workspace_binding_git_credential CASCADE;
DROP FUNCTION IF EXISTS public.revoke_project_workspace_binding_git_credential CASCADE;
DROP FUNCTION IF EXISTS public.reconcile_workspace_binding_credentials CASCADE;
DROP FUNCTION IF EXISTS public._project_member_reconcile_binding_credentials CASCADE;
DROP FUNCTION IF EXISTS public._project_visibility_reconcile_binding_credentials CASCADE;
DROP FUNCTION IF EXISTS public._org_role_reconcile_binding_credentials CASCADE;
DROP FUNCTION IF EXISTS public._repository_scope_reconcile_workspace_bindings CASCADE;
DROP FUNCTION IF EXISTS public._validate_project_workspace_binding CASCADE;

ALTER TABLE public.access_surface_credentials
    DROP COLUMN workspace_binding_id;

DROP TABLE public.project_workspace_bindings CASCADE;

ALTER TABLE public.access_surface_credentials
    ADD CONSTRAINT access_surface_credentials_user_org_fkey
      FOREIGN KEY (org_id, user_id)
      REFERENCES public.org_members(org_id, user_id) ON DELETE CASCADE,
    ADD CONSTRAINT access_surface_credentials_lifecycle_check
      CHECK (credential_lifecycle IN ('shared', 'session', 'user')),
    ADD CONSTRAINT access_surface_credentials_lifecycle_shape_check
      CHECK (
          (
              credential_lifecycle = 'user'
              AND user_id IS NOT NULL
              AND credential_type = 'git_http_token'
          )
          OR (
              credential_lifecycle IN ('shared', 'session')
              AND user_id IS NULL
              AND (credential_lifecycle <> 'session' OR expires_at IS NOT NULL)
          )
      );

CREATE INDEX idx_access_surface_credentials_user
    ON public.access_surface_credentials(user_id, project_id, status)
    WHERE user_id IS NOT NULL;

COMMENT ON COLUMN public.access_surface_credentials.user_id IS
  'Owner of a user-issued Git credential. This identifies a human principal, never a device, folder, or checkout.';
COMMENT ON COLUMN public.access_surface_credentials.credential_lifecycle IS
  'Independent credential domain: shared service slot, expiring session, or user-owned Git credential.';
COMMENT ON COLUMN public.access_surface_credentials.grant_mode IS
  'Credential capability ceiling. Effective Git mode is the minimum of credential, target, Surface policy, and the owner current ProjectGrant.';

CREATE OR REPLACE FUNCTION public._validate_access_surface_credential()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    selected_kind text;
    selected_target_max_mode text;
    selected_surface_mode text;
    selected_surface_status text;
    selected_user_org text;
    selected_user_role text;
BEGIN
    SELECT
        s.kind,
        CASE WHEN s.scope_id IS NULL THEN 'rw' ELSE rs.max_mode END,
        COALESCE(s.config ->> 'mode', 'rw'),
        s.status
      INTO
        selected_kind,
        selected_target_max_mode,
        selected_surface_mode,
        selected_surface_status
    FROM public.access_surfaces s
    LEFT JOIN public.repository_scopes rs
      ON rs.id = s.scope_id AND rs.project_id = s.project_id
    WHERE s.id = NEW.access_surface_id
      AND s.project_id = NEW.project_id
      AND s.org_id = NEW.org_id
      AND (s.scope_id IS NULL OR rs.id IS NOT NULL);

    IF NOT FOUND THEN
        RAISE EXCEPTION 'credential Surface/Project/Organization mismatch';
    END IF;
    IF selected_surface_mode NOT IN ('r', 'rw') THEN
        RAISE EXCEPTION 'credential Surface has an invalid mode';
    END IF;
    IF NEW.status = 'active' AND selected_surface_status <> 'active' THEN
        RAISE EXCEPTION 'active credential requires an active Surface';
    END IF;
    IF NEW.credential_lifecycle IS NULL THEN
        NEW.credential_lifecycle := CASE
            WHEN NEW.user_id IS NOT NULL THEN 'user'
            WHEN NEW.expires_at IS NOT NULL THEN 'session'
            ELSE 'shared'
        END;
    END IF;
    IF (NEW.user_id IS NOT NULL)
       IS DISTINCT FROM (NEW.credential_lifecycle = 'user') THEN
        RAISE EXCEPTION 'credential lifecycle/user owner mismatch';
    END IF;
    IF NEW.credential_lifecycle = 'session' AND NEW.expires_at IS NULL THEN
        RAISE EXCEPTION 'session credential requires an expiry';
    END IF;
    IF NEW.credential_lifecycle = 'user'
       AND NEW.credential_type <> 'git_http_token' THEN
        RAISE EXCEPTION 'user lifecycle is only valid for Git credentials';
    END IF;
    IF NEW.credential_type IN ('git_http_token', 'ssh_public_key')
       AND selected_kind <> 'git_remote' THEN
        RAISE EXCEPTION 'Git credential requires a git_remote Surface';
    END IF;
    IF NEW.credential_type = 'bearer_token'
       AND selected_kind NOT IN ('cli', 'agent', 'mcp', 'sandbox') THEN
        RAISE EXCEPTION 'bearer credential is invalid for this Surface kind';
    END IF;

    IF NEW.grant_mode IS NULL THEN
        NEW.grant_mode := COALESCE(selected_target_max_mode, 'r');
    END IF;
    IF NEW.grant_mode = 'rw' AND selected_target_max_mode <> 'rw' THEN
        RAISE EXCEPTION 'credential mode cannot exceed target mode';
    END IF;

    IF NEW.status = 'active' AND NEW.credential_lifecycle = 'user' THEN
        SELECT role.org_id, role.effective_role
          INTO selected_user_org, selected_user_role
        FROM public.resolve_project_role(NEW.project_id, NEW.user_id) role;
        IF selected_user_org IS DISTINCT FROM NEW.org_id
           OR selected_user_role IS NULL THEN
            RAISE EXCEPTION 'user Git credential Project authorization denied'
                USING ERRCODE = '42501';
        END IF;
        IF NEW.grant_mode = 'rw' AND selected_user_role = 'viewer' THEN
            RAISE EXCEPTION 'user Git credential write authorization denied'
                USING ERRCODE = '42501';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_validate_access_surface_credential
    BEFORE INSERT OR UPDATE OF
      org_id, project_id, access_surface_id, user_id,
      credential_type, grant_mode, credential_lifecycle, status, expires_at
    ON public.access_surface_credentials
    FOR EACH ROW EXECUTE FUNCTION public._validate_access_surface_credential();

-- Shared rotation remains a separate administration domain. It never touches
-- session or user-owned credentials on the same Surface.
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
        RAISE EXCEPTION 'access Surface/Project/Organization mismatch';
    END IF;

    UPDATE public.access_surface_credentials
       SET status = 'revoked', revoked_at = now()
     WHERE access_surface_id = p_access_surface_id
       AND credential_type = 'bearer_token'
       AND credential_lifecycle = 'shared'
       AND status = 'active';

    INSERT INTO public.access_surface_credentials (
        org_id, project_id, access_surface_id,
        credential_type, credential_lifecycle,
        key_prefix, key_last4, key_hash, hash_alg, status,
        created_by, expires_at
    ) VALUES (
        p_org_id, p_project_id, p_access_surface_id,
        'bearer_token', 'shared', p_key_prefix, p_key_last4,
        p_key_hash, p_hash_alg, 'active', p_created_by, p_expires_at
    ) RETURNING id INTO new_id;
    RETURN new_id;
END;
$$;

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
    PERFORM 1
    FROM public.access_surfaces s
    LEFT JOIN public.repository_scopes rs
      ON rs.id = s.scope_id AND rs.project_id = s.project_id
    WHERE s.id = p_access_surface_id
      AND s.project_id = p_project_id
      AND s.org_id = p_org_id
      AND s.kind = 'git_remote'
      AND s.status = 'active'
      AND (s.scope_id IS NULL OR rs.id IS NOT NULL)
      AND (p_grant_mode = 'r' OR s.scope_id IS NULL OR rs.max_mode = 'rw')
    FOR UPDATE OF s;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Git credential Surface or capability is invalid';
    END IF;

    UPDATE public.access_surface_credentials
       SET status = 'revoked', revoked_at = now()
     WHERE access_surface_id = p_access_surface_id
       AND credential_type = 'git_http_token'
       AND credential_lifecycle = 'shared'
       AND grant_mode = p_grant_mode
       AND status = 'active';

    INSERT INTO public.access_surface_credentials (
        id, org_id, project_id, access_surface_id,
        credential_type, grant_mode, credential_lifecycle,
        key_prefix, key_last4, key_hash, hash_alg, status, created_by, expires_at
    ) VALUES (
        gen_random_uuid()::text, p_org_id, p_project_id,
        p_access_surface_id, 'git_http_token', p_grant_mode, 'shared',
        p_key_prefix, p_key_last4, p_key_hash, p_hash_alg,
        'active', p_created_by, p_expires_at
    ) RETURNING id INTO new_id;
    RETURN new_id;
END;
$$;

-- One current user may have any number of local Git clients. The credential
-- records the human principal and exact repository target; it intentionally
-- records no device or checkout identifier.
CREATE FUNCTION public.issue_user_git_http_credential(
    p_credential_id text,
    p_access_surface_id text,
    p_org_id text,
    p_project_id text,
    p_scope_id text,
    p_user_id uuid,
    p_grant_mode text,
    p_key_prefix text,
    p_key_last4 text,
    p_key_hash text,
    p_hash_alg text
)
RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    resolved_org_id text;
    effective_role text;
    target_max_mode text := 'rw';
    selected_surface_id text;
BEGIN
    IF p_grant_mode NOT IN ('r', 'rw') THEN
        RAISE EXCEPTION 'invalid Git credential mode';
    END IF;
    SELECT role.org_id, role.effective_role
      INTO resolved_org_id, effective_role
    FROM public.resolve_project_role(p_project_id, p_user_id) role;
    IF resolved_org_id IS DISTINCT FROM p_org_id OR effective_role IS NULL THEN
        RAISE EXCEPTION 'Project Git credential authorization denied'
            USING ERRCODE = '42501';
    END IF;
    IF p_grant_mode = 'rw' AND effective_role = 'viewer' THEN
        RAISE EXCEPTION 'Project Git write credential authorization denied'
            USING ERRCODE = '42501';
    END IF;
    IF p_scope_id IS NOT NULL THEN
        SELECT max_mode INTO target_max_mode
        FROM public.repository_scopes
        WHERE id = p_scope_id AND project_id = p_project_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'repository Scope target is invalid';
        END IF;
    END IF;
    IF p_grant_mode = 'rw' AND target_max_mode <> 'rw' THEN
        RAISE EXCEPTION 'Git credential mode exceeds target mode';
    END IF;

    PERFORM public.ensure_repository_target_access_surfaces(
        p_project_id,
        p_scope_id,
        p_user_id,
        p_access_surface_id,
        NULL
    );
    SELECT id INTO selected_surface_id
    FROM public.access_surfaces
    WHERE project_id = p_project_id
      AND org_id = p_org_id
      AND scope_id IS NOT DISTINCT FROM p_scope_id
      AND kind = 'git_remote'
      AND status = 'active'
    ORDER BY created_at ASC
    LIMIT 1;
    IF selected_surface_id IS NULL THEN
        RAISE EXCEPTION 'Git credential Surface is unavailable';
    END IF;

    INSERT INTO public.access_surface_credentials (
        id, org_id, project_id, access_surface_id, user_id,
        credential_type, grant_mode, credential_lifecycle,
        key_prefix, key_last4, key_hash, hash_alg, status, created_by
    ) VALUES (
        p_credential_id, p_org_id, p_project_id, selected_surface_id, p_user_id,
        'git_http_token', p_grant_mode, 'user', p_key_prefix, p_key_last4,
        p_key_hash, p_hash_alg, 'active', p_user_id
    );

    INSERT INTO public.audit_logs (
        action, path, project_id, operator_type, operator_id, status, metadata
    ) VALUES (
        'git_credential.issue', '', p_project_id, 'user', p_user_id::text,
        'success', jsonb_build_object(
            'credential_id', p_credential_id,
            'target', CASE WHEN p_scope_id IS NULL
                THEN jsonb_build_object('kind', 'project_root', 'project_id', p_project_id)
                ELSE jsonb_build_object(
                    'kind', 'scope', 'project_id', p_project_id, 'scope_id', p_scope_id
                )
            END,
            'mode', p_grant_mode,
            'credential_type', 'git_http_token'
        )
    );
    RETURN p_credential_id;
END;
$$;

-- Revocation identifies only a credential and its owning human principal.
-- It deliberately has no workspace, device, or local-folder dimension.
CREATE FUNCTION public.revoke_user_git_http_credential(
    p_credential_id text,
    p_project_id text,
    p_user_id uuid
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    affected integer;
BEGIN
    UPDATE public.access_surface_credentials
       SET status = 'revoked', revoked_at = COALESCE(revoked_at, now())
     WHERE id = p_credential_id
       AND project_id = p_project_id
       AND user_id = p_user_id
       AND credential_type = 'git_http_token'
       AND credential_lifecycle = 'user'
       AND status = 'active';
    GET DIAGNOSTICS affected = ROW_COUNT;
    IF affected = 0 THEN
        RETURN false;
    END IF;

    INSERT INTO public.audit_logs (
        action, path, project_id, operator_type, operator_id, status, metadata
    ) VALUES (
        'git_credential.revoke', '', p_project_id, 'user', p_user_id::text,
        'success', jsonb_build_object('credential_id', p_credential_id)
    );
    RETURN true;
END;
$$;

CREATE FUNCTION public.resolve_git_runtime_credential(p_key_hash text)
RETURNS TABLE(
    credential_id text,
    org_id text,
    project_id text,
    access_surface_id text,
    target_kind text,
    scope_id text,
    path_prefix text,
    excludes jsonb,
    target_max_mode text,
    user_id uuid,
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
    CASE WHEN s.scope_id IS NULL THEN 'project_root' ELSE 'scope' END,
    s.scope_id,
    COALESCE(rs.path, ''),
    COALESCE(rs.exclude, '[]'::jsonb)
      || COALESCE(
          (
              SELECT jsonb_agg(descendant.path ORDER BY descendant.path)
              FROM public.repository_scopes descendant
              WHERE s.scope_id IS NOT NULL
                AND descendant.project_id = s.project_id
                AND descendant.id <> s.scope_id
                AND left(descendant.path, length(rs.path) + 1) = rs.path || '/'
          ),
          '[]'::jsonb
      ),
    COALESCE(rs.max_mode, 'rw'),
    c.user_id,
    CASE
        WHEN c.grant_mode <> 'rw'
          OR COALESCE(rs.max_mode, 'rw') <> 'rw'
          OR COALESCE(s.config ->> 'mode', 'rw') <> 'rw'
          OR (c.credential_lifecycle = 'user' AND role.effective_role = 'viewer')
        THEN 'r'
        ELSE 'rw'
    END
FROM public.access_surface_credentials c
JOIN public.access_surfaces s
  ON s.id = c.access_surface_id
 AND s.project_id = c.project_id
 AND s.org_id = c.org_id
 AND s.kind = 'git_remote'
 AND s.status = 'active'
LEFT JOIN public.repository_scopes rs
  ON rs.id = s.scope_id AND rs.project_id = s.project_id
LEFT JOIN LATERAL public.resolve_project_role(c.project_id, c.user_id) role
  ON c.credential_lifecycle = 'user'
WHERE c.key_hash = p_key_hash
  AND c.credential_type = 'git_http_token'
  AND c.status = 'active'
  AND (c.expires_at IS NULL OR c.expires_at > now())
  AND (s.scope_id IS NULL OR rs.id IS NOT NULL)
  AND (
      c.credential_lifecycle <> 'user'
      OR (
          c.user_id IS NOT NULL
          AND role.org_id = c.org_id
          AND role.effective_role IS NOT NULL
      )
  )
LIMIT 1;
$$;

REVOKE ALL ON FUNCTION public.rotate_access_surface_bearer_token(
    text, text, text, text, text, text, text, uuid, timestamptz
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.rotate_access_surface_bearer_token(
    text, text, text, text, text, text, text, uuid, timestamptz
) TO service_role;
REVOKE ALL ON FUNCTION public.rotate_access_surface_git_http_token(
    text, text, text, text, text, text, text, text, uuid, timestamptz
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.rotate_access_surface_git_http_token(
    text, text, text, text, text, text, text, text, uuid, timestamptz
) TO service_role;
REVOKE ALL ON FUNCTION public.issue_user_git_http_credential(
    text, text, text, text, text, uuid, text, text, text, text, text
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.issue_user_git_http_credential(
    text, text, text, text, text, uuid, text, text, text, text, text
) TO service_role;
REVOKE ALL ON FUNCTION public.revoke_user_git_http_credential(text, text, uuid)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.revoke_user_git_http_credential(text, text, uuid)
    TO service_role;
REVOKE ALL ON FUNCTION public.resolve_git_runtime_credential(text)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.resolve_git_runtime_credential(text)
    TO service_role;

CREATE OR REPLACE FUNCTION public.unified_authorization_preflight()
RETURNS jsonb
LANGUAGE sql
STABLE
SET search_path = pg_catalog, public, pg_temp
AS $$
SELECT jsonb_build_object(
    'invalid_project_members', (
        SELECT count(*) FROM public.project_members pm
        LEFT JOIN public.projects p
          ON p.id = pm.project_id AND p.org_id = pm.org_id
        LEFT JOIN public.org_members om
          ON om.org_id = pm.org_id AND om.user_id = pm.user_id
        WHERE p.id IS NULL OR om.id IS NULL
    ),
    'creator_admin_unresolved', (
        SELECT count(*) FROM public.projects p
        LEFT JOIN public.org_members om
          ON om.org_id = p.org_id AND om.user_id = p.created_by
        LEFT JOIN public.project_members pm
          ON pm.project_id = p.id AND pm.user_id = p.created_by
        WHERE p.created_by IS NOT NULL
          AND (om.id IS NULL OR pm.role IS DISTINCT FROM 'admin')
    ),
    'invalid_repository_scopes', (
        SELECT count(*) FROM public.repository_scopes
        WHERE path = '' OR path LIKE '/%' OR path LIKE '%/' OR path LIKE '%//%'
    ),
    'orphan_access_surfaces', (
        SELECT count(*) FROM public.access_surfaces s
        LEFT JOIN public.projects p
          ON p.id = s.project_id AND p.org_id = s.org_id
        LEFT JOIN public.repository_scopes rs
          ON rs.id = s.scope_id AND rs.project_id = s.project_id
        WHERE p.id IS NULL OR (s.scope_id IS NOT NULL AND rs.id IS NULL)
    ),
    'orphan_access_credentials', (
        SELECT count(*) FROM public.access_surface_credentials c
        LEFT JOIN public.access_surfaces s
          ON s.id = c.access_surface_id
         AND s.project_id = c.project_id
         AND s.org_id = c.org_id
        LEFT JOIN public.org_members om
          ON om.org_id = c.org_id AND om.user_id = c.user_id
        WHERE s.id IS NULL
           OR (c.credential_lifecycle = 'user' AND om.id IS NULL)
    ),
    'invalid_access_tool_bindings', (
        SELECT count(*) FROM public.access_tools at
        LEFT JOIN public.access_surfaces s ON s.id = at.access_point_id
        LEFT JOIN public.tools t ON t.id = at.tool_id
        WHERE s.id IS NULL
           OR t.id IS NULL
           OR t.org_id IS DISTINCT FROM s.org_id
           OR (t.project_id IS NOT NULL
               AND t.project_id IS DISTINCT FROM s.project_id)
    ),
    'legacy_table_present', to_regclass('public.repo_user_permissions') IS NOT NULL
);
$$;

CREATE OR REPLACE FUNCTION public.repository_target_integrity_report()
RETURNS jsonb
LANGUAGE sql
STABLE
SET search_path = pg_catalog, public, pg_temp
AS $$
SELECT jsonb_build_object(
    'empty_scope_paths', (
        SELECT count(*) FROM public.repository_scopes WHERE path = ''
    ),
    'orphan_surface_targets', (
        SELECT count(*)
        FROM public.access_surfaces s
        LEFT JOIN public.projects p
          ON p.id = s.project_id AND p.org_id = s.org_id
        LEFT JOIN public.repository_scopes rs
          ON rs.id = s.scope_id AND rs.project_id = s.project_id
        WHERE p.id IS NULL OR (s.scope_id IS NOT NULL AND rs.id IS NULL)
    ),
    'credential_target_mismatches', (
        SELECT count(*)
        FROM public.access_surface_credentials c
        LEFT JOIN public.access_surfaces s
          ON s.id = c.access_surface_id
         AND s.project_id = c.project_id
         AND s.org_id = c.org_id
        LEFT JOIN public.org_members om
          ON om.org_id = c.org_id AND om.user_id = c.user_id
        WHERE s.id IS NULL
           OR (c.credential_lifecycle = 'user' AND om.id IS NULL)
    ),
    'duplicate_builtin_targets', (
        SELECT count(*) FROM (
            SELECT project_id, scope_id, kind
            FROM public.access_surfaces
            WHERE kind IN ('git_remote', 'cli', 'filesystem')
            GROUP BY project_id, scope_id, kind
            HAVING count(*) > 1
        ) duplicates
    ),
    'legacy_agent_scope_configs', (
        SELECT count(*)
        FROM public.access_surfaces
        WHERE kind = 'agent' AND config ? 'scope'
    ),
    'orphan_scope_dependents', (
        SELECT count(*)
        FROM (
            SELECT c.project_id, c.scope_id
            FROM public.connections c
            LEFT JOIN public.repository_scopes rs
              ON rs.id = c.scope_id AND rs.project_id = c.project_id
            WHERE c.scope_id IS NOT NULL AND rs.id IS NULL
            UNION ALL
            SELECT s.project_id, s.scope_id
            FROM public.scope_sandbox_sessions s
            LEFT JOIN public.repository_scopes rs
              ON rs.id = s.scope_id AND rs.project_id = s.project_id
            WHERE rs.id IS NULL
            UNION ALL
            SELECT e.project_id, e.scope_id
            FROM public.scope_sync_events e
            LEFT JOIN public.repository_scopes rs
              ON rs.id = e.scope_id AND rs.project_id = e.project_id
            WHERE rs.id IS NULL
            UNION ALL
            SELECT s.project_id, s.scope_id
            FROM public.scope_sync_settings s
            LEFT JOIN public.repository_scopes rs
              ON rs.id = s.scope_id AND rs.project_id = s.project_id
            WHERE rs.id IS NULL
        ) invalid_dependents
    )
);
$$;

REVOKE ALL ON FUNCTION public.unified_authorization_preflight()
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.unified_authorization_preflight()
    TO service_role;
REVOKE ALL ON FUNCTION public.repository_target_integrity_report()
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.repository_target_integrity_report()
    TO service_role;

DO $$
DECLARE
    report jsonb;
BEGIN
    IF to_regclass('public.project_workspace_bindings') IS NOT NULL THEN
        RAISE EXCEPTION 'Workspace Binding table still exists';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'access_surface_credentials'
          AND column_name = 'workspace_binding_id'
    ) THEN
        RAISE EXCEPTION 'Workspace Binding credential column still exists';
    END IF;
    SELECT public.repository_target_integrity_report() INTO report;
    IF EXISTS (
        SELECT 1
        FROM jsonb_each_text(report) item
        WHERE item.value::bigint <> 0
    ) THEN
        RAISE EXCEPTION 'repository target postflight failed: %', report;
    END IF;
END;
$$;

COMMIT;
