-- A durable deletion job is the write-admission fence for its Project.
-- Keep that invariant at the shared database boundary so every deletion
-- source transitions the Project before asynchronous drain and purge begin.

CREATE OR REPLACE FUNCTION public._prepare_project_deletion_job_storage()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM public.project_storage_inventory_state inventory
        WHERE inventory.singleton AND inventory.inventory_complete
    ) THEN
        RAISE EXCEPTION 'Project storage inventory is incomplete'
            USING ERRCODE = '55000',
                  HINT = 'Run the resumable Project storage inventory before deletion.';
    END IF;

    UPDATE public.projects
    SET lifecycle_status = 'deleting', updated_at = now()
    WHERE id = NEW.project_id
      AND lifecycle_status IN ('initializing', 'ready');

    -- Capture the authoritative ownership manifest before relational cleanup
    -- can remove any of its source rows.
    NEW.storage_principals := public._project_deletion_storage_principals(
        NEW.project_id, NEW.requested_by
    );
    NEW.object_prefixes := public._project_deletion_object_prefixes(
        NEW.project_id, NEW.storage_principals
    );
    NEW.search_namespace_prefixes := public._project_deletion_search_prefixes(
        NEW.project_id
    );
    NEW.sandbox_resources := public._project_deletion_sandbox_resources(
        NEW.project_id
    );
    RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION public._prepare_project_deletion_job_storage()
    FROM PUBLIC, anon, authenticated;
