-- ===========================================================================
-- Keep Project creator authority internally consistent
-- ===========================================================================
-- A Project creator is a tenant member and an explicit Project Admin.  The
-- atomic creation RPC has published both facts since 20260712010000, but
-- direct membership mutation could still remove or downgrade the creator.
-- This forward migration closes that write path and exposes disjoint,
-- aggregate-only diagnostics for historical reconciliation.

BEGIN;

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '15min';

CREATE OR REPLACE FUNCTION public.project_creator_authorization_preflight()
RETURNS jsonb
LANGUAGE sql
STABLE
SET search_path = pg_catalog, public, pg_temp
AS $$
SELECT jsonb_build_object(
    'creator_missing_profile', (
        SELECT count(*)
        FROM public.projects p
        LEFT JOIN public.profiles pr ON pr.user_id = p.created_by
        WHERE p.created_by IS NOT NULL AND pr.user_id IS NULL
    ),
    'creator_missing_org_membership', (
        SELECT count(*)
        FROM public.projects p
        JOIN public.profiles pr ON pr.user_id = p.created_by
        LEFT JOIN public.org_members om
          ON om.org_id = p.org_id AND om.user_id = p.created_by
        WHERE p.created_by IS NOT NULL AND om.id IS NULL
    ),
    'creator_missing_project_membership', (
        SELECT count(*)
        FROM public.projects p
        JOIN public.org_members om
          ON om.org_id = p.org_id AND om.user_id = p.created_by
        LEFT JOIN public.project_members pm
          ON pm.project_id = p.id AND pm.user_id = p.created_by
        WHERE p.created_by IS NOT NULL AND pm.id IS NULL
    ),
    'creator_non_admin_project_membership', (
        SELECT count(*)
        FROM public.projects p
        JOIN public.org_members om
          ON om.org_id = p.org_id AND om.user_id = p.created_by
        JOIN public.project_members pm
          ON pm.project_id = p.id AND pm.user_id = p.created_by
        WHERE p.created_by IS NOT NULL AND pm.role IS DISTINCT FROM 'admin'
    )
);
$$;

REVOKE ALL ON FUNCTION public.project_creator_authorization_preflight()
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.project_creator_authorization_preflight()
    TO service_role;

-- Reject direct or RPC-driven deletion, reassignment, or downgrade of the
-- creator's explicit Project Admin row.  A future ownership-transfer RPC can
-- change projects.created_by and the two membership facts in one transaction.
CREATE OR REPLACE FUNCTION public._enforce_project_creator_admin_member()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF EXISTS (
            SELECT 1
            FROM public.projects p
            WHERE p.id = OLD.project_id AND p.created_by = OLD.user_id
        ) THEN
            RAISE EXCEPTION 'project creator must retain explicit Project Admin membership'
                USING ERRCODE = '23514';
        END IF;
        RETURN OLD;
    END IF;

    IF TG_OP = 'UPDATE' AND EXISTS (
        SELECT 1
        FROM public.projects p
        WHERE p.id = OLD.project_id
          AND p.created_by = OLD.user_id
          AND (
              NEW.project_id IS DISTINCT FROM OLD.project_id
              OR NEW.user_id IS DISTINCT FROM OLD.user_id
              OR NEW.org_id IS DISTINCT FROM OLD.org_id
              OR NEW.role IS DISTINCT FROM 'admin'
          )
    ) THEN
        RAISE EXCEPTION 'project creator must retain explicit Project Admin membership'
            USING ERRCODE = '23514';
    END IF;

    IF TG_OP IN ('INSERT', 'UPDATE')
       AND NEW.role IS DISTINCT FROM 'admin'
       AND EXISTS (
           SELECT 1
           FROM public.projects p
           WHERE p.id = NEW.project_id AND p.created_by = NEW.user_id
       ) THEN
        RAISE EXCEPTION 'project creator must retain explicit Project Admin membership'
            USING ERRCODE = '23514';
    END IF;

    RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION public._enforce_project_creator_admin_member()
    FROM PUBLIC, anon, authenticated;

DROP TRIGGER IF EXISTS trg_project_members_creator_admin_guard
    ON public.project_members;
CREATE TRIGGER trg_project_members_creator_admin_guard
    BEFORE INSERT OR UPDATE OR DELETE ON public.project_members
    FOR EACH ROW EXECUTE FUNCTION public._enforce_project_creator_admin_member();

-- Projects may be inserted before their creator membership within the same
-- transaction, so validate this side of the cross-table invariant at commit.
CREATE OR REPLACE FUNCTION public._assert_project_creator_admin_at_commit()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    project_ids text[];
BEGIN
    IF TG_OP = 'INSERT' THEN
        project_ids := ARRAY[NEW.id];
    ELSE
        project_ids := ARRAY[OLD.id, NEW.id];
    END IF;

    IF EXISTS (
        SELECT 1
        FROM public.projects p
        LEFT JOIN public.org_members om
          ON om.org_id = p.org_id AND om.user_id = p.created_by
        LEFT JOIN public.project_members pm
          ON pm.project_id = p.id
         AND pm.org_id = p.org_id
         AND pm.user_id = p.created_by
        WHERE p.id = ANY(project_ids)
          AND p.created_by IS NOT NULL
          AND (om.id IS NULL OR pm.role IS DISTINCT FROM 'admin')
    ) THEN
        RAISE EXCEPTION 'project creator must be an organization member and explicit Project Admin'
            USING ERRCODE = '23514';
    END IF;

    RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION public._assert_project_creator_admin_at_commit()
    FROM PUBLIC, anon, authenticated;

DROP TRIGGER IF EXISTS trg_projects_creator_admin_guard ON public.projects;
CREATE CONSTRAINT TRIGGER trg_projects_creator_admin_guard
    AFTER INSERT OR UPDATE OF id, org_id, created_by ON public.projects
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION public._assert_project_creator_admin_at_commit();

COMMIT;
