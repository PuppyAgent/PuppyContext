-- ISSUE-039: Project-owned canonical repository target cutover.
-- requires-data-migration: 20260715_project_owned_repository_targets_preflight
-- data-migration-checksum: c9c417a19b0ad2a9086588e31775e604e0eefe18d2fcb5c8c1f5ce570661ae55
--
-- This is the single destructive transition from synthetic root Scope rows to
-- the final model:
--   * Project is the canonical root target.
--   * repository_scopes stores only non-empty path boundaries.
--   * access_surfaces/project_workspace_bindings use nullable scope_id.
--   * target kind is derived once; is_root/binding_kind are not persisted.
--
-- Production execution requires the release runbook's maintenance window,
-- restore point, exact-SHA gate, and preflight capture. The transaction aborts
-- before its first mutation when any legacy invariant is corrupt.

BEGIN;

-- ---------------------------------------------------------------------------
-- Read-only preflight. Keep every check before the first schema/data mutation.
-- ---------------------------------------------------------------------------

DO $$
DECLARE
    invalid_count bigint;
BEGIN
    IF EXISTS (SELECT 1 FROM public.projects)
       AND NOT EXISTS (
           SELECT 1
           FROM public.migration_log
           WHERE name = '20260715_project_owned_repository_targets_preflight'
             AND COALESCE((summary ->> 'verified')::boolean, false)
             AND summary ->> 'artifact_checksum' =
                 'c9c417a19b0ad2a9086588e31775e604e0eefe18d2fcb5c8c1f5ce570661ae55'
       ) THEN
        RAISE EXCEPTION
          'DATA_MIGRATION_REQUIRED:20260715_project_owned_repository_targets_preflight';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'repo_scopes'
          AND column_name IN (
              'access_key', 'access_key_hash', 'access_key_revoked_at'
          )
    ) THEN
        RAISE EXCEPTION
          'repository target cutover blocked: legacy Scope credential columns remain';
    END IF;

    SELECT count(*) INTO invalid_count
    FROM (
        SELECT p.id
        FROM public.projects p
        LEFT JOIN public.repo_scopes s
          ON s.project_id = p.id AND s.is_root = true
        GROUP BY p.id
        HAVING count(s.id) <> 1
    ) invalid_roots;
    IF invalid_count > 0 THEN
        RAISE EXCEPTION
          'repository target cutover blocked: % Projects lack exactly one legacy root',
          invalid_count;
    END IF;

    SELECT count(*) INTO invalid_count
    FROM public.repo_scopes
    WHERE (is_root AND (path <> '' OR exclude <> '[]'::jsonb OR mode <> 'rw'))
       OR (NOT is_root AND path = '')
       OR path LIKE '/%'
       OR path LIKE '%/'
       OR path LIKE '%//%';
    IF invalid_count > 0 THEN
        RAISE EXCEPTION
          'repository target cutover blocked: % malformed legacy Scope rows',
          invalid_count;
    END IF;

    IF to_regclass('public.repo_user_permissions') IS NOT NULL THEN
        SELECT count(*) INTO invalid_count
        FROM public.repo_user_permissions;
        IF invalid_count > 0 AND NOT EXISTS (
            SELECT 1
            FROM public.migration_log
            WHERE name = '20260712_repo_user_permissions_to_project_members'
              AND COALESCE((summary ->> 'verified')::boolean, false)
              AND summary ->> 'artifact_checksum' =
                  '649b84361ea1c8b72dfcef8f6c9e5beeafa520a1322b4d9f1ecbb79202fd6bce'
        ) THEN
            RAISE EXCEPTION
              'DATA_MIGRATION_REQUIRED:20260712_repo_user_permissions_to_project_members';
        END IF;

        SELECT count(*) INTO invalid_count
        FROM public.repo_user_permissions rp
        LEFT JOIN public.projects p ON p.id = rp.project_id
        LEFT JOIN public.project_members pm
          ON pm.project_id = rp.project_id
         AND pm.user_id = rp.user_id
         AND pm.role = CASE rp.role
            WHEN 'admin' THEN 'admin'
            WHEN 'editor' THEN 'editor'
            WHEN 'reader' THEN 'viewer'
            ELSE NULL
         END
        WHERE rp.role NOT IN ('admin', 'editor', 'reader')
           OR rp.allowed_scope_ids IS NOT NULL
           OR p.id IS NULL
           OR pm.id IS NULL
           OR pm.org_id IS DISTINCT FROM p.org_id;
        IF invalid_count > 0 THEN
            RAISE EXCEPTION
              'repository target cutover blocked: % unresolved legacy Human permission rows',
              invalid_count;
        END IF;
    END IF;

    SELECT count(*) INTO invalid_count
    FROM public.access_surfaces s
    LEFT JOIN public.projects p
      ON p.id = s.project_id AND p.org_id = s.org_id
    LEFT JOIN public.repo_scopes rs
      ON rs.id = s.scope_id AND rs.project_id = s.project_id
    WHERE p.id IS NULL OR rs.id IS NULL;
    IF invalid_count > 0 THEN
        RAISE EXCEPTION
          'repository target cutover blocked: % invalid Access Surface targets',
          invalid_count;
    END IF;

    SELECT count(*) INTO invalid_count
    FROM public.project_workspace_bindings b
    LEFT JOIN public.projects p
      ON p.id = b.project_id AND p.org_id = b.org_id
    LEFT JOIN public.repo_scopes rs
      ON rs.id = b.scope_id AND rs.project_id = b.project_id
    WHERE p.id IS NULL
       OR rs.id IS NULL
       OR (b.binding_kind = 'full') IS DISTINCT FROM rs.is_root
       OR (b.mode = 'rw' AND rs.mode <> 'rw');
    IF invalid_count > 0 THEN
        RAISE EXCEPTION
          'repository target cutover blocked: % invalid Workspace Binding targets',
          invalid_count;
    END IF;

    SELECT count(*) INTO invalid_count
    FROM public.access_surface_credentials c
    LEFT JOIN public.access_surfaces s
      ON s.id = c.access_surface_id
     AND s.project_id = c.project_id
     AND s.org_id = c.org_id
    LEFT JOIN public.project_workspace_bindings b
      ON b.id = c.workspace_binding_id
    WHERE s.id IS NULL
       OR (c.status = 'active' AND s.status <> 'active')
       OR (
           c.workspace_binding_id IS NOT NULL
           AND (
               b.id IS NULL
               OR b.project_id IS DISTINCT FROM c.project_id
               OR b.org_id IS DISTINCT FROM c.org_id
               OR b.scope_id IS DISTINCT FROM s.scope_id
               OR (c.status = 'active' AND b.status <> 'active')
           )
       );
    IF invalid_count > 0 THEN
        RAISE EXCEPTION
          'repository target cutover blocked: % invalid credential target chains',
          invalid_count;
    END IF;

    SELECT count(*) INTO invalid_count
    FROM public.connections c
    LEFT JOIN public.repo_scopes rs
      ON rs.id = c.scope_id AND rs.project_id = c.project_id
    WHERE c.scope_id IS NOT NULL AND rs.id IS NULL;
    IF invalid_count > 0 THEN
        RAISE EXCEPTION
          'repository target cutover blocked: % invalid Integration Scope targets',
          invalid_count;
    END IF;

    SELECT count(*) INTO invalid_count
    FROM public.scope_sandbox_sessions s
    LEFT JOIN public.repo_scopes rs
      ON rs.id = s.scope_id AND rs.project_id = s.project_id
    WHERE rs.id IS NULL;
    IF invalid_count > 0 THEN
        RAISE EXCEPTION
          'repository target cutover blocked: % invalid Sandbox Scope targets',
          invalid_count;
    END IF;

    SELECT count(*) INTO invalid_count
    FROM (
        SELECT e.project_id, e.scope_id
        FROM public.scope_sync_events e
        LEFT JOIN public.repo_scopes rs
          ON rs.id = e.scope_id AND rs.project_id = e.project_id
        WHERE rs.id IS NULL
        UNION ALL
        SELECT s.project_id, s.scope_id
        FROM public.scope_sync_settings s
        LEFT JOIN public.repo_scopes rs
          ON rs.id = s.scope_id AND rs.project_id = s.project_id
        WHERE rs.id IS NULL
    ) invalid_scope_sync_targets;
    IF invalid_count > 0 THEN
        RAISE EXCEPTION
          'repository target cutover blocked: % invalid Scope Sync targets',
          invalid_count;
    END IF;

    SELECT count(*) INTO invalid_count
    FROM public.project_workspace_bindings b
    LEFT JOIN LATERAL public.resolve_project_role(
        b.project_id, b.bound_user_id
    ) grant_row ON true
    WHERE b.status = 'active'
      AND (
          grant_row.effective_role IS NULL
          OR (b.mode = 'rw' AND grant_row.effective_role = 'viewer')
      );
    IF invalid_count > 0 THEN
        RAISE EXCEPTION
          'repository target cutover blocked: % active Bindings lack current Project capability',
          invalid_count;
    END IF;

    SELECT count(*) INTO invalid_count
    FROM (
        SELECT s.project_id, rs.is_root, s.scope_id, s.kind
        FROM public.access_surfaces s
        JOIN public.repo_scopes rs
          ON rs.id = s.scope_id AND rs.project_id = s.project_id
        WHERE s.kind IN ('git_remote', 'cli', 'filesystem')
        GROUP BY s.project_id, rs.is_root, s.scope_id, s.kind
        HAVING count(*) > 1
    ) duplicates;
    IF invalid_count > 0 THEN
        RAISE EXCEPTION
          'repository target cutover blocked: % duplicate builtin target Surfaces',
          invalid_count;
    END IF;
END;
$$;

-- ---------------------------------------------------------------------------
-- Remove legacy validators/RPCs before changing the row shape.
-- ---------------------------------------------------------------------------

DROP TRIGGER IF EXISTS trg_validate_project_workspace_binding
    ON public.project_workspace_bindings;
DROP TRIGGER IF EXISTS trg_repo_scope_reconcile_workspace_bindings
    ON public.repo_scopes;
DROP FUNCTION IF EXISTS public._repo_scope_reconcile_workspace_bindings();
DROP TRIGGER IF EXISTS trg_validate_access_surface_credential
    ON public.access_surface_credentials;
-- Defensive final-state guard: Scope insertion must never create Access
-- Surfaces as a side effect, even on a database that skipped the historical
-- context-entrypoint cleanup migration.
DROP TRIGGER IF EXISTS trg_create_builtin_connectors ON public.repo_scopes;
DROP FUNCTION IF EXISTS public.create_builtin_connectors_for_scope();

DROP FUNCTION IF EXISTS public.create_project_workspace_binding(
    text, text, text, text, text, uuid, text, text, text, text, text,
    text, text, text, text
);
DROP FUNCTION IF EXISTS public.rotate_project_workspace_binding_credential(
    text, uuid, text, text, text, text, text
);
DROP FUNCTION IF EXISTS public.create_project_workspace_git_binding(
    text, text, text, text, text, uuid, text, text, text, text, text,
    text, text, text, text
);
DROP FUNCTION IF EXISTS public.resolve_git_runtime_credential(text);
DROP FUNCTION IF EXISTS public.unified_authorization_preflight();
DROP TABLE IF EXISTS public.repo_user_permissions;

ALTER TABLE public.access_surfaces
    DROP CONSTRAINT IF EXISTS access_surfaces_scope_id_fkey,
    DROP CONSTRAINT IF EXISTS access_surfaces_scope_project_fkey,
    ALTER COLUMN scope_id DROP NOT NULL;

ALTER TABLE public.project_workspace_bindings
    DROP CONSTRAINT IF EXISTS project_workspace_bindings_scope_project_fkey,
    ALTER COLUMN scope_id DROP NOT NULL;

-- Root Surface/Binding identity becomes Project + NULL. IDs, credential hashes,
-- lifecycle, timestamps, and audit history remain unchanged.
UPDATE public.access_surfaces s
SET scope_id = NULL,
    principal_type = CASE
        WHEN s.principal_type = 'scope' THEN 'project'
        ELSE s.principal_type
    END,
    principal_id = CASE
        WHEN s.principal_type = 'scope' THEN s.project_id
        ELSE s.principal_id
    END,
    config = s.config - 'path'
FROM public.repo_scopes rs
WHERE rs.id = s.scope_id
  AND rs.project_id = s.project_id
  AND rs.is_root = true;

UPDATE public.project_workspace_bindings b
SET scope_id = NULL
FROM public.repo_scopes rs
WHERE rs.id = b.scope_id
  AND rs.project_id = b.project_id
  AND rs.is_root = true;

UPDATE public.connections c
SET scope_id = NULL
FROM public.repo_scopes rs
WHERE rs.id = c.scope_id
  AND rs.project_id = c.project_id
  AND rs.is_root = true;

-- These tables are Scope-only APIs in the final model. Project-root sync and
-- sandbox state belong to their Project-level contracts, so legacy rows keyed
-- by the retired root Scope identity are removed instead of re-sentinelized.
DELETE FROM public.scope_sync_events e
USING public.repo_scopes rs
WHERE rs.id = e.scope_id
  AND rs.project_id = e.project_id
  AND rs.is_root = true;

DELETE FROM public.scope_sync_settings s
USING public.repo_scopes rs
WHERE rs.id = s.scope_id
  AND rs.project_id = s.project_id
  AND rs.is_root = true;

DELETE FROM public.scope_sandbox_sessions s
USING public.repo_scopes rs
WHERE rs.id = s.scope_id
  AND rs.project_id = s.project_id
  AND rs.is_root = true;

-- Referencing legacy tables either SET NULL or CASCADE according to their
-- already-declared lifecycle. Canonical Surfaces and Bindings were mapped above.
DELETE FROM public.repo_scopes WHERE is_root = true;

ALTER TABLE public.repo_scopes RENAME TO repository_scopes;

ALTER TABLE public.repository_scopes
    RENAME COLUMN mode TO max_mode;

ALTER TABLE public.repository_scopes
    DROP COLUMN is_root;

ALTER TABLE public.project_workspace_bindings
    DROP COLUMN binding_kind;

-- Agent configuration previously duplicated target identity in config.scope
-- and used its presence as an activation flag. Rewrite every Agent to the
-- canonical resolved-view shape, preserve any existing Bash path as a
-- narrower operational view, and keep activation as an explicit boolean.
WITH agent_views AS (
    SELECT
        s.id,
        s.project_id,
        s.scope_id,
        s.config,
        COALESCE(rs.path, '') AS path_prefix,
        COALESCE(rs.exclude, '[]'::jsonb) AS excludes,
        COALESCE(rs.max_mode, 'rw') AS max_mode
    FROM public.access_surfaces s
    LEFT JOIN public.repository_scopes rs
      ON rs.id = s.scope_id AND rs.project_id = s.project_id
    WHERE s.kind = 'agent'
)
UPDATE public.access_surfaces s
SET config = (v.config - 'scope')
    || jsonb_build_object(
        'repository_view', jsonb_build_object(
            'target', CASE WHEN v.scope_id IS NULL
                THEN jsonb_build_object(
                    'kind', 'project_root', 'project_id', v.project_id
                )
                ELSE jsonb_build_object(
                    'kind', 'scope', 'project_id', v.project_id,
                    'scope_id', v.scope_id
                )
            END,
            'path_prefix', v.path_prefix,
            'excludes', v.excludes,
            'max_mode', v.max_mode
        ),
        'activated', CASE
            WHEN jsonb_typeof(v.config -> 'activated') = 'boolean'
                THEN v.config -> 'activated'
            WHEN v.config ? 'scope' THEN 'true'::jsonb
            ELSE 'false'::jsonb
        END
    )
    || CASE
        WHEN v.config ? 'scope'
             AND jsonb_typeof(v.config -> 'scope') = 'object'
        THEN jsonb_build_object(
            'bash_view', jsonb_build_object(
                'path_prefix', COALESCE(v.config #>> '{scope,path}', v.path_prefix),
                'excludes', COALESCE(v.config #> '{scope,exclude}', v.excludes),
                'max_mode', COALESCE(v.config #>> '{scope,mode}', v.max_mode)
            )
        )
        ELSE '{}'::jsonb
    END
FROM agent_views v
WHERE s.id = v.id;

ALTER TABLE public.repository_scopes
    DROP CONSTRAINT IF EXISTS repo_scopes_path_canonical;
ALTER TABLE public.repository_scopes
    ADD CONSTRAINT repository_scopes_path_canonical CHECK (
        path <> ''
        AND path NOT LIKE '/%'
        AND path NOT LIKE '%/'
        AND path NOT LIKE '%//%'
    );

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.repository_scopes'::regclass
          AND conname = 'repo_scopes_pkey'
    ) THEN
        ALTER TABLE public.repository_scopes
            RENAME CONSTRAINT repo_scopes_pkey TO repository_scopes_pkey;
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.repository_scopes'::regclass
          AND conname = 'repo_scopes_project_id_path_key'
    ) THEN
        ALTER TABLE public.repository_scopes
            RENAME CONSTRAINT repo_scopes_project_id_path_key
            TO repository_scopes_project_id_path_key;
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.repository_scopes'::regclass
          AND conname = 'repo_scopes_project_id_fkey'
    ) THEN
        ALTER TABLE public.repository_scopes
            RENAME CONSTRAINT repo_scopes_project_id_fkey
            TO repository_scopes_project_id_fkey;
    END IF;
END;
$$;

DROP INDEX IF EXISTS public.idx_repo_scopes_one_root_per_project;
DROP INDEX IF EXISTS public.idx_repo_scopes_project;
DROP INDEX IF EXISTS public.idx_repo_scopes_access_key_active;
DROP INDEX IF EXISTS public.uq_repo_scopes_id_project;
CREATE UNIQUE INDEX uq_repository_scopes_id_project
    ON public.repository_scopes(id, project_id);
CREATE INDEX idx_repository_scopes_project_path
    ON public.repository_scopes(project_id, path);

DROP TRIGGER IF EXISTS trg_repo_scopes_updated_at
    ON public.repository_scopes;
DROP FUNCTION IF EXISTS public._repo_scopes_bump_updated_at();
CREATE FUNCTION public._repository_scopes_bump_updated_at()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;
CREATE TRIGGER trg_repository_scopes_updated_at
    BEFORE UPDATE ON public.repository_scopes
    FOR EACH ROW EXECUTE FUNCTION public._repository_scopes_bump_updated_at();

DROP POLICY IF EXISTS repo_scopes_service_role_all
    ON public.repository_scopes;
CREATE POLICY repository_scopes_service_role_all
    ON public.repository_scopes
    FOR ALL TO service_role USING (true) WITH CHECK (true);

-- Exact Project/optional-Scope geometry. MATCH SIMPLE is intentional: NULL
-- means Project root; non-NULL must match a Scope from the same Project.
ALTER TABLE public.access_surfaces
    ADD CONSTRAINT access_surfaces_scope_project_fkey
      FOREIGN KEY (scope_id, project_id)
      REFERENCES public.repository_scopes(id, project_id) ON DELETE CASCADE;

ALTER TABLE public.project_workspace_bindings
    ADD CONSTRAINT project_workspace_bindings_scope_project_fkey
      FOREIGN KEY (scope_id, project_id)
      REFERENCES public.repository_scopes(id, project_id) ON DELETE CASCADE;

ALTER TABLE public.connections
    DROP CONSTRAINT IF EXISTS connections_scope_id_fkey,
    DROP CONSTRAINT IF EXISTS connections_scope_project_fkey;
ALTER TABLE public.connections
    ADD CONSTRAINT connections_scope_project_fkey
      FOREIGN KEY (scope_id, project_id)
      REFERENCES public.repository_scopes(id, project_id)
      ON DELETE SET NULL (scope_id);

ALTER TABLE public.scope_sandbox_sessions
    DROP CONSTRAINT IF EXISTS scope_sandbox_sessions_scope_id_fkey,
    DROP CONSTRAINT IF EXISTS scope_sandbox_sessions_scope_project_fkey;
ALTER TABLE public.scope_sandbox_sessions
    ADD CONSTRAINT scope_sandbox_sessions_scope_project_fkey
      FOREIGN KEY (scope_id, project_id)
      REFERENCES public.repository_scopes(id, project_id) ON DELETE CASCADE;

ALTER TABLE public.scope_sync_events
    DROP CONSTRAINT IF EXISTS scope_sync_events_scope_project_fkey;
ALTER TABLE public.scope_sync_events
    ADD CONSTRAINT scope_sync_events_scope_project_fkey
      FOREIGN KEY (scope_id, project_id)
      REFERENCES public.repository_scopes(id, project_id) ON DELETE CASCADE;

ALTER TABLE public.scope_sync_settings
    DROP CONSTRAINT IF EXISTS scope_sync_settings_scope_project_fkey;
ALTER TABLE public.scope_sync_settings
    ADD CONSTRAINT scope_sync_settings_scope_project_fkey
      FOREIGN KEY (scope_id, project_id)
      REFERENCES public.repository_scopes(id, project_id) ON DELETE CASCADE;

DROP INDEX IF EXISTS public.idx_access_surfaces_builtin_one_per_scope;
CREATE UNIQUE INDEX uq_access_surfaces_builtin_target_kind
    ON public.access_surfaces(project_id, scope_id, kind) NULLS NOT DISTINCT
    WHERE kind IN ('git_remote', 'cli', 'filesystem');
CREATE INDEX IF NOT EXISTS idx_access_surfaces_target
    ON public.access_surfaces(project_id, scope_id, status, kind);
CREATE INDEX IF NOT EXISTS idx_project_workspace_bindings_target
    ON public.project_workspace_bindings(project_id, scope_id, status);

-- ---------------------------------------------------------------------------
-- Final validators and atomic machine credential contract.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public._validate_project_workspace_binding()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    scope_max_mode text := 'rw';
BEGIN
    IF NEW.scope_id IS NOT NULL THEN
        SELECT max_mode INTO scope_max_mode
        FROM public.repository_scopes
        WHERE id = NEW.scope_id AND project_id = NEW.project_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'workspace binding Scope/Project mismatch';
        END IF;
    END IF;
    IF NEW.mode = 'rw' AND scope_max_mode <> 'rw' THEN
        RAISE EXCEPTION 'binding mode cannot exceed target mode';
    END IF;
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_validate_project_workspace_binding
    BEFORE INSERT OR UPDATE ON public.project_workspace_bindings
    FOR EACH ROW EXECUTE FUNCTION public._validate_project_workspace_binding();

CREATE OR REPLACE FUNCTION public._validate_access_surface_credential()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    selected_kind text;
    selected_scope_id text;
    selected_target_max_mode text;
    selected_surface_mode text;
    selected_surface_status text;
    selected_binding public.project_workspace_bindings%ROWTYPE;
BEGIN
    SELECT
        s.kind,
        s.scope_id,
        CASE WHEN s.scope_id IS NULL THEN 'rw' ELSE rs.max_mode END,
        COALESCE(s.config ->> 'mode', 'rw'),
        s.status
      INTO
        selected_kind,
        selected_scope_id,
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
            WHEN NEW.workspace_binding_id IS NOT NULL THEN 'binding'
            WHEN NEW.expires_at IS NOT NULL THEN 'session'
            ELSE 'shared'
        END;
    END IF;
    IF (NEW.workspace_binding_id IS NOT NULL)
       IS DISTINCT FROM (NEW.credential_lifecycle = 'binding') THEN
        RAISE EXCEPTION 'credential lifecycle/Workspace Binding mismatch';
    END IF;
    IF NEW.credential_lifecycle = 'session' AND NEW.expires_at IS NULL THEN
        RAISE EXCEPTION 'session credential requires an expiry';
    END IF;
    IF NEW.credential_type IN ('git_http_token', 'ssh_public_key')
       AND selected_kind <> 'git_remote' THEN
        RAISE EXCEPTION 'Git credential requires a git_remote Surface';
    END IF;
    IF NEW.credential_type = 'bearer_token'
       AND selected_kind NOT IN ('cli', 'agent', 'mcp', 'sandbox') THEN
        RAISE EXCEPTION 'bearer credential is invalid for this Surface kind';
    END IF;

    IF NEW.workspace_binding_id IS NOT NULL THEN
        SELECT * INTO selected_binding
        FROM public.project_workspace_bindings
        WHERE id = NEW.workspace_binding_id;
        IF NOT FOUND
           OR selected_binding.project_id IS DISTINCT FROM NEW.project_id
           OR selected_binding.org_id IS DISTINCT FROM NEW.org_id
           OR selected_binding.scope_id IS DISTINCT FROM selected_scope_id THEN
            RAISE EXCEPTION 'credential/Workspace Binding target mismatch';
        END IF;
        IF NEW.status = 'active' AND selected_binding.status <> 'active' THEN
            RAISE EXCEPTION 'active credential requires an active Workspace Binding';
        END IF;
    END IF;

    IF NEW.grant_mode IS NULL THEN
        NEW.grant_mode := COALESCE(selected_binding.mode, selected_target_max_mode, 'r');
    END IF;
    IF NEW.grant_mode = 'rw' AND selected_target_max_mode <> 'rw' THEN
        RAISE EXCEPTION 'credential mode cannot exceed target mode';
    END IF;
    IF NEW.workspace_binding_id IS NOT NULL
       AND NEW.grant_mode = 'rw'
       AND selected_binding.mode <> 'rw' THEN
        RAISE EXCEPTION 'credential mode cannot exceed Workspace Binding mode';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_validate_access_surface_credential
    BEFORE INSERT OR UPDATE OF
      org_id, project_id, access_surface_id, workspace_binding_id,
      credential_type, grant_mode, credential_lifecycle, status, expires_at
    ON public.access_surface_credentials
    FOR EACH ROW EXECUTE FUNCTION public._validate_access_surface_credential();

-- Idempotent, concurrency-safe enable action for the two standard entry
-- points. Scope creation remains independent; callers invoke this function
-- only from an explicit "enable access" or workspace-attach workflow.
CREATE OR REPLACE FUNCTION public.ensure_repository_target_access_surfaces(
    p_project_id text,
    p_scope_id text,
    p_created_by uuid DEFAULT NULL,
    p_git_surface_id text DEFAULT NULL,
    p_cli_surface_id text DEFAULT NULL
)
RETURNS SETOF public.access_surfaces
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    selected_org_id text;
    selected_path text := '';
    selected_max_mode text := 'rw';
BEGIN
    -- Serialize per target so concurrent enable/attach requests converge on
    -- the same Surfaces without treating a uniqueness race as an error.
    PERFORM pg_advisory_xact_lock(
        hashtextextended(p_project_id || E'\n' || COALESCE(p_scope_id, ''), 0)
    );

    SELECT org_id INTO selected_org_id
    FROM public.projects
    WHERE id = p_project_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Repository target Project is invalid';
    END IF;

    IF p_scope_id IS NOT NULL THEN
        SELECT path, max_mode INTO selected_path, selected_max_mode
        FROM public.repository_scopes
        WHERE id = p_scope_id AND project_id = p_project_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'Repository Scope target is invalid';
        END IF;
    END IF;

    INSERT INTO public.access_surfaces (
        id, org_id, project_id, scope_id, kind, name, status,
        principal_type, principal_id, config, created_by
    ) VALUES (
        COALESCE(p_git_surface_id, gen_random_uuid()::text),
        selected_org_id, p_project_id, p_scope_id, 'git_remote', 'Git Remote',
        'active', CASE WHEN p_scope_id IS NULL THEN 'project' ELSE 'scope' END,
        COALESCE(p_scope_id, p_project_id),
        jsonb_build_object(
            'mode', selected_max_mode,
            'direction', 'bidirectional'
        ) || CASE WHEN p_scope_id IS NULL THEN '{}'::jsonb
                  ELSE jsonb_build_object('path', selected_path) END,
        p_created_by
    ) ON CONFLICT DO NOTHING;

    INSERT INTO public.access_surfaces (
        id, org_id, project_id, scope_id, kind, name, status,
        principal_type, principal_id, config, created_by
    ) VALUES (
        COALESCE(p_cli_surface_id, gen_random_uuid()::text),
        selected_org_id, p_project_id, p_scope_id, 'cli', 'FS CLI',
        'active', CASE WHEN p_scope_id IS NULL THEN 'project' ELSE 'scope' END,
        COALESCE(p_scope_id, p_project_id),
        jsonb_build_object(
            'mode', selected_max_mode,
            'direction', 'bidirectional'
        ) || CASE WHEN p_scope_id IS NULL THEN '{}'::jsonb
                  ELSE jsonb_build_object('path', selected_path) END,
        p_created_by
    ) ON CONFLICT DO NOTHING;

    RETURN QUERY
    SELECT s.*
    FROM public.access_surfaces s
    WHERE s.project_id = p_project_id
      AND s.scope_id IS NOT DISTINCT FROM p_scope_id
      AND s.kind IN ('git_remote', 'cli')
    ORDER BY s.kind;
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

CREATE FUNCTION public.create_project_workspace_git_binding(
    p_binding_id text,
    p_org_id text,
    p_project_id text,
    p_scope_id text,
    p_workspace_instance_id text,
    p_bound_user_id uuid,
    p_cloud_origin text,
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
    target_max_mode text := 'rw';
    selected_surface_id text;
    created_binding public.project_workspace_bindings%ROWTYPE;
BEGIN
    SELECT r.org_id, r.effective_role
      INTO resolved_org_id, effective_role
    FROM public.resolve_project_role(p_project_id, p_bound_user_id) r;
    IF NOT FOUND OR resolved_org_id IS DISTINCT FROM p_org_id THEN
        RAISE EXCEPTION 'Project binding authorization denied' USING ERRCODE = '42501';
    END IF;
    IF effective_role IS NULL OR (p_mode = 'rw' AND effective_role = 'viewer') THEN
        RAISE EXCEPTION 'Project binding capability denied' USING ERRCODE = '42501';
    END IF;
    IF p_scope_id IS NOT NULL THEN
        SELECT max_mode INTO target_max_mode
        FROM public.repository_scopes
        WHERE id = p_scope_id AND project_id = p_project_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'Workspace Binding target is invalid';
        END IF;
    END IF;
    IF p_mode = 'rw' AND target_max_mode <> 'rw' THEN
        RAISE EXCEPTION 'Workspace Binding mode exceeds target mode';
    END IF;
    PERFORM public.ensure_repository_target_access_surfaces(
        p_project_id,
        p_scope_id,
        p_bound_user_id,
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
    LIMIT 1;
    IF selected_surface_id IS NULL THEN
        RAISE EXCEPTION 'Workspace Binding Git Surface is invalid';
    END IF;

    INSERT INTO public.project_workspace_bindings (
        id, org_id, project_id, scope_id, workspace_instance_id,
        bound_user_id, cloud_origin, mode, created_by
    ) VALUES (
        p_binding_id, p_org_id, p_project_id, p_scope_id,
        p_workspace_instance_id, p_bound_user_id,
        lower(trim(trailing '/' FROM p_cloud_origin)), p_mode, p_bound_user_id
    ) RETURNING * INTO created_binding;

    INSERT INTO public.access_surface_credentials (
        id, org_id, project_id, access_surface_id, workspace_binding_id,
        credential_type, grant_mode, credential_lifecycle,
        key_prefix, key_last4, key_hash, hash_alg, status, created_by
    ) VALUES (
        p_credential_id, p_org_id, p_project_id, selected_surface_id,
        p_binding_id, 'git_http_token', p_mode, 'binding', p_key_prefix,
        p_key_last4, p_key_hash, p_hash_alg, 'active', p_bound_user_id
    );

    INSERT INTO public.audit_logs (
        action, path, project_id, operator_type, operator_id, status, metadata
    ) VALUES (
        'workspace_binding.create', '', p_project_id, 'user',
        p_bound_user_id::text, 'success',
        jsonb_build_object(
            'binding_id', p_binding_id,
            'target', CASE WHEN p_scope_id IS NULL
                THEN jsonb_build_object('kind', 'project_root', 'project_id', p_project_id)
                ELSE jsonb_build_object(
                    'kind', 'scope', 'project_id', p_project_id, 'scope_id', p_scope_id
                )
            END,
            'mode', p_mode,
            'credential_type', 'git_http_token'
        )
    );
    RETURN NEXT created_binding;
END;
$$;

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
    IF NOT FOUND THEN RETURN false; END IF;

    SELECT r.effective_role INTO effective_role
    FROM public.resolve_project_role(selected_binding.project_id, p_bound_user_id) r;
    IF effective_role IS NULL
       OR (selected_binding.mode = 'rw' AND effective_role = 'viewer') THEN
        RAISE EXCEPTION 'Workspace credential capability denied' USING ERRCODE = '42501';
    END IF;

    SELECT s.id INTO selected_surface_id
    FROM public.access_surfaces s
    LEFT JOIN public.repository_scopes rs
      ON rs.id = selected_binding.scope_id
     AND rs.project_id = selected_binding.project_id
    WHERE s.project_id = selected_binding.project_id
      AND s.org_id = selected_binding.org_id
      AND s.scope_id IS NOT DISTINCT FROM selected_binding.scope_id
      AND s.kind = 'git_remote'
      AND s.status = 'active'
      AND (s.scope_id IS NULL OR rs.id IS NOT NULL)
      AND (selected_binding.mode = 'r' OR s.scope_id IS NULL OR rs.max_mode = 'rw')
    ORDER BY s.created_at ASC
    LIMIT 1;
    IF selected_surface_id IS NULL THEN RETURN false; END IF;

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
    RETURN true;
END;
$$;

CREATE OR REPLACE FUNCTION public._repository_scope_reconcile_workspace_bindings()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
    IF OLD.max_mode = 'rw' AND NEW.max_mode = 'r' THEN
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

CREATE TRIGGER trg_repository_scope_reconcile_workspace_bindings
    BEFORE UPDATE OF max_mode ON public.repository_scopes
    FOR EACH ROW EXECUTE FUNCTION public._repository_scope_reconcile_workspace_bindings();

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
    c.workspace_binding_id,
    b.bound_user_id,
    CASE
        WHEN c.grant_mode <> 'rw'
          OR COALESCE(rs.max_mode, 'rw') <> 'rw'
          OR COALESCE(s.config ->> 'mode', 'rw') <> 'rw'
          OR COALESCE(b.mode, 'rw') <> 'rw'
          OR pr.effective_role = 'viewer'
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
LEFT JOIN public.project_workspace_bindings b
  ON b.id = c.workspace_binding_id
LEFT JOIN LATERAL public.resolve_project_role(c.project_id, b.bound_user_id) pr
  ON c.workspace_binding_id IS NOT NULL
WHERE c.key_hash = p_key_hash
  AND c.credential_type = 'git_http_token'
  AND c.status = 'active'
  AND (c.expires_at IS NULL OR c.expires_at > now())
  AND (s.scope_id IS NULL OR rs.id IS NOT NULL)
  AND (
      c.workspace_binding_id IS NULL
      OR (
          b.id IS NOT NULL
          AND b.status = 'active'
          AND b.project_id = c.project_id
          AND b.org_id = c.org_id
          AND b.scope_id IS NOT DISTINCT FROM s.scope_id
          AND pr.effective_role IS NOT NULL
          AND (b.mode = 'r' OR pr.effective_role <> 'viewer')
      )
  )
LIMIT 1;
$$;

-- Locked-down execution boundaries for the rewritten RPCs.
REVOKE ALL ON FUNCTION public.create_project_workspace_git_binding(
    text, text, text, text, text, uuid, text, text, text, text, text,
    text, text, text
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.create_project_workspace_git_binding(
    text, text, text, text, text, uuid, text, text, text, text, text,
    text, text, text
) TO service_role;
REVOKE ALL ON FUNCTION public.ensure_repository_target_access_surfaces(
    text, text, uuid, text, text
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.ensure_repository_target_access_surfaces(
    text, text, uuid, text, text
) TO service_role;
REVOKE ALL ON FUNCTION public.rotate_access_surface_git_http_token(
    text, text, text, text, text, text, text, text, uuid, timestamptz
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.rotate_access_surface_git_http_token(
    text, text, text, text, text, text, text, text, uuid, timestamptz
) TO service_role;
REVOKE ALL ON FUNCTION public.rotate_project_workspace_binding_git_credential(
    text, uuid, text, text, text, text, text
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.rotate_project_workspace_binding_git_credential(
    text, uuid, text, text, text, text, text
) TO service_role;
REVOKE ALL ON FUNCTION public.resolve_git_runtime_credential(text)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.resolve_git_runtime_credential(text)
    TO service_role;

-- Replace the operational authorization report so it remains executable after
-- the physical table/column cutover and treats NULL Scope associations as the
-- canonical Project-root shape.
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
    'orphan_workspace_bindings', (
        SELECT count(*) FROM public.project_workspace_bindings b
        LEFT JOIN public.projects p
          ON p.id = b.project_id AND p.org_id = b.org_id
        LEFT JOIN public.repository_scopes rs
          ON rs.id = b.scope_id AND rs.project_id = b.project_id
        WHERE p.id IS NULL OR (b.scope_id IS NOT NULL AND rs.id IS NULL)
    ),
    'orphan_access_credentials', (
        SELECT count(*) FROM public.access_surface_credentials c
        LEFT JOIN public.access_surfaces s
          ON s.id = c.access_surface_id
         AND s.project_id = c.project_id
         AND s.org_id = c.org_id
        LEFT JOIN public.project_workspace_bindings b
          ON b.id = c.workspace_binding_id
        WHERE s.id IS NULL
           OR (c.workspace_binding_id IS NOT NULL AND (
               b.id IS NULL
               OR b.project_id IS DISTINCT FROM s.project_id
               OR b.org_id IS DISTINCT FROM s.org_id
               OR b.scope_id IS DISTINCT FROM s.scope_id
           ))
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

REVOKE ALL ON FUNCTION public.unified_authorization_preflight()
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.unified_authorization_preflight()
    TO service_role;

-- Final machine-readable postflight. All counts must be zero.
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
    'orphan_binding_targets', (
        SELECT count(*)
        FROM public.project_workspace_bindings b
        LEFT JOIN public.projects p
          ON p.id = b.project_id AND p.org_id = b.org_id
        LEFT JOIN public.repository_scopes rs
          ON rs.id = b.scope_id AND rs.project_id = b.project_id
        WHERE p.id IS NULL OR (b.scope_id IS NOT NULL AND rs.id IS NULL)
    ),
    'credential_target_mismatches', (
        SELECT count(*)
        FROM public.access_surface_credentials c
        LEFT JOIN public.access_surfaces s
          ON s.id = c.access_surface_id
         AND s.project_id = c.project_id
         AND s.org_id = c.org_id
        LEFT JOIN public.project_workspace_bindings b
          ON b.id = c.workspace_binding_id
        WHERE s.id IS NULL
           OR (c.workspace_binding_id IS NOT NULL AND (
               b.id IS NULL
               OR b.scope_id IS DISTINCT FROM s.scope_id
               OR b.project_id IS DISTINCT FROM s.project_id
               OR b.org_id IS DISTINCT FROM s.org_id
           ))
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

REVOKE ALL ON FUNCTION public.repository_target_integrity_report()
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.repository_target_integrity_report()
    TO service_role;

DO $$
DECLARE
    report jsonb;
BEGIN
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
