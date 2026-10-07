-- ISSUE-049 cleanup; promote in a SEPARATE release after process/queue drain.
-- requires-data-migration: 20260927_entrypoint_storage_backfill
-- data-migration-checksum: e8f8ffef8022598a03d836422ab59e74e2c0a6427a4334f362fe820c504f1f7c
-- Do not add to migrations in the Expand/Cutover release.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '5min';
LOCK TABLE public.access_tools, public.github_sync_bindings, public.github_sync_log,
  public.uploads, public.search_index_tasks IN ACCESS EXCLUSIVE MODE;
DO $$
BEGIN
  IF (EXISTS (SELECT 1 FROM public.access_tools)
      OR EXISTS (SELECT 1 FROM public.github_sync_bindings)
      OR EXISTS (SELECT 1 FROM public.github_sync_log)
      OR EXISTS (SELECT 1 FROM public.uploads WHERE type = 'search_index')
      OR EXISTS (SELECT 1 FROM public.search_index_tasks))
     AND NOT EXISTS (
       SELECT 1 FROM public.migration_log WHERE name = '20260927_entrypoint_storage_backfill'
       AND coalesce((summary->>'verified')::boolean, false)
       AND summary->>'artifact_checksum' = 'e8f8ffef8022598a03d836422ab59e74e2c0a6427a4334f362fe820c504f1f7c'
     ) THEN
    RAISE EXCEPTION 'DATA_MIGRATION_REQUIRED:20260927_entrypoint_storage_backfill';
  END IF;
END $$;
-- Also valid after Contract: old columns and mirror rows are intentionally gone.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM public.access_tools WHERE access_surface_id IS NULL)
     OR EXISTS (SELECT 1 FROM public.github_sync_log WHERE binding_id IS NULL) THEN
    RAISE EXCEPTION 'entrypoint identifiers have not been backfilled';
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_schema='public' AND table_name='access_tools' AND column_name='access_point_id') THEN
    IF EXISTS (SELECT 1 FROM public.access_tools WHERE access_surface_id IS DISTINCT FROM access_point_id)
       OR EXISTS (SELECT 1 FROM public.github_sync_log WHERE binding_id IS DISTINCT FROM integration_id) THEN
      RAISE EXCEPTION 'entrypoint identifier mismatch';
    END IF;
    IF EXISTS (
      SELECT 1 FROM public.uploads u FULL JOIN public.search_index_tasks s
        ON u.id = s.id AND u.type = 'search_index'
      WHERE (u.type = 'search_index' OR s.id IS NOT NULL)
        AND (u.id IS NULL OR s.id IS NULL OR to_jsonb(u) - 'type' IS DISTINCT FROM to_jsonb(s))
    ) THEN
      RAISE EXCEPTION 'search index task mismatch';
    END IF;
  ELSE
    IF EXISTS (SELECT 1 FROM public.uploads WHERE type = 'search_index') THEN
      RAISE EXCEPTION 'search index tasks remain in uploads after Contract';
    END IF;
  END IF;
END $$;

-- Replace SQL bodies before removing their legacy column references.
CREATE OR REPLACE FUNCTION "public"."_validate_access_tool_project_boundary"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
DECLARE
    surface_project_id text;
    surface_org_id text;
    tool_project_id text;
    tool_org_id text;
BEGIN
    SELECT project_id, org_id INTO surface_project_id, surface_org_id
    FROM public.access_surfaces WHERE id = NEW.access_surface_id;
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

CREATE OR REPLACE FUNCTION "public"."abandon_project_initialization"("p_project_id" "text", "p_operation_key" "text", "p_actor_user_id" "uuid", "p_quiescence_seconds" integer DEFAULT 3600, "p_worker_id" "text" DEFAULT NULL::"text") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
DECLARE
    create_operation public.project_create_operations%ROWTYPE;
    deletion_job public.project_deletion_jobs%ROWTYPE;
    project_row public.projects%ROWTYPE;
    empty_tree constant text := '4b825dc642cb6eb9a060e54bf8d69288fbee4904';
BEGIN
    PERFORM pg_advisory_xact_lock(
        hashtextextended('project-create:' || p_actor_user_id::text || ':' || p_operation_key, 0)
    );
    SELECT * INTO create_operation
    FROM public.project_create_operations
    WHERE actor_user_id = p_actor_user_id AND operation_key = p_operation_key
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('outcome', 'not_found');
    END IF;
    IF create_operation.project_id IS DISTINCT FROM p_project_id THEN
        RETURN jsonb_build_object('outcome', 'conflict');
    END IF;
    IF create_operation.publication_mode <> 'empty' THEN
        RETURN jsonb_build_object('outcome', 'not_abandonable');
    END IF;

    SELECT * INTO deletion_job
    FROM public.project_deletion_jobs
    WHERE project_id = p_project_id;
    IF create_operation.status = 'deleted' THEN
        IF deletion_job.id IS NULL THEN
            RETURN jsonb_build_object('outcome', 'gone');
        END IF;
        RETURN jsonb_build_object(
            'outcome', 'replayed',
            'job', to_jsonb(deletion_job)
        );
    END IF;
    IF p_worker_id IS NOT NULL
       AND (
           create_operation.status <> 'initializing'
           OR create_operation.initialization_claimed_by IS DISTINCT FROM p_worker_id
       ) THEN
        RETURN jsonb_build_object('outcome', 'claim_lost');
    END IF;

    SELECT * INTO project_row
    FROM public.projects
    WHERE id = p_project_id
    FOR UPDATE;

    IF project_row.id IS NOT NULL THEN
        IF project_row.created_by IS DISTINCT FROM p_actor_user_id
           OR (
               p_worker_id IS NULL
               AND NOT EXISTS (
                   SELECT 1 FROM public.org_members om
                   WHERE om.org_id = project_row.org_id
                     AND om.user_id = p_actor_user_id
               )
           ) THEN
            RETURN jsonb_build_object('outcome', 'forbidden');
        END IF;
        IF (
               to_jsonb(project_row)
                   - ARRAY[
                       'version_root_hash', 'mut_root_hash',
                       'updated_at', 'lifecycle_status'
                   ]::text[]
             ) IS DISTINCT FROM (
               create_operation.project_snapshot
                   - ARRAY[
                       'version_root_hash', 'mut_root_hash',
                       'updated_at', 'lifecycle_status'
                   ]::text[]
             )
           OR NOT (
               (
                   COALESCE(project_row.version_root_hash, '') = ''
                   AND COALESCE(project_row.mut_root_hash, '') = ''
               )
               OR (
                   project_row.version_root_hash = empty_tree
                   AND project_row.mut_root_hash = empty_tree
               )
           )
           OR EXISTS (
               SELECT 1 FROM public.version_scope_state state
               WHERE state.project_id = p_project_id
                 AND (
                     state.scope_path <> ''
                     OR COALESCE(state.scope_hash, '') <> ''
                     OR COALESCE(state.head_commit_id, '') <> ''
                 )
           )
           OR EXISTS (
               SELECT 1 FROM public.version_transactions tx
               WHERE tx.project_id = p_project_id
                 AND tx.status = 'committed'
           )
           OR (SELECT count(*) FROM public.project_members member
               WHERE member.project_id = p_project_id) <> 1
           OR NOT EXISTS (
               SELECT 1 FROM public.project_members member
               WHERE member.project_id = p_project_id
                 AND member.org_id = project_row.org_id
                 AND member.user_id = p_actor_user_id
                 AND member.role = 'admin'
                 AND member.granted_by = p_actor_user_id
           )
           -- The only credential operation allowed during bootstrap is the
           -- user Git credential issued with the same publish operation key.
           OR EXISTS (
               SELECT 1
               FROM public.git_credential_issue_operations op
               WHERE op.project_id = p_project_id
                 AND (
                     op.actor_user_id IS DISTINCT FROM p_actor_user_id
                     OR op.operation_key IS DISTINCT FROM p_operation_key
                     OR op.org_id IS DISTINCT FROM project_row.org_id
                     OR op.status IS DISTINCT FROM 'active'
                     OR NOT EXISTS (
                         SELECT 1
                         FROM public.access_surface_credentials credential
                         JOIN public.access_surfaces surface
                           ON surface.id = credential.access_surface_id
                         WHERE credential.id = op.credential_id
                           AND credential.project_id = p_project_id
                           AND credential.org_id = project_row.org_id
                           AND credential.user_id = p_actor_user_id
                           AND credential.created_by = p_actor_user_id
                           AND credential.credential_type = 'git_http_token'
                           AND credential.credential_lifecycle = 'user'
                           AND credential.status = 'active'
                           AND credential.key_hash = op.credential_hash
                           AND surface.project_id = p_project_id
                           AND surface.org_id = project_row.org_id
                           AND surface.scope_id IS NULL
                           AND surface.kind = 'git_remote'
                     )
                 )
           )
           OR EXISTS (
               SELECT 1
               FROM public.access_surface_credentials credential
               WHERE credential.project_id = p_project_id
                 AND NOT EXISTS (
                     SELECT 1
                     FROM public.git_credential_issue_operations op
                     WHERE op.actor_user_id = p_actor_user_id
                       AND op.operation_key = p_operation_key
                       AND op.project_id = p_project_id
                       AND op.org_id = project_row.org_id
                       AND op.credential_id = credential.id
                       AND op.credential_hash = credential.key_hash
                       AND op.status = 'active'
                 )
           )
           -- Bootstrap may materialize only the standard root Git/CLI
           -- Surfaces.  Any Scope, Agent, policy, tool, or modified Surface
           -- means this Project has become a real user resource.
           OR (SELECT count(*) FROM public.access_surfaces surface
               WHERE surface.project_id = p_project_id) NOT IN (0, 2)
           OR EXISTS (
               SELECT 1 FROM public.access_surfaces surface
               WHERE surface.project_id = p_project_id
                 AND (
                     surface.org_id IS DISTINCT FROM project_row.org_id
                     OR surface.scope_id IS NOT NULL
                     OR surface.kind NOT IN ('git_remote', 'cli')
                     OR surface.name IS DISTINCT FROM CASE surface.kind
                         WHEN 'git_remote' THEN 'Git Remote'
                         WHEN 'cli' THEN 'FS CLI'
                     END
                     OR surface.status IS DISTINCT FROM 'active'
                     OR surface.principal_type IS DISTINCT FROM 'project'
                     OR surface.principal_id IS DISTINCT FROM p_project_id
                     OR surface.config IS DISTINCT FROM jsonb_build_object(
                         'mode', 'rw', 'direction', 'bidirectional'
                     )
                     OR surface.created_by IS DISTINCT FROM p_actor_user_id
                 )
           )
           OR EXISTS (
               SELECT 1
               FROM public.access_surface_policies policy
               JOIN public.access_surfaces surface
                 ON surface.id = policy.access_surface_id
               WHERE surface.project_id = p_project_id
           )
           OR EXISTS (
               SELECT 1
               FROM public.access_tools tool_binding
               JOIN public.access_surfaces surface
                 ON surface.id = tool_binding.access_surface_id
               WHERE surface.project_id = p_project_id
           )
           OR public._project_initialization_has_cascade_dependents(p_project_id)
        THEN
            RETURN jsonb_build_object('outcome', 'not_abandonable');
        END IF;
    END IF;

    UPDATE public.access_surface_credentials c
    SET status = 'revoked', revoked_at = COALESCE(c.revoked_at, now())
    FROM public.git_credential_issue_operations op
    WHERE op.actor_user_id = p_actor_user_id
      AND op.operation_key = p_operation_key
      AND op.project_id = p_project_id
      AND c.id = op.credential_id
      AND c.status = 'active';
    UPDATE public.git_credential_issue_operations
    SET status = 'revoked', revoked_at = COALESCE(revoked_at, now())
    WHERE actor_user_id = p_actor_user_id
      AND operation_key = p_operation_key
      AND project_id = p_project_id
      AND status = 'active';

    INSERT INTO public.project_deletion_jobs (
        project_id, org_id, requested_by, source, source_operation_key,
        object_prefixes, quiescence_seconds, available_at
    ) VALUES (
        p_project_id, create_operation.org_id, p_actor_user_id,
        'initialization_abandon', p_operation_key,
        jsonb_build_array(
            'version/' || p_project_id || '/',
            'mut/' || p_project_id || '/',
            'projects/' || p_project_id || '/'
        ),
        GREATEST(COALESCE(p_quiescence_seconds, 3600), 1800),
        now()
    )
    ON CONFLICT (project_id) DO UPDATE
      SET updated_at = now()
    RETURNING * INTO deletion_job;

    UPDATE public.project_create_operations
    SET status = 'deleted', deleted_at = COALESCE(deleted_at, now())
    WHERE actor_user_id = p_actor_user_id AND operation_key = p_operation_key;
    UPDATE public.projects
    SET lifecycle_status = 'deleting', updated_at = now()
    WHERE id = p_project_id AND lifecycle_status = 'initializing';

    RETURN jsonb_build_object(
        'outcome', 'accepted',
        'job', to_jsonb(deletion_job)
    );
END;
$$;

CREATE OR REPLACE FUNCTION "public"."replace_mcp_surface_policy"("p_surface_id" "text", "p_accesses" "jsonb", "p_tools_policy" "jsonb", "p_bindings" "jsonb") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'public', 'pg_temp'
    AS $$
DECLARE
    v_policy public.access_surface_policies%ROWTYPE;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM public.access_surfaces
        WHERE id = p_surface_id AND kind = 'mcp'
    ) THEN
        RAISE EXCEPTION 'MCP access surface not found';
    END IF;

    INSERT INTO public.access_surface_policies (
        access_surface_id, version, fs_policy, tools_policy,
        shell_policy, network_policy
    ) VALUES (
        p_surface_id,
        1,
        jsonb_build_object('accesses', COALESCE(p_accesses, '[]'::jsonb)),
        COALESCE(p_tools_policy, '{}'::jsonb),
        jsonb_build_object('enabled', false),
        '{}'::jsonb
    )
    ON CONFLICT (access_surface_id) DO UPDATE SET
        version = EXCLUDED.version,
        fs_policy = EXCLUDED.fs_policy,
        tools_policy = EXCLUDED.tools_policy,
        shell_policy = EXCLUDED.shell_policy,
        network_policy = EXCLUDED.network_policy
    RETURNING * INTO v_policy;

    DELETE FROM public.access_tools WHERE access_surface_id = p_surface_id;
    INSERT INTO public.access_tools (
        id, access_surface_id, tool_id, enabled, mcp_exposed
    )
    SELECT
        gen_random_uuid()::text,
        p_surface_id,
        binding ->> 'tool_id',
        COALESCE((binding ->> 'enabled')::boolean, true),
        true
    FROM jsonb_array_elements(COALESCE(p_bindings, '[]'::jsonb)) binding
    JOIN public.tools t ON t.id = binding ->> 'tool_id'
    WHERE NULLIF(binding ->> 'tool_id', '') IS NOT NULL;

    RETURN to_jsonb(v_policy);
END;
$$;

CREATE OR REPLACE FUNCTION "public"."unified_authorization_preflight"() RETURNS "jsonb"
    LANGUAGE "sql" STABLE
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
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
        LEFT JOIN public.access_surfaces s ON s.id = at.access_surface_id
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

ALTER TABLE public.access_tools ALTER COLUMN access_surface_id SET NOT NULL;
ALTER TABLE public.github_sync_log ALTER COLUMN binding_id SET NOT NULL;
DROP TRIGGER a00_sync_access_tool_surface_columns ON public.access_tools;
DROP FUNCTION public._sync_access_tool_surface_columns();
DROP TRIGGER a00_sync_github_binding_columns ON public.github_sync_log;
DROP FUNCTION public._sync_github_binding_columns();
ALTER TABLE public.access_tools DROP COLUMN access_point_id;
ALTER TABLE public.github_sync_log DROP COLUMN integration_id;
DROP VIEW public.github_integrations;

-- Remove mirrors only after exact row equality has been established under lock.
DROP TRIGGER mirror_search_tasks_from_uploads ON public.uploads;
DROP TRIGGER mirror_search_tasks_to_uploads ON public.search_index_tasks;
DROP FUNCTION public._mirror_search_tasks_from_uploads();
DROP FUNCTION public._mirror_search_tasks_to_uploads();
DELETE FROM public.uploads WHERE type = 'search_index';
ALTER TABLE public.uploads DROP CONSTRAINT chk_uploads_type;
ALTER TABLE public.uploads ADD CONSTRAINT chk_uploads_type
  CHECK (type IN ('file_ocr', 'file_postprocess', 'import'));

-- D04: only constraints/indexes/policies owned by the touched domains.
ALTER TABLE public.access_tools RENAME CONSTRAINT agent_tool_pkey TO access_tools_pkey;
ALTER TABLE public.access_tools RENAME CONSTRAINT agent_tool_tool_id_fkey TO access_tools_tool_id_fkey;
ALTER POLICY service_role_all_agent_tool ON public.access_tools RENAME TO access_tools_service_role_all;
-- The legacy table still owns this index name; retain all its history.
ALTER TABLE public.connector_runs RENAME CONSTRAINT sync_runs_pkey TO connector_runs_pkey;
ALTER TABLE public.sync_runs RENAME CONSTRAINT sync_runs_pkey1 TO sync_runs_pkey;
ALTER TABLE public.github_sync_bindings RENAME CONSTRAINT github_integrations_auto_import_needs_webhook TO github_sync_bindings_auto_import_needs_webhook;
ALTER TABLE public.github_sync_bindings RENAME CONSTRAINT github_integrations_default_branch_check TO github_sync_bindings_default_branch_check;
ALTER TABLE public.github_sync_bindings RENAME CONSTRAINT github_integrations_github_repo_name_check TO github_sync_bindings_github_repo_name_check;
ALTER TABLE public.github_sync_bindings RENAME CONSTRAINT github_integrations_github_repo_owner_check TO github_sync_bindings_github_repo_owner_check;
ALTER TABLE public.github_sync_bindings RENAME CONSTRAINT github_integrations_pkey TO github_sync_bindings_pkey;
ALTER TABLE public.github_sync_bindings RENAME CONSTRAINT github_integrations_project_id_key TO github_sync_bindings_project_id_key;
ALTER TABLE public.github_sync_bindings RENAME CONSTRAINT github_integrations_oauth_connection_id_fkey TO github_sync_bindings_oauth_connection_id_fkey;
ALTER TABLE public.github_sync_bindings RENAME CONSTRAINT github_integrations_project_id_fkey TO github_sync_bindings_project_id_fkey;
ALTER INDEX public.idx_github_integrations_oauth_connection RENAME TO idx_github_sync_bindings_oauth_connection;
ALTER INDEX public.idx_github_integrations_repo_coords RENAME TO idx_github_sync_bindings_repo_coords;
ALTER FUNCTION public._github_integrations_bump_updated_at() RENAME TO _github_sync_bindings_bump_updated_at;
ALTER TRIGGER trg_github_integrations_updated_at ON public.github_sync_bindings RENAME TO trg_github_sync_bindings_updated_at;
ALTER POLICY github_integrations_service_role_all ON public.github_sync_bindings RENAME TO github_sync_bindings_service_role_all;
NOTIFY pgrst, 'reload schema';
COMMIT;
