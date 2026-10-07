DO $$
DECLARE t text;
BEGIN
  IF to_regclass('public.synchronize_bindings') IS NULL THEN
    PERFORM public.entrypoint_source_migration_preflight();
    IF EXISTS (
      SELECT 1 FROM public.connections b JOIN public.entrypoint_source_decisions d ON d.legacy_id=b.id
      LEFT JOIN public.import_database_sources s ON s.id=d.import_database_source_id
      WHERE d.disposition IN ('import','both') AND (
        s.id IS NULL OR s.org_id IS DISTINCT FROM b.org_id OR s.project_id IS DISTINCT FROM b.project_id
        OR s.created_by IS DISTINCT FROM b.created_by OR s.name IS DISTINCT FROM b.name
        OR s.provider IS DISTINCT FROM b.config->>'db_provider' OR s.config IS DISTINCT FROM b.config
        OR s.status IS DISTINCT FROM b.status OR s.last_used_at IS DISTINCT FROM b.last_synced_at
        OR s.created_at IS DISTINCT FROM b.created_at OR s.updated_at IS DISTINCT FROM b.updated_at
        OR s.synchronize_binding_id IS DISTINCT FROM CASE WHEN d.disposition='both' THEN b.id ELSE NULL END)
    ) THEN RAISE EXCEPTION 'ENTRYPOINT_SOURCE_COPY_MISMATCH'; END IF;
  ELSE
    -- After normal application writes/deletes resume, historical snapshot
    -- equality is no longer a live invariant. Final structural/ownership checks
    -- remain rerunnable; the cutover receipt retains its immutable provenance.
    IF to_regclass('public.connections') IS NOT NULL OR to_regclass('public.sync_runs') IS NOT NULL
      OR to_regclass('public.github_sync_bindings') IS NOT NULL OR to_regclass('public.github_sync_log') IS NOT NULL
      OR to_regclass('public.github_integrations') IS NOT NULL
      OR to_regclass('public.entrypoint_source_decisions') IS NOT NULL
      OR to_regclass('public.entrypoint_cutover_control') IS NOT NULL THEN
      RAISE EXCEPTION 'ENTRYPOINT_CONTRACT_INCOMPLETE';
    END IF;
    IF to_regclass('public.synchronize_runs') IS NULL OR to_regclass('public.synchronize_github_bindings') IS NULL
      OR to_regclass('public.synchronize_github_logs') IS NULL THEN
      RAISE EXCEPTION 'ENTRYPOINT_FINAL_TABLE_MISSING';
    END IF;
    IF EXISTS (SELECT 1 FROM public.synchronize_bindings b WHERE
        ((b.config ? 'db_config' OR b.provider='database') AND b.legacy_read_only_reason IS NULL)
        OR (b.legacy_read_only_reason IS NOT NULL AND b.status<>'disabled'))
      OR EXISTS (SELECT 1 FROM public.synchronize_runs r JOIN public.synchronize_bindings b
                   ON b.id=r.synchronize_binding_id WHERE r.project_id<>b.project_id)
      OR EXISTS (SELECT 1 FROM public.import_database_sources s JOIN public.projects p ON p.id=s.project_id
                   WHERE s.org_id IS DISTINCT FROM p.org_id)
      OR EXISTS (SELECT 1 FROM public.import_database_sources s JOIN public.synchronize_bindings b
                   ON b.id=s.synchronize_binding_id WHERE s.project_id<>b.project_id OR s.org_id IS DISTINCT FROM b.org_id) THEN
      RAISE EXCEPTION 'ENTRYPOINT_FINAL_OWNERSHIP_MISMATCH';
    END IF;
    IF EXISTS (SELECT 1 FROM public.context_activity_items WHERE kind='sync_run') THEN
      RAISE EXCEPTION 'ENTRYPOINT_ACTIVITY_KIND_NOT_CONTRACTED';
    END IF;
    FOREACH t IN ARRAY ARRAY['synchronize_bindings','synchronize_runs','synchronize_github_bindings',
                             'synchronize_github_logs','import_database_sources','access_tools','search_index_tasks'] LOOP
      IF NOT (SELECT relrowsecurity FROM pg_class WHERE oid=('public.'||t)::regclass)
        OR has_table_privilege('anon','public.'||t,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
        OR has_table_privilege('authenticated','public.'||t,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
        OR has_any_column_privilege('anon','public.'||t,'SELECT,INSERT,UPDATE,REFERENCES')
        OR has_any_column_privilege('authenticated','public.'||t,'SELECT,INSERT,UPDATE,REFERENCES') THEN
        RAISE EXCEPTION 'ENTRYPOINT_FINAL_PRIVILEGE_MISMATCH';
      END IF;
    END LOOP;
  END IF;
END $$;
