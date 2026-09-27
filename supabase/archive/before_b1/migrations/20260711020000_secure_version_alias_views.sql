-- ISSUE-019 security correction for the Phase-1 version_* aliases.
--
-- Ordinary PostgreSQL views use the view owner's privileges/RLS policies.
-- The original migration granted authenticated SELECT and incorrectly assumed
-- base-table RLS would run as the caller. These storage aliases are backend
-- implementation details, so browser roles must never access them directly.

BEGIN;

ALTER VIEW IF EXISTS public.version_commits
    SET (security_invoker = true);
ALTER VIEW IF EXISTS public.version_scope_state
    SET (security_invoker = true);
ALTER VIEW IF EXISTS public.version_view_commits
    SET (security_invoker = true);
ALTER VIEW IF EXISTS public.version_outbox
    SET (security_invoker = true);
ALTER VIEW IF EXISTS public.version_conflicts
    SET (security_invoker = true);
ALTER VIEW IF EXISTS public.version_object_locations
    SET (security_invoker = true);

REVOKE ALL ON public.version_commits FROM PUBLIC, anon, authenticated;
REVOKE ALL ON public.version_scope_state FROM PUBLIC, anon, authenticated;
REVOKE ALL ON public.version_view_commits FROM PUBLIC, anon, authenticated;
REVOKE ALL ON public.version_outbox FROM PUBLIC, anon, authenticated;
REVOKE ALL ON public.version_conflicts FROM PUBLIC, anon, authenticated;
REVOKE ALL ON public.version_object_locations FROM PUBLIC, anon, authenticated;

GRANT SELECT, INSERT, UPDATE, DELETE ON public.version_commits TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.version_scope_state TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.version_view_commits TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.version_outbox TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.version_conflicts TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.version_object_locations TO service_role;

NOTIFY pgrst, 'reload schema';

COMMIT;
