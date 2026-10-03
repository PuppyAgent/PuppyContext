-- The portable runner owns transaction, verification, locking and receipt.
-- No old row is deleted in Release A; source/config values never enter logs.
DO $$
BEGIN
  IF to_regclass('public.synchronize_bindings') IS NULL THEN
    LOCK TABLE public.connections, public.sync_runs, public.import_database_sources,
      public.entrypoint_source_decisions, public.entrypoint_cutover_control IN ACCESS EXCLUSIVE MODE;
    PERFORM public.entrypoint_source_migration_preflight();
    PERFORM set_config('puppyone.entrypoint_migration_token',
      (SELECT migration_token::text FROM public.entrypoint_cutover_control WHERE singleton), true);
    INSERT INTO public.import_database_sources(
      id,org_id,project_id,created_by,name,provider,config,status,last_used_at,created_at,updated_at,synchronize_binding_id)
    SELECT d.import_database_source_id,b.org_id,b.project_id,b.created_by,b.name,b.config->>'db_provider',
      b.config,b.status,b.last_synced_at,b.created_at,b.updated_at,
      CASE WHEN d.disposition='both' THEN b.id ELSE NULL END
    FROM public.connections b JOIN public.entrypoint_source_decisions d ON d.legacy_id=b.id
    WHERE d.disposition IN ('import','both')
    ON CONFLICT (id) DO NOTHING;
    -- verify.sql rejects collisions/mismatches atomically, not just row counts.
  END IF;
END $$;
