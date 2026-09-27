DO $$
DECLARE
    inventory_complete boolean;
    pending_orphans bigint;
BEGIN
    IF to_regclass('public.project_storage_inventory_state') IS NULL
       OR to_regclass('public.project_storage_orphan_prefixes') IS NULL THEN
        RAISE EXCEPTION
            'Project storage inventory schema is missing; apply schema migrations first';
    END IF;

    SELECT state.inventory_complete INTO inventory_complete
    FROM public.project_storage_inventory_state state
    WHERE state.singleton;
    IF inventory_complete IS DISTINCT FROM true THEN
        RAISE EXCEPTION 'Project storage inventory is not complete';
    END IF;

    SELECT count(*) INTO pending_orphans
    FROM public.project_storage_orphan_prefixes
    WHERE status = 'pending';
    IF pending_orphans <> 0 THEN
        RAISE EXCEPTION
            'Project storage inventory left % pending orphan prefix(es)', pending_orphans;
    END IF;
END;
$$;
