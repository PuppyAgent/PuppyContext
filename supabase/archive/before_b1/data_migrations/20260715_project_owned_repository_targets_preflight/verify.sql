DO $$
DECLARE
    invalid_count bigint;
BEGIN
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
          'repository target preflight blocked: % Projects lack exactly one legacy root',
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
          'repository target preflight blocked: % malformed legacy Scope rows',
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
              'repository target preflight blocked: % unresolved legacy Human permission rows',
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
          'repository target preflight blocked: % invalid Access Surface targets',
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
          'repository target preflight blocked: % invalid Workspace Binding targets',
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
          'repository target preflight blocked: % invalid credential target chains',
          invalid_count;
    END IF;

    SELECT count(*) INTO invalid_count
    FROM public.connections c
    LEFT JOIN public.repo_scopes rs
      ON rs.id = c.scope_id AND rs.project_id = c.project_id
    WHERE c.scope_id IS NOT NULL AND rs.id IS NULL;
    IF invalid_count > 0 THEN
        RAISE EXCEPTION
          'repository target preflight blocked: % invalid Integration Scope targets',
          invalid_count;
    END IF;

    SELECT count(*) INTO invalid_count
    FROM public.scope_sandbox_sessions s
    LEFT JOIN public.repo_scopes rs
      ON rs.id = s.scope_id AND rs.project_id = s.project_id
    WHERE rs.id IS NULL;
    IF invalid_count > 0 THEN
        RAISE EXCEPTION
          'repository target preflight blocked: % invalid Sandbox Scope targets',
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
          'repository target preflight blocked: % invalid Scope Sync targets',
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
          'repository target preflight blocked: % active Bindings lack current Project capability',
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
          'repository target preflight blocked: % duplicate standard target Surfaces',
          invalid_count;
    END IF;
END;
$$;
