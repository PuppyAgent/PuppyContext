-- ISSUE-049 Release B ONLY, after separate Expand/data deployment and consumer exit.
-- requires-data-migration: 20261003_final_entrypoint_storage
-- data-migration-checksum: 4556f98f8ecd96cb60cadad0a5bc4815fff8b4abb6f18f30afed48e7ac324482
-- Promote byte-for-byte only after the existing 20260927 Contract and cutover gates.
BEGIN;
SET LOCAL lock_timeout='10s';
SET LOCAL statement_timeout='5min';
LOCK TABLE public.connections, public.sync_runs, public.github_sync_bindings, public.github_sync_log,
  public.import_database_sources, public.entrypoint_source_decisions, public.entrypoint_cutover_control,
  public.access_tools, public.search_index_tasks IN ACCESS EXCLUSIVE MODE;
DO $$
BEGIN
  IF to_regclass('public.github_integrations') IS NOT NULL OR EXISTS (
    SELECT 1 FROM information_schema.columns WHERE table_schema='public'
      AND ((table_name='access_tools' AND column_name='access_point_id')
        OR (table_name='github_sync_log' AND column_name='integration_id'))
  ) THEN RAISE EXCEPTION 'ENTRYPOINT_PREVIOUS_CONTRACT_REQUIRED'; END IF;
  IF EXISTS (SELECT 1 FROM public.projects) OR EXISTS (SELECT 1 FROM public.connections)
     OR EXISTS (SELECT 1 FROM public.import_database_sources) OR EXISTS (SELECT 1 FROM public.github_sync_bindings) THEN
    IF NOT EXISTS (SELECT 1 FROM public.migration_log WHERE name='20261003_final_entrypoint_storage'
      AND coalesce((summary->>'verified')::boolean,false)
      AND summary->>'artifact_checksum'='4556f98f8ecd96cb60cadad0a5bc4815fff8b4abb6f18f30afed48e7ac324482') THEN
      RAISE EXCEPTION 'DATA_MIGRATION_REQUIRED:20261003_final_entrypoint_storage';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.entrypoint_cutover_control WHERE singleton AND writes_frozen) THEN
      RAISE EXCEPTION 'ENTRYPOINT_FREEZE_REQUIRED';
    END IF;
  END IF;
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
  PERFORM set_config('puppyone.entrypoint_migration_token',
    (SELECT migration_token::text FROM public.entrypoint_cutover_control WHERE singleton),true);
END $$;

-- Preserve timestamps/history. Only explicitly approved dual-use configuration
-- is separated; an unsupported legacy binding remains visible but read-only.
ALTER TABLE public.connections DISABLE TRIGGER trg_connections_updated_at;
UPDATE public.connections b SET config=CASE WHEN d.disposition='both' THEN b.config-'db_config'-'db_provider' ELSE b.config END,
  legacy_read_only_reason=d.binding_read_only_reason,
  status=CASE WHEN d.binding_read_only_reason IS NOT NULL THEN 'disabled' ELSE b.status END,
  error_message=coalesce(d.binding_read_only_reason,b.error_message)
FROM public.entrypoint_source_decisions d WHERE d.legacy_id=b.id
  AND (d.disposition='both' OR (d.disposition='synchronize' AND d.binding_read_only_reason IS NOT NULL));
ALTER TABLE public.connections ENABLE TRIGGER trg_connections_updated_at;
DELETE FROM public.connections b USING public.entrypoint_source_decisions d
WHERE d.legacy_id=b.id AND d.disposition='import';

ALTER TABLE public.connections RENAME TO synchronize_bindings;
ALTER TABLE public.sync_runs RENAME TO synchronize_runs;
ALTER TABLE public.synchronize_bindings RENAME COLUMN last_sync_run_id TO last_synchronize_run_id;
ALTER TABLE public.synchronize_bindings RENAME COLUMN last_sync_commit_id TO last_synchronize_commit_id;
ALTER TABLE public.synchronize_runs RENAME COLUMN connection_id TO synchronize_binding_id;
ALTER TABLE public.synchronize_bindings ADD CONSTRAINT synchronize_bindings_no_import_config
  CHECK (legacy_read_only_reason IS NOT NULL OR NOT config ? 'db_config');
ALTER TABLE public.synchronize_bindings ADD CONSTRAINT synchronize_bindings_read_only_status
  CHECK ((legacy_read_only_reason IS NULL OR status='disabled')
    AND (provider<>'database' OR legacy_read_only_reason IS NOT NULL));
ALTER TABLE public.synchronize_bindings ADD CONSTRAINT synchronize_bindings_project_identity UNIQUE(id,project_id);
ALTER TABLE public.synchronize_runs ADD CONSTRAINT synchronize_runs_binding_identity UNIQUE(id,synchronize_binding_id,project_id);
ALTER TABLE public.synchronize_runs DROP CONSTRAINT sync_runs_connection_id_fkey;
ALTER TABLE public.synchronize_runs ADD CONSTRAINT synchronize_runs_binding_project_fkey
  FOREIGN KEY (synchronize_binding_id,project_id) REFERENCES public.synchronize_bindings(id,project_id) ON DELETE CASCADE;
ALTER TABLE public.synchronize_bindings DROP CONSTRAINT connections_last_sync_run_fk;
ALTER TABLE public.synchronize_bindings ADD CONSTRAINT synchronize_bindings_last_synchronize_run_fkey
  FOREIGN KEY (last_synchronize_run_id,id,project_id) REFERENCES public.synchronize_runs(id,synchronize_binding_id,project_id)
  ON DELETE SET NULL (last_synchronize_run_id);

ALTER TABLE public.github_sync_bindings RENAME TO synchronize_github_bindings;
ALTER TABLE public.github_sync_log RENAME TO synchronize_github_logs;
ALTER TABLE public.synchronize_github_bindings RENAME COLUMN auto_import TO auto_pull;
ALTER TABLE public.synchronize_github_bindings RENAME COLUMN last_imported_sha TO last_pulled_sha;
ALTER TABLE public.synchronize_github_bindings RENAME COLUMN last_imported_at TO last_pulled_at;
ALTER TABLE public.synchronize_github_bindings RENAME COLUMN last_exported_sha TO last_pushed_sha;
ALTER TABLE public.synchronize_github_bindings RENAME COLUMN last_exported_at TO last_pushed_at;
ALTER TABLE public.synchronize_github_logs RENAME COLUMN binding_id TO synchronize_github_binding_id;
ALTER TABLE public.synchronize_github_logs DROP CONSTRAINT github_sync_log_direction_check;
UPDATE public.synchronize_github_logs SET direction=CASE direction WHEN 'import' THEN 'inbound' WHEN 'export' THEN 'outbound' ELSE direction END;
ALTER TABLE public.synchronize_github_logs ADD CONSTRAINT synchronize_github_logs_direction_check CHECK (direction IN ('inbound','outbound'));

-- OID dependencies survive rename; literal SQL bodies and view discriminators
-- do not. Rewrite only these named owned functions, not arbitrary catalog text.
DO $$ DECLARE signature text; definition text; BEGIN
  FOREACH signature IN ARRAY ARRAY['public.repository_target_integrity_report()', 'public._validate_import_database_source()'] LOOP
    SELECT pg_get_functiondef(signature::regprocedure) INTO STRICT definition;
    IF position('public.connections' IN definition)=0 THEN
      RAISE EXCEPTION 'ENTRYPOINT_FUNCTION_SHAPE_CHANGED: %', signature;
    END IF;
    definition := replace(definition,'public.connections','public.synchronize_bindings');
    EXECUTE definition;
  END LOOP;
  EXECUTE 'CREATE OR REPLACE VIEW public.context_activity_items WITH (security_invoker=true) AS '
    || replace(pg_get_viewdef('public.context_activity_items'::regclass,true),'''sync_run''::text','''synchronize_run''::text');
END $$;
ALTER FUNCTION public._github_sync_bindings_bump_updated_at() RENAME TO _synchronize_github_bindings_bump_updated_at;
COMMENT ON COLUMN public.synchronize_bindings.target_path IS 'Explicit Project-root Synchronize write destination. Empty text is root; Scope geometry is not write authority.';

-- Rename owned object labels without touching OAuth IDs or unrelated scope_sync
-- / Version Engine / connector_runs history. Constraints rename their indexes.
DO $$ DECLARE item record; renamed text; old_name text; new_name text; pair text[]; BEGIN
  FOREACH pair SLICE 1 IN ARRAY ARRAY[
    ['connections','synchronize_bindings'],['sync_runs','synchronize_runs'],
    ['github_sync_bindings','synchronize_github_bindings'],['github_sync_log','synchronize_github_logs']
  ] LOOP
    old_name:=pair[1]; new_name:=pair[2];
    FOR item IN SELECT conname FROM pg_constraint WHERE conrelid=('public.'||new_name)::regclass LOOP
      renamed:=replace(item.conname,old_name,new_name);
      IF renamed<>item.conname THEN EXECUTE format('ALTER TABLE public.%I RENAME CONSTRAINT %I TO %I',new_name,item.conname,renamed); END IF;
    END LOOP;
    FOR item IN SELECT indexrelid::regclass AS qualified,c.relname FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
      WHERE indrelid=('public.'||new_name)::regclass LOOP
      renamed:=replace(item.relname,old_name,new_name);
      IF renamed<>item.relname THEN EXECUTE format('ALTER INDEX %s RENAME TO %I',item.qualified,renamed); END IF;
    END LOOP;
    FOR item IN SELECT tgname FROM pg_trigger WHERE tgrelid=('public.'||new_name)::regclass AND NOT tgisinternal LOOP
      renamed:=replace(item.tgname,old_name,new_name);
      IF renamed<>item.tgname THEN EXECUTE format('ALTER TRIGGER %I ON public.%I RENAME TO %I',item.tgname,new_name,renamed); END IF;
    END LOOP;
    FOR item IN SELECT polname FROM pg_policy WHERE polrelid=('public.'||new_name)::regclass LOOP
      renamed:=replace(item.polname,old_name,new_name);
      IF renamed<>item.polname THEN EXECUTE format('ALTER POLICY %I ON public.%I RENAME TO %I',item.polname,new_name,renamed); END IF;
    END LOOP;
  END LOOP;
END $$;

DO $$ DECLARE t text; columns text; BEGIN
  FOREACH t IN ARRAY ARRAY['synchronize_bindings','synchronize_runs','synchronize_github_bindings','synchronize_github_logs',
                           'access_tools','search_index_tasks','import_database_sources'] LOOP
    EXECUTE format('DROP TRIGGER a00_entrypoint_cutover_freeze ON public.%I',t);
    EXECUTE format('REVOKE ALL ON public.%I FROM PUBLIC,anon,authenticated,service_role',t);
    SELECT string_agg(quote_ident(attname),',') INTO columns FROM pg_attribute
      WHERE attrelid=('public.'||t)::regclass AND attnum>0 AND NOT attisdropped;
    EXECUTE format('REVOKE ALL (%s) ON public.%I FROM PUBLIC,anon,authenticated,service_role',columns,t);
    EXECUTE format('GRANT SELECT,INSERT,UPDATE,DELETE ON public.%I TO service_role',t);
  END LOOP;
END $$;
REVOKE ALL ON public.context_activity_items FROM PUBLIC,anon,authenticated,service_role;
GRANT SELECT ON public.context_activity_items TO service_role;
DROP FUNCTION public._guard_entrypoint_cutover_write();
DROP FUNCTION public.entrypoint_source_migration_preflight();
DROP FUNCTION public.entrypoint_source_snapshot(text);
DROP TABLE public.entrypoint_source_decisions;
DROP TABLE public.entrypoint_cutover_control;
NOTIFY pgrst, 'reload schema';
COMMIT;
