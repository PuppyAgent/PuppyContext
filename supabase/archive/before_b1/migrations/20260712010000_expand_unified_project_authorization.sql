-- ==========================================================================
-- Unified human Project authorization and explicit Desktop workspace binding
-- ==========================================================================
-- Final control/data-plane model:
--   organizations, org_members, projects, project_members,
--   project_workspace_bindings, repo_scopes, access_surfaces,
--   access_surface_credentials, access_surface_policies.
--
-- This migration is additive except for tightening invalid tenant facts. The
-- versioned data migration
-- 20260712_repo_user_permissions_to_project_members performs the blocking
-- legacy permission copy. A later contract migration removes
-- repo_user_permissions only after Qubits and Production verification.

BEGIN;

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '15min';

-- Composite keys let child tables prove that related rows belong to the same
-- tenant/project instead of trusting application-side joins.
CREATE UNIQUE INDEX IF NOT EXISTS uq_projects_id_org
    ON public.projects(id, org_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_org_members_org_user
    ON public.org_members(org_id, user_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_repo_scopes_id_project
    ON public.repo_scopes(id, project_id);

ALTER TABLE public.project_members
    ADD COLUMN IF NOT EXISTS org_id text,
    ADD COLUMN IF NOT EXISTS granted_by uuid REFERENCES auth.users(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();

UPDATE public.project_members pm
SET org_id = p.org_id,
    granted_by = COALESCE(pm.granted_by, p.created_by)
FROM public.projects p
WHERE p.id = pm.project_id
  AND (pm.org_id IS NULL OR pm.granted_by IS NULL);

DO $$
DECLARE
    invalid_count bigint;
BEGIN
    SELECT count(*) INTO invalid_count
    FROM public.project_members pm
    LEFT JOIN public.projects p
      ON p.id = pm.project_id AND p.org_id = pm.org_id
    LEFT JOIN public.org_members om
      ON om.org_id = pm.org_id AND om.user_id = pm.user_id
    WHERE pm.org_id IS NULL OR p.id IS NULL OR om.id IS NULL;

    IF invalid_count > 0 THEN
        RAISE EXCEPTION
          'unified authorization migration blocked: % invalid project_members tenant facts',
          invalid_count;
    END IF;
END $$;

ALTER TABLE public.project_members ALTER COLUMN org_id SET NOT NULL;

ALTER TABLE public.project_members
    DROP CONSTRAINT IF EXISTS project_members_project_org_fkey,
    DROP CONSTRAINT IF EXISTS project_members_org_user_fkey;
ALTER TABLE public.project_members
    ADD CONSTRAINT project_members_project_org_fkey
      FOREIGN KEY (project_id, org_id)
      REFERENCES public.projects(id, org_id) ON DELETE CASCADE,
    ADD CONSTRAINT project_members_org_user_fkey
      FOREIGN KEY (org_id, user_id)
      REFERENCES public.org_members(org_id, user_id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS idx_project_members_user_org_project
    ON public.project_members(user_id, org_id, project_id);

CREATE OR REPLACE FUNCTION public._project_members_bump_updated_at()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_project_members_updated_at ON public.project_members;
CREATE TRIGGER trg_project_members_updated_at
    BEFORE UPDATE ON public.project_members
    FOR EACH ROW EXECUTE FUNCTION public._project_members_bump_updated_at();

-- Restore the invariant for legacy Projects whose old best-effort creator
-- membership write was lost. Projects with an explicit non-Admin creator row
-- are reported by preflight instead of silently widening that role.
INSERT INTO public.project_members (
    id, org_id, project_id, user_id, role, granted_by
)
SELECT
    gen_random_uuid()::text,
    p.org_id,
    p.id,
    p.created_by,
    'admin',
    p.created_by
FROM public.projects p
JOIN public.org_members om
  ON om.org_id = p.org_id AND om.user_id = p.created_by
LEFT JOIN public.project_members pm
  ON pm.project_id = p.id AND pm.user_id = p.created_by
WHERE p.created_by IS NOT NULL AND pm.id IS NULL
ON CONFLICT (project_id, user_id) DO NOTHING;

-- A Project and its creator's Admin grant are one fact publication.
CREATE OR REPLACE FUNCTION public.create_project_with_admin(
    p_id text,
    p_name text,
    p_description text,
    p_org_id text,
    p_created_by uuid,
    p_share_token text
)
RETURNS SETOF public.projects
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    created_project public.projects%ROWTYPE;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM public.org_members
        WHERE org_id = p_org_id AND user_id = p_created_by
    ) THEN
        RAISE EXCEPTION 'project creator must be an organization member'
            USING ERRCODE = '42501';
    END IF;

    INSERT INTO public.projects (
        id, name, description, org_id, created_by, share_token
    ) VALUES (
        p_id, p_name, p_description, p_org_id, p_created_by, p_share_token
    ) RETURNING * INTO created_project;

    INSERT INTO public.project_members (
        id, org_id, project_id, user_id, role, granted_by
    ) VALUES (
        gen_random_uuid()::text, p_org_id, p_id, p_created_by, 'admin', p_created_by
    );

    RETURN NEXT created_project;
END;
$$;

REVOKE ALL ON FUNCTION public.create_project_with_admin(
    text, text, text, text, uuid, text
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.create_project_with_admin(
    text, text, text, text, uuid, text
) TO service_role;

-- Canonical transaction-time role resolver. Application authorization still
-- flows through AuthorizationService; mutation RPCs use this function as a
-- final TOCTOU defence at commit time.
CREATE OR REPLACE FUNCTION public.resolve_project_role(
    p_project_id text,
    p_user_id uuid
)
RETURNS TABLE(org_id text, effective_role text, grant_source text)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
WITH facts AS (
    SELECT
        p.org_id,
        p.visibility,
        om.role AS org_role,
        pm.role AS project_role,
        pm.org_id AS project_member_org_id
    FROM public.projects p
    JOIN public.org_members om
      ON om.org_id = p.org_id AND om.user_id = p_user_id
    LEFT JOIN public.project_members pm
      ON pm.project_id = p.id
     AND pm.org_id = p.org_id
     AND pm.user_id = p_user_id
    WHERE p.id = p_project_id
), resolved AS (
    SELECT
        org_id,
        CASE
            WHEN org_role = 'owner' THEN 'admin'
            WHEN project_role IN ('admin', 'editor', 'viewer')
             AND project_member_org_id = org_id THEN project_role
            WHEN visibility = 'org' THEN 'viewer'
            ELSE NULL
        END AS effective_role,
        CASE
            WHEN org_role = 'owner' THEN 'org_owner'
            WHEN project_role IN ('admin', 'editor', 'viewer')
             AND project_member_org_id = org_id THEN 'project_member'
            WHEN visibility = 'org' THEN 'org_visibility'
            ELSE NULL
        END AS grant_source
    FROM facts
)
SELECT org_id, effective_role, grant_source
FROM resolved
WHERE effective_role IS NOT NULL;
$$;

REVOKE ALL ON FUNCTION public.resolve_project_role(text, uuid)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.resolve_project_role(text, uuid)
    TO service_role;

-- Project member management is re-authorized inside the transaction. These
-- RPCs also write the audit event atomically with the membership fact.
CREATE OR REPLACE FUNCTION public.add_project_member_authorized(
    p_project_id text,
    p_target_user_id uuid,
    p_role text,
    p_actor_user_id uuid
)
RETURNS SETOF public.project_members
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    project_org_id text;
    actor_role text;
    created_member public.project_members%ROWTYPE;
BEGIN
    IF p_role NOT IN ('admin', 'editor', 'viewer') THEN
        RAISE EXCEPTION 'invalid project role' USING ERRCODE = '22023';
    END IF;

    SELECT r.org_id, r.effective_role
      INTO project_org_id, actor_role
    FROM public.resolve_project_role(p_project_id, p_actor_user_id) r;
    IF actor_role IS DISTINCT FROM 'admin' THEN
        RAISE EXCEPTION 'project member management denied'
            USING ERRCODE = '42501';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM public.org_members
        WHERE org_id = project_org_id AND user_id = p_target_user_id
    ) THEN
        RAISE EXCEPTION 'project member must belong to the organization'
            USING ERRCODE = '42501';
    END IF;

    INSERT INTO public.project_members (
        id, org_id, project_id, user_id, role, granted_by
    ) VALUES (
        gen_random_uuid()::text, project_org_id, p_project_id,
        p_target_user_id, p_role, p_actor_user_id
    ) RETURNING * INTO created_member;

    INSERT INTO public.audit_logs (
        action, path, project_id, operator_type, operator_id, status, metadata
    ) VALUES (
        'project_member.add', '', p_project_id, 'user',
        p_actor_user_id::text, 'success',
        jsonb_build_object('target_user_id', p_target_user_id, 'role', p_role)
    );
    RETURN NEXT created_member;
END;
$$;

CREATE OR REPLACE FUNCTION public.update_project_member_role_authorized(
    p_project_id text,
    p_target_user_id uuid,
    p_role text,
    p_actor_user_id uuid
)
RETURNS SETOF public.project_members
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    actor_role text;
    previous_role text;
    updated_member public.project_members%ROWTYPE;
BEGIN
    IF p_role NOT IN ('admin', 'editor', 'viewer') THEN
        RAISE EXCEPTION 'invalid project role' USING ERRCODE = '22023';
    END IF;
    SELECT r.effective_role INTO actor_role
    FROM public.resolve_project_role(p_project_id, p_actor_user_id) r;
    IF actor_role IS DISTINCT FROM 'admin' THEN
        RAISE EXCEPTION 'project member management denied'
            USING ERRCODE = '42501';
    END IF;

    SELECT role INTO previous_role
    FROM public.project_members
    WHERE project_id = p_project_id AND user_id = p_target_user_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN;
    END IF;

    UPDATE public.project_members
    SET role = p_role, granted_by = p_actor_user_id
    WHERE project_id = p_project_id AND user_id = p_target_user_id
    RETURNING * INTO updated_member;

    INSERT INTO public.audit_logs (
        action, path, project_id, operator_type, operator_id, status, metadata
    ) VALUES (
        'project_member.role.update', '', p_project_id, 'user',
        p_actor_user_id::text, 'success',
        jsonb_build_object(
            'target_user_id', p_target_user_id,
            'previous_role', previous_role,
            'role', p_role
        )
    );
    RETURN NEXT updated_member;
END;
$$;

CREATE OR REPLACE FUNCTION public.remove_project_member_authorized(
    p_project_id text,
    p_target_user_id uuid,
    p_actor_user_id uuid
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    actor_role text;
    removed_count integer;
    previous_role text;
BEGIN
    SELECT r.effective_role INTO actor_role
    FROM public.resolve_project_role(p_project_id, p_actor_user_id) r;
    IF actor_role IS DISTINCT FROM 'admin' THEN
        RAISE EXCEPTION 'project member management denied'
            USING ERRCODE = '42501';
    END IF;

    SELECT role INTO previous_role
    FROM public.project_members
    WHERE project_id = p_project_id AND user_id = p_target_user_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN false;
    END IF;

    DELETE FROM public.project_members
    WHERE project_id = p_project_id AND user_id = p_target_user_id;
    GET DIAGNOSTICS removed_count = ROW_COUNT;

    INSERT INTO public.audit_logs (
        action, path, project_id, operator_type, operator_id, status, metadata
    ) VALUES (
        'project_member.remove', '', p_project_id, 'user',
        p_actor_user_id::text, 'success',
        jsonb_build_object(
            'target_user_id', p_target_user_id,
            'previous_role', previous_role
        )
    );
    RETURN removed_count > 0;
END;
$$;

CREATE OR REPLACE FUNCTION public.join_project_via_share_token(
    p_share_token text,
    p_user_id uuid
)
RETURNS TABLE(
    project_id text,
    project_name text,
    role text,
    newly_joined boolean
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    selected_project public.projects%ROWTYPE;
    existing_role text;
BEGIN
    SELECT * INTO selected_project
    FROM public.projects
    WHERE share_token = p_share_token
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM public.org_members
        WHERE org_id = selected_project.org_id AND user_id = p_user_id
    ) THEN
        RAISE EXCEPTION 'share recipient must belong to the organization'
            USING ERRCODE = '42501';
    END IF;

    SELECT pm.role INTO existing_role
    FROM public.project_members pm
    WHERE pm.project_id = selected_project.id AND pm.user_id = p_user_id;
    IF existing_role IS NOT NULL THEN
        RETURN QUERY SELECT selected_project.id, selected_project.name,
            existing_role, false;
        RETURN;
    END IF;

    INSERT INTO public.project_members (
        id, org_id, project_id, user_id, role, granted_by
    ) VALUES (
        gen_random_uuid()::text, selected_project.org_id,
        selected_project.id, p_user_id, 'viewer', p_user_id
    );
    INSERT INTO public.audit_logs (
        action, path, project_id, operator_type, operator_id, status, metadata
    ) VALUES (
        'project_member.share_join', '', selected_project.id, 'user',
        p_user_id::text, 'success', jsonb_build_object('role', 'viewer')
    );
    RETURN QUERY SELECT selected_project.id, selected_project.name,
        'viewer'::text, true;
END;
$$;

REVOKE ALL ON FUNCTION public.add_project_member_authorized(text, uuid, text, uuid),
    public.update_project_member_role_authorized(text, uuid, text, uuid),
    public.remove_project_member_authorized(text, uuid, uuid),
    public.join_project_via_share_token(text, uuid)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.add_project_member_authorized(text, uuid, text, uuid),
    public.update_project_member_role_authorized(text, uuid, text, uuid),
    public.remove_project_member_authorized(text, uuid, uuid),
    public.join_project_via_share_token(text, uuid)
    TO service_role;

-- Access surfaces and credentials carry explicit tenant integrity.
UPDATE public.access_surfaces s
SET org_id = p.org_id
FROM public.projects p
WHERE p.id = s.project_id AND s.org_id IS DISTINCT FROM p.org_id;

DO $$
DECLARE
    invalid_count bigint;
BEGIN
    SELECT count(*) INTO invalid_count
    FROM public.access_surfaces s
    LEFT JOIN public.projects p
      ON p.id = s.project_id AND p.org_id = s.org_id
    LEFT JOIN public.repo_scopes rs
      ON rs.id = s.scope_id AND rs.project_id = s.project_id
    WHERE p.id IS NULL OR rs.id IS NULL;
    IF invalid_count > 0 THEN
        RAISE EXCEPTION
          'unified authorization migration blocked: % orphan access surfaces',
          invalid_count;
    END IF;
END $$;

ALTER TABLE public.access_surfaces ALTER COLUMN org_id SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_access_surfaces_id_project_org
    ON public.access_surfaces(id, project_id, org_id);
ALTER TABLE public.access_surfaces
    DROP CONSTRAINT IF EXISTS access_surfaces_project_org_fkey,
    DROP CONSTRAINT IF EXISTS access_surfaces_scope_project_fkey;
ALTER TABLE public.access_surfaces
    ADD CONSTRAINT access_surfaces_project_org_fkey
      FOREIGN KEY (project_id, org_id)
      REFERENCES public.projects(id, org_id) ON DELETE CASCADE,
    ADD CONSTRAINT access_surfaces_scope_project_fkey
      FOREIGN KEY (scope_id, project_id)
      REFERENCES public.repo_scopes(id, project_id) ON DELETE CASCADE;

UPDATE public.access_surface_credentials c
SET org_id = p.org_id
FROM public.projects p
WHERE p.id = c.project_id AND c.org_id IS DISTINCT FROM p.org_id;

DO $$
DECLARE
    invalid_count bigint;
BEGIN
    SELECT count(*) INTO invalid_count
    FROM public.access_surface_credentials c
    LEFT JOIN public.access_surfaces s
      ON s.id = c.access_surface_id
     AND s.project_id = c.project_id
     AND s.org_id = c.org_id
    WHERE c.org_id IS NULL OR s.id IS NULL;
    IF invalid_count > 0 THEN
        RAISE EXCEPTION
          'unified authorization migration blocked: % orphan access credentials',
          invalid_count;
    END IF;
END $$;
ALTER TABLE public.access_surface_credentials ALTER COLUMN org_id SET NOT NULL;

-- Agent/MCP tool bindings are a child boundary of Project authorization.
-- A surface may use a Project-local tool from the same Project or an
-- Organization-level tool from the same tenant, never a sibling Project or
-- another tenant.  The historical access_points FK was dropped with that
-- table, so re-anchor the junction to access_surfaces explicitly.
UPDATE public.access_tools binding
SET access_point_id = (
    SELECT min(s.id)
    FROM public.access_surfaces s
    WHERE s.config ->> 'legacy_connector_id' = binding.access_point_id
)
WHERE NOT EXISTS (
        SELECT 1 FROM public.access_surfaces current_surface
        WHERE current_surface.id = binding.access_point_id
    )
  AND 1 = (
        SELECT count(*) FROM public.access_surfaces mapped_surface
        WHERE mapped_surface.config ->> 'legacy_connector_id' = binding.access_point_id
    );

DO $$
DECLARE
    invalid_count bigint;
BEGIN
    SELECT count(*) INTO invalid_count
    FROM public.access_tools at
    LEFT JOIN public.access_surfaces s ON s.id = at.access_point_id
    LEFT JOIN public.tools t ON t.id = at.tool_id
    WHERE s.id IS NULL
       OR t.id IS NULL
       OR t.org_id IS DISTINCT FROM s.org_id
       OR (t.project_id IS NOT NULL AND t.project_id IS DISTINCT FROM s.project_id);
    IF invalid_count > 0 THEN
        RAISE EXCEPTION
          'unified authorization migration blocked: % invalid access tool bindings',
          invalid_count;
    END IF;
END $$;

ALTER TABLE public.access_tools
    DROP CONSTRAINT IF EXISTS access_tools_surface_fkey;
ALTER TABLE public.access_tools
    ADD CONSTRAINT access_tools_surface_fkey
      FOREIGN KEY (access_point_id)
      REFERENCES public.access_surfaces(id) ON DELETE CASCADE;

CREATE OR REPLACE FUNCTION public._validate_access_tool_project_boundary()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    surface_project_id text;
    surface_org_id text;
    tool_project_id text;
    tool_org_id text;
BEGIN
    SELECT project_id, org_id INTO surface_project_id, surface_org_id
    FROM public.access_surfaces WHERE id = NEW.access_point_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'access tool surface not found';
    END IF;

    SELECT project_id, org_id INTO tool_project_id, tool_org_id
    FROM public.tools WHERE id = NEW.tool_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'access tool not found';
    END IF;

    IF tool_org_id IS DISTINCT FROM surface_org_id
       OR (tool_project_id IS NOT NULL
           AND tool_project_id IS DISTINCT FROM surface_project_id) THEN
        RAISE EXCEPTION 'access tool crosses Project or Organization boundary';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_validate_access_tool_project_boundary
    ON public.access_tools;
CREATE TRIGGER trg_validate_access_tool_project_boundary
    BEFORE INSERT OR UPDATE OF access_point_id, tool_id ON public.access_tools
    FOR EACH ROW EXECUTE FUNCTION public._validate_access_tool_project_boundary();

-- Explicit identity link. It deliberately stores no local path, secret, role,
-- or capability snapshot.
CREATE TABLE IF NOT EXISTS public.project_workspace_bindings (
    id                    text PRIMARY KEY DEFAULT gen_random_uuid()::text,
    org_id                text NOT NULL,
    project_id            text NOT NULL,
    scope_id              text NOT NULL,
    workspace_instance_id text NOT NULL,
    bound_user_id         uuid NOT NULL,
    cloud_origin          text NOT NULL,
    binding_kind          text NOT NULL CHECK (binding_kind IN ('full', 'scoped')),
    mode                  text NOT NULL CHECK (mode IN ('r', 'rw')),
    status                text NOT NULL DEFAULT 'active'
                            CHECK (status IN ('active', 'revoked')),
    created_by            uuid REFERENCES auth.users(id) ON DELETE SET NULL,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    last_seen_at          timestamptz NOT NULL DEFAULT now(),
    revoked_at            timestamptz,

    CONSTRAINT project_workspace_bindings_origin_nonempty
      CHECK (cloud_origin = lower(trim(trailing '/' FROM cloud_origin))
             AND cloud_origin ~ '^https?://[^/@?#[:space:]]+(:[0-9]+)?$'),
    CONSTRAINT project_workspace_bindings_instance_nonempty
      CHECK (length(trim(workspace_instance_id)) BETWEEN 16 AND 200),
    CONSTRAINT project_workspace_bindings_revoke_shape
      CHECK ((status = 'active' AND revoked_at IS NULL)
          OR (status = 'revoked' AND revoked_at IS NOT NULL)),
    CONSTRAINT project_workspace_bindings_project_org_fkey
      FOREIGN KEY (project_id, org_id)
      REFERENCES public.projects(id, org_id) ON DELETE CASCADE,
    CONSTRAINT project_workspace_bindings_scope_project_fkey
      FOREIGN KEY (scope_id, project_id)
      REFERENCES public.repo_scopes(id, project_id) ON DELETE CASCADE,
    CONSTRAINT project_workspace_bindings_org_user_fkey
      FOREIGN KEY (org_id, bound_user_id)
      REFERENCES public.org_members(org_id, user_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_project_workspace_bindings_active_instance
    ON public.project_workspace_bindings(workspace_instance_id)
    WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_project_workspace_bindings_user_project
    ON public.project_workspace_bindings(bound_user_id, project_id, status);
CREATE INDEX IF NOT EXISTS idx_project_workspace_bindings_project
    ON public.project_workspace_bindings(project_id, status, updated_at DESC);

CREATE OR REPLACE FUNCTION public._validate_project_workspace_binding()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    scope_root boolean;
    scope_mode text;
BEGIN
    SELECT is_root, mode INTO scope_root, scope_mode
    FROM public.repo_scopes
    WHERE id = NEW.scope_id AND project_id = NEW.project_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'workspace binding scope/project mismatch';
    END IF;
    IF NEW.binding_kind = 'full' AND NOT scope_root THEN
        RAISE EXCEPTION 'full workspace binding requires canonical root scope';
    END IF;
    IF NEW.binding_kind = 'scoped' AND scope_root THEN
        RAISE EXCEPTION 'scoped workspace binding requires a non-root scope';
    END IF;
    IF NEW.mode = 'rw' AND scope_mode <> 'rw' THEN
        RAISE EXCEPTION 'binding mode cannot exceed scope mode';
    END IF;

    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_validate_project_workspace_binding
    ON public.project_workspace_bindings;
CREATE TRIGGER trg_validate_project_workspace_binding
    BEFORE INSERT OR UPDATE ON public.project_workspace_bindings
    FOR EACH ROW EXECUTE FUNCTION public._validate_project_workspace_binding();

ALTER TABLE public.project_workspace_bindings ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS project_workspace_bindings_service_role_all
    ON public.project_workspace_bindings;
CREATE POLICY project_workspace_bindings_service_role_all
    ON public.project_workspace_bindings
    FOR ALL TO service_role USING (true) WITH CHECK (true);

ALTER TABLE public.access_surface_credentials
    ADD COLUMN IF NOT EXISTS workspace_binding_id text
      REFERENCES public.project_workspace_bindings(id) ON DELETE CASCADE;

CREATE UNIQUE INDEX IF NOT EXISTS uq_access_surface_credentials_active_binding_type
    ON public.access_surface_credentials(workspace_binding_id, credential_type)
    WHERE status = 'active' AND workspace_binding_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_access_surface_credentials_binding
    ON public.access_surface_credentials(workspace_binding_id, status);

-- Shared surface-key rotation must not revoke device-specific binding keys.
-- The original RPC predates workspace_binding_id and rotated every bearer
-- token on a surface, disconnecting unrelated local workspaces.
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
    IF p_org_id IS NULL OR NOT EXISTS (
        SELECT 1 FROM public.access_surfaces s
        WHERE s.id = p_access_surface_id
          AND s.project_id = p_project_id
          AND s.org_id = p_org_id
    ) THEN
        RAISE EXCEPTION 'access surface/project/org mismatch';
    END IF;

    UPDATE public.access_surface_credentials
       SET status = 'revoked', revoked_at = now()
     WHERE access_surface_id = p_access_surface_id
       AND credential_type = 'bearer_token'
       AND workspace_binding_id IS NULL
       AND status = 'active';

    INSERT INTO public.access_surface_credentials (
        org_id, project_id, access_surface_id, workspace_binding_id,
        credential_type, key_prefix, key_last4, key_hash, hash_alg,
        status, created_by, expires_at
    ) VALUES (
        p_org_id, p_project_id, p_access_surface_id, NULL,
        'bearer_token', p_key_prefix, p_key_last4, p_key_hash, p_hash_alg,
        'active', p_created_by, p_expires_at
    ) RETURNING id INTO new_id;
    RETURN new_id;
END;
$$;

-- CREATE OR REPLACE currently preserves the ACL established by the previous
-- migration.  Repeat it here so this security boundary remains correct when
-- the function is inspected, replayed, or consolidated independently.
REVOKE ALL ON FUNCTION public.rotate_access_surface_bearer_token(
    text, text, text, text, text, text, text, uuid, timestamptz
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.rotate_access_surface_bearer_token(
    text, text, text, text, text, text, text, uuid, timestamptz
) TO service_role;

ALTER TABLE public.access_surface_credentials
    DROP CONSTRAINT IF EXISTS access_surface_credentials_surface_tenant_fkey;
ALTER TABLE public.access_surface_credentials
    ADD CONSTRAINT access_surface_credentials_surface_tenant_fkey
      FOREIGN KEY (access_surface_id, project_id, org_id)
      REFERENCES public.access_surfaces(id, project_id, org_id) ON DELETE CASCADE;

-- Atomic binding + independent credential publication. Application code sends
-- only the hash and one-time display metadata; plaintext never crosses SQL.
CREATE OR REPLACE FUNCTION public.create_project_workspace_binding(
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
          AND kind = 'cli'
          AND status = 'active'
    ) THEN
        RAISE EXCEPTION 'binding credential surface is invalid';
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
        credential_type, key_prefix, key_last4, key_hash, hash_alg,
        status, created_by
    ) VALUES (
        p_credential_id, p_org_id, p_project_id, p_access_surface_id,
        p_binding_id, 'bearer_token', p_key_prefix, p_key_last4,
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
            'mode', p_mode
        )
    );

    RETURN NEXT created_binding;
END;
$$;

REVOKE ALL ON FUNCTION public.create_project_workspace_binding(
    text, text, text, text, text, uuid, text, text, text, text, text,
    text, text, text, text
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.create_project_workspace_binding(
    text, text, text, text, text, uuid, text, text, text, text, text,
    text, text, text, text
) TO service_role;

CREATE OR REPLACE FUNCTION public.revoke_project_workspace_binding(
    p_binding_id text,
    p_bound_user_id uuid
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    changed_count integer;
BEGIN
    UPDATE public.project_workspace_bindings
    SET status = 'revoked', revoked_at = now(), updated_at = now()
    WHERE id = p_binding_id
      AND bound_user_id = p_bound_user_id
      AND status = 'active';
    GET DIAGNOSTICS changed_count = ROW_COUNT;

    IF changed_count > 0 THEN
        UPDATE public.access_surface_credentials
        SET status = 'revoked', revoked_at = now()
        WHERE workspace_binding_id = p_binding_id AND status = 'active';

        INSERT INTO public.audit_logs (
            action, path, project_id, operator_type, operator_id, status, metadata
        )
        SELECT
            'workspace_binding.revoke', '', b.project_id, 'user',
            p_bound_user_id::text, 'success',
            jsonb_build_object('binding_id', b.id)
        FROM public.project_workspace_bindings b
        WHERE b.id = p_binding_id;
    END IF;

    RETURN changed_count > 0;
END;
$$;

REVOKE ALL ON FUNCTION public.revoke_project_workspace_binding(text, uuid)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.revoke_project_workspace_binding(text, uuid)
    TO service_role;

CREATE OR REPLACE FUNCTION public.revoke_project_workspace_binding_admin(
    p_binding_id text,
    p_project_id text,
    p_actor_user_id uuid
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    actor_role text;
    changed_count integer;
BEGIN
    SELECT r.effective_role INTO actor_role
    FROM public.resolve_project_role(p_project_id, p_actor_user_id) r;
    IF actor_role IS DISTINCT FROM 'admin' THEN
        RAISE EXCEPTION 'workspace binding management denied'
            USING ERRCODE = '42501';
    END IF;

    UPDATE public.project_workspace_bindings
    SET status = 'revoked', revoked_at = now(), updated_at = now()
    WHERE id = p_binding_id
      AND project_id = p_project_id
      AND status = 'active';
    GET DIAGNOSTICS changed_count = ROW_COUNT;

    IF changed_count > 0 THEN
        UPDATE public.access_surface_credentials
        SET status = 'revoked', revoked_at = now()
        WHERE workspace_binding_id = p_binding_id AND status = 'active';

        INSERT INTO public.audit_logs (
            action, path, project_id, operator_type, operator_id, status, metadata
        ) VALUES (
            'workspace_binding.admin_revoke', '', p_project_id, 'user',
            p_actor_user_id::text, 'success',
            jsonb_build_object('binding_id', p_binding_id)
        );
    END IF;

    RETURN changed_count > 0;
END;
$$;

CREATE OR REPLACE FUNCTION public.rotate_project_workspace_binding_credential(
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

    SELECT c.access_surface_id INTO selected_surface_id
    FROM public.access_surface_credentials c
    JOIN public.access_surfaces s
      ON s.id = c.access_surface_id
     AND s.project_id = selected_binding.project_id
     AND s.org_id = selected_binding.org_id
     AND s.scope_id = selected_binding.scope_id
     AND s.status = 'active'
    JOIN public.repo_scopes rs
      ON rs.id = selected_binding.scope_id
     AND rs.project_id = selected_binding.project_id
    WHERE c.workspace_binding_id = p_binding_id
      AND c.credential_type = 'bearer_token'
      AND (
          selected_binding.mode = 'r'
          OR rs.mode = 'rw'
      )
    ORDER BY c.created_at DESC
    LIMIT 1;
    IF selected_surface_id IS NULL THEN
        RETURN false;
    END IF;

    UPDATE public.access_surface_credentials
    SET status = 'revoked', revoked_at = now()
    WHERE workspace_binding_id = p_binding_id AND status = 'active';

    INSERT INTO public.access_surface_credentials (
        id, org_id, project_id, access_surface_id, workspace_binding_id,
        credential_type, key_prefix, key_last4, key_hash, hash_alg,
        status, created_by
    ) VALUES (
        p_credential_id, selected_binding.org_id, selected_binding.project_id,
        selected_surface_id, p_binding_id, 'bearer_token', p_key_prefix,
        p_key_last4, p_key_hash, p_hash_alg, 'active', p_bound_user_id
    );
    INSERT INTO public.audit_logs (
        action, path, project_id, operator_type, operator_id, status, metadata
    ) VALUES (
        'workspace_binding.credential.rotate', '', selected_binding.project_id,
        'user', p_bound_user_id::text, 'success',
        jsonb_build_object('binding_id', p_binding_id)
    );
    RETURN true;
END;
$$;

REVOKE ALL ON FUNCTION public.revoke_project_workspace_binding_admin(text, text, uuid),
    public.rotate_project_workspace_binding_credential(
        text, uuid, text, text, text, text, text
    ) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.revoke_project_workspace_binding_admin(text, text, uuid),
    public.rotate_project_workspace_binding_credential(
        text, uuid, text, text, text, text, text
    ) TO service_role;

-- Reconcile human-bound machine credentials whenever a Human Project grant
-- shrinks. A Viewer may keep r credentials; rw credentials do not survive a
-- downgrade, and complete Project access loss revokes every binding secret.
CREATE OR REPLACE FUNCTION public.reconcile_workspace_binding_credentials(
    p_project_id text,
    p_user_id uuid
)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    effective_role text;
    changed_count integer;
BEGIN
    SELECT r.effective_role INTO effective_role
    FROM public.resolve_project_role(p_project_id, p_user_id) r;

    UPDATE public.access_surface_credentials c
    SET status = 'revoked', revoked_at = now()
    FROM public.project_workspace_bindings b
    WHERE c.workspace_binding_id = b.id
      AND c.status = 'active'
      AND b.project_id = p_project_id
      AND b.bound_user_id = p_user_id
      AND (
          effective_role IS NULL
          OR (effective_role = 'viewer' AND b.mode = 'rw')
      );
    GET DIAGNOSTICS changed_count = ROW_COUNT;
    RETURN changed_count;
END;
$$;

CREATE OR REPLACE FUNCTION public._project_member_reconcile_binding_credentials()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        PERFORM public.reconcile_workspace_binding_credentials(
            OLD.project_id, OLD.user_id
        );
        RETURN OLD;
    END IF;
    PERFORM public.reconcile_workspace_binding_credentials(
        NEW.project_id, NEW.user_id
    );
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_project_member_reconcile_binding_credentials
    ON public.project_members;
CREATE TRIGGER trg_project_member_reconcile_binding_credentials
    AFTER UPDATE OF role OR DELETE ON public.project_members
    FOR EACH ROW EXECUTE FUNCTION public._project_member_reconcile_binding_credentials();

CREATE OR REPLACE FUNCTION public._project_visibility_reconcile_binding_credentials()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    binding_user uuid;
BEGIN
    IF NEW.visibility IS DISTINCT FROM OLD.visibility THEN
        FOR binding_user IN
            SELECT DISTINCT bound_user_id
            FROM public.project_workspace_bindings
            WHERE project_id = NEW.id AND status = 'active'
        LOOP
            PERFORM public.reconcile_workspace_binding_credentials(
                NEW.id, binding_user
            );
        END LOOP;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_project_visibility_reconcile_binding_credentials
    ON public.projects;
CREATE TRIGGER trg_project_visibility_reconcile_binding_credentials
    AFTER UPDATE OF visibility ON public.projects
    FOR EACH ROW EXECUTE FUNCTION public._project_visibility_reconcile_binding_credentials();

CREATE OR REPLACE FUNCTION public._org_role_reconcile_binding_credentials()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    bound_project_id text;
BEGIN
    IF NEW.role IS DISTINCT FROM OLD.role THEN
        FOR bound_project_id IN
            SELECT DISTINCT project_id
            FROM public.project_workspace_bindings
            WHERE org_id = NEW.org_id
              AND bound_user_id = NEW.user_id
              AND status = 'active'
        LOOP
            PERFORM public.reconcile_workspace_binding_credentials(
                bound_project_id, NEW.user_id
            );
        END LOOP;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_org_role_reconcile_binding_credentials
    ON public.org_members;
CREATE TRIGGER trg_org_role_reconcile_binding_credentials
    AFTER UPDATE OF role ON public.org_members
    FOR EACH ROW EXECUTE FUNCTION public._org_role_reconcile_binding_credentials();

REVOKE ALL ON FUNCTION public.reconcile_workspace_binding_credentials(text, uuid)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.reconcile_workspace_binding_credentials(text, uuid)
    TO service_role;

-- Scope geometry is part of RuntimeGrant, not Human authorization.  A scope
-- downgrade must nevertheless invalidate an already-issued rw binding secret,
-- and an active binding must never outlive a root/non-root identity flip.
CREATE OR REPLACE FUNCTION public._repo_scope_reconcile_workspace_bindings()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
    IF NEW.is_root IS DISTINCT FROM OLD.is_root
       AND EXISTS (
           SELECT 1 FROM public.project_workspace_bindings b
           WHERE b.scope_id = NEW.id AND b.status = 'active'
       ) THEN
        RAISE EXCEPTION 'cannot change root identity of a bound scope';
    END IF;

    IF OLD.mode = 'rw' AND NEW.mode = 'r' THEN
        UPDATE public.access_surface_credentials c
        SET status = 'revoked', revoked_at = now()
        FROM public.project_workspace_bindings b
        WHERE c.workspace_binding_id = b.id
          AND c.status = 'active'
          AND b.scope_id = NEW.id
          AND b.mode = 'rw'
          AND b.status = 'active';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_repo_scope_reconcile_workspace_bindings
    ON public.repo_scopes;
CREATE TRIGGER trg_repo_scope_reconcile_workspace_bindings
    BEFORE UPDATE OF is_root, mode ON public.repo_scopes
    FOR EACH ROW EXECUTE FUNCTION public._repo_scope_reconcile_workspace_bindings();

-- Replace the Version Engine transaction-time write-state resolver with the
-- exact same effective-role precedence. The compatibility wrapper installed
-- by the physical-rename migration delegates to this canonical function.
CREATE OR REPLACE FUNCTION public.get_version_project_write_state(
    p_project_id text,
    p_user_id text
) RETURNS TABLE (
    project_id text,
    project_name text,
    org_id text,
    visibility text,
    role text,
    can_write boolean,
    root_hash text,
    head_commit_id text
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
SELECT
    p.id::text,
    p.name::text,
    p.org_id::text,
    COALESCE(p.visibility, 'private')::text,
    r.effective_role::text,
    (r.effective_role IN ('admin', 'editor')),
    COALESCE(p.version_root_hash, '')::text,
    COALESCE(s.head_commit_id, '')::text
FROM public.projects p
JOIN public.resolve_project_role(p.id, p_user_id::uuid) r ON true
LEFT JOIN public.version_scope_state s
  ON s.project_id = p.id AND s.scope_path = ''
WHERE p.id = p_project_id;
$$;

REVOKE ALL ON FUNCTION public.get_version_project_write_state(text, text)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.get_version_project_write_state(text, text)
    TO service_role;

-- Machine-readable preflight report used by deployment and CI.
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
    'duplicate_or_missing_root_scopes', (
        SELECT count(*) FROM (
            SELECT p.id
            FROM public.projects p
            LEFT JOIN public.repo_scopes s
              ON s.project_id = p.id AND s.is_root = true
            GROUP BY p.id
            HAVING count(s.id) <> 1
        ) invalid_roots
    ),
    'orphan_access_surfaces', (
        SELECT count(*) FROM public.access_surfaces s
        LEFT JOIN public.projects p
          ON p.id = s.project_id AND p.org_id = s.org_id
        LEFT JOIN public.repo_scopes rs
          ON rs.id = s.scope_id AND rs.project_id = s.project_id
        WHERE p.id IS NULL OR rs.id IS NULL
    ),
    'orphan_access_credentials', (
        SELECT count(*) FROM public.access_surface_credentials c
        LEFT JOIN public.access_surfaces s
          ON s.id = c.access_surface_id
         AND s.project_id = c.project_id
         AND s.org_id = c.org_id
        WHERE s.id IS NULL
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
    'legacy_denied', CASE WHEN to_regclass('public.repo_user_permissions') IS NULL
        THEN 0 ELSE (SELECT count(*) FROM public.repo_user_permissions WHERE role = 'denied') END,
    'legacy_unknown_roles', CASE WHEN to_regclass('public.repo_user_permissions') IS NULL
        THEN 0 ELSE (SELECT count(*) FROM public.repo_user_permissions
                     WHERE role NOT IN ('admin', 'editor', 'reader', 'denied')) END,
    'legacy_scoped', CASE WHEN to_regclass('public.repo_user_permissions') IS NULL
        THEN 0 ELSE (SELECT count(*) FROM public.repo_user_permissions
                     WHERE allowed_scope_ids IS NOT NULL
                       AND allowed_scope_ids <> 'null'::jsonb
                       AND CASE jsonb_typeof(allowed_scope_ids)
                           WHEN 'array' THEN jsonb_array_length(allowed_scope_ids) > 0
                           ELSE true
                       END) END,
    'legacy_tenant_mismatch', CASE WHEN to_regclass('public.repo_user_permissions') IS NULL
        THEN 0 ELSE (
            SELECT count(*) FROM public.repo_user_permissions rp
            JOIN public.projects p ON p.id = rp.project_id
            LEFT JOIN public.org_members om
              ON om.org_id = p.org_id AND om.user_id = rp.user_id
            WHERE om.id IS NULL
        ) END
);
$$;

REVOKE ALL ON FUNCTION public.unified_authorization_preflight()
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.unified_authorization_preflight()
    TO service_role;

COMMIT;
