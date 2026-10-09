-- Product deletion is an intent transition; physical storage GC is delayed.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

CREATE TRIGGER agent_workspace_project_deleting
AFTER UPDATE OF lifecycle_status ON public.projects
FOR EACH ROW
WHEN (NEW.lifecycle_status = 'deleting' AND OLD.lifecycle_status IS DISTINCT FROM NEW.lifecycle_status)
EXECUTE FUNCTION public.agent_workspace_owner_deleted();

COMMIT;
