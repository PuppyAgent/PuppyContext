-- Historical Project creation could commit the Project row without its root
-- Scope. Every such Project blocks the repository target preflight
-- (20260715_project_owned_repository_targets_preflight) and is unusable by
-- the application, which assumes one root Scope per Project.
--
-- This repair restores exactly the row the application would have created:
-- path='', exclude='[]', mode='rw', is_root=true. It never touches Projects
-- that already have a root Scope, and it records one audit fact per repaired
-- Project.

DO $$
DECLARE
    blocked bigint;
BEGIN
    -- A non-root Scope on the root path is ambiguous corruption this repair
    -- must not guess about; the (project_id, path) UNIQUE would silently
    -- swallow the repair row. Fail closed before mutating anything.
    SELECT count(*) INTO blocked
    FROM public.repo_scopes
    WHERE NOT is_root AND path = '';
    IF blocked > 0 THEN
        RAISE EXCEPTION
          'root Scope repair blocked: % non-root Scope rows already claim the root path',
          blocked;
    END IF;
END;
$$;

WITH repaired AS (
    INSERT INTO public.repo_scopes (project_id, name, path, exclude, mode, is_root)
    SELECT p.id, 'Root', '', '[]'::jsonb, 'rw', true
    FROM public.projects p
    WHERE NOT EXISTS (
        SELECT 1 FROM public.repo_scopes s
        WHERE s.project_id = p.id AND s.is_root = true
    )
    ORDER BY p.id
    RETURNING id, project_id
)
INSERT INTO public.audit_logs (
    action, path, project_id, operator_type, operator_id, status, metadata
)
SELECT
    'repository_target.root_scope.repair',
    '',
    project_id,
    'system',
    '20260717_repair_missing_root_scopes',
    'success',
    jsonb_build_object('scope_id', id)
FROM repaired;
