-- ISSUE-049 Release A: additive only. No legacy row is classified or removed here.
BEGIN;
SET LOCAL lock_timeout = '10s';
SET LOCAL statement_timeout = '60s';

CREATE TABLE public.import_database_sources (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  org_id text REFERENCES public.organizations(id) ON DELETE CASCADE,
  project_id text NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,
  created_by uuid REFERENCES auth.users(id) ON DELETE SET NULL,
  name text NOT NULL CHECK (name <> ''),
  provider text NOT NULL CHECK (provider <> ''),
  config jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(config) = 'object'),
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','syncing','error','disabled')),
  last_used_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  synchronize_binding_id text REFERENCES public.connections(id) ON DELETE SET NULL
);
COMMENT ON TABLE public.import_database_sources IS
  'One-time Database Import source configuration. Not a binding or an ImportJob. Config is encrypted and never public metadata.';
COMMENT ON COLUMN public.import_database_sources.synchronize_binding_id IS
  'Optional explicitly reviewed dual-use relationship; equal source/binding IDs never imply this relation.';
CREATE INDEX import_database_sources_project ON public.import_database_sources(project_id, created_at);
CREATE INDEX import_database_sources_binding ON public.import_database_sources(synchronize_binding_id)
  WHERE synchronize_binding_id IS NOT NULL;
ALTER TABLE public.import_database_sources ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.import_database_sources FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.import_database_sources TO service_role;
CREATE POLICY import_database_sources_service_role ON public.import_database_sources
  TO service_role USING (true) WITH CHECK (true);

CREATE FUNCTION public._validate_import_database_source() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE owner_org text;
BEGIN
  SELECT org_id INTO owner_org FROM public.projects WHERE id=NEW.project_id;
  IF NOT FOUND OR owner_org IS DISTINCT FROM NEW.org_id THEN
    RAISE EXCEPTION 'Import source crosses Project or Organization boundary';
  END IF;
  IF NEW.synchronize_binding_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM public.connections b WHERE b.id=NEW.synchronize_binding_id
      AND b.project_id=NEW.project_id AND b.org_id IS NOT DISTINCT FROM NEW.org_id
  ) THEN
    RAISE EXCEPTION 'Import source binding crosses Project or Organization boundary';
  END IF;
  RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION public._validate_import_database_source() FROM PUBLIC, anon, authenticated, service_role;
CREATE TRIGGER validate_import_database_source BEFORE INSERT OR UPDATE
  ON public.import_database_sources FOR EACH ROW EXECUTE FUNCTION public._validate_import_database_source();
CREATE TRIGGER import_database_sources_updated_at BEFORE UPDATE
  ON public.import_database_sources FOR EACH ROW EXECUTE FUNCTION public._context_entrypoint_bump_updated_at();

-- A retained unsupported historical binding is explicit read-only data, never
-- automatically activated merely because classification completed.
ALTER TABLE public.connections ADD COLUMN legacy_read_only_reason text
  CHECK (legacy_read_only_reason IS NULL OR btrim(legacy_read_only_reason) <> '');

-- Private transitional evidence, not application resources. Only the DB owner
-- can approve decisions/freeze; service_role cannot manufacture these facts.
CREATE TABLE public.entrypoint_cutover_control (
  singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  writes_frozen boolean NOT NULL DEFAULT false,
  migration_token uuid NOT NULL DEFAULT gen_random_uuid(),
  consumer_evidence_sha256 text,
  restore_point_ref text,
  approved_by text,
  frozen_at timestamptz,
  CHECK (NOT writes_frozen OR (
    consumer_evidence_sha256 ~ '^[0-9a-f]{64}$'
    AND btrim(restore_point_ref) <> '' AND btrim(approved_by) <> '' AND frozen_at IS NOT NULL
    AND consumer_evidence_sha256 IS NOT NULL AND restore_point_ref IS NOT NULL AND approved_by IS NOT NULL))
);
INSERT INTO public.entrypoint_cutover_control(singleton) VALUES (true);
ALTER TABLE public.entrypoint_cutover_control ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.entrypoint_cutover_control FROM PUBLIC, anon, authenticated, service_role;

CREATE TABLE public.entrypoint_source_decisions (
  legacy_id text PRIMARY KEY,
  project_id text NOT NULL,
  snapshot_sha256 text NOT NULL CHECK (snapshot_sha256 ~ '^[0-9a-f]{64}$'),
  disposition text NOT NULL CHECK (disposition IN ('synchronize','import','both')),
  import_database_source_id text UNIQUE,
  binding_read_only_reason text,
  evidence_ref text NOT NULL CHECK (btrim(evidence_ref) <> ''),
  approved_by text NOT NULL CHECK (btrim(approved_by) <> ''),
  approved_at timestamptz NOT NULL DEFAULT now(),
  CHECK ((disposition='synchronize' AND import_database_source_id IS NULL)
    OR (disposition IN ('import','both') AND import_database_source_id IS NOT NULL AND btrim(import_database_source_id) <> '')),
  CHECK (disposition <> 'import' OR import_database_source_id=legacy_id),
  CHECK (binding_read_only_reason IS NULL OR (disposition IN ('synchronize','both') AND btrim(binding_read_only_reason) <> ''))
);
ALTER TABLE public.entrypoint_source_decisions ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.entrypoint_source_decisions FROM PUBLIC, anon, authenticated, service_role;

CREATE FUNCTION public.entrypoint_source_snapshot(p_id text) RETURNS text
LANGUAGE sql STABLE SET search_path = pg_catalog, public, extensions AS $$
  SELECT encode(extensions.digest(jsonb_build_object(
    'binding', to_jsonb(b),
    'runs', (SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY r.id), '[]')
             FROM public.sync_runs r WHERE r.connection_id=b.id)
  )::text, 'sha256'), 'hex') FROM public.connections b WHERE b.id=p_id
$$;
REVOKE ALL ON FUNCTION public.entrypoint_source_snapshot(text) FROM PUBLIC, anon, authenticated, service_role;

CREATE FUNCTION public._guard_entrypoint_cutover_write() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE control public.entrypoint_cutover_control%ROWTYPE;
BEGIN
  SELECT * INTO STRICT control FROM public.entrypoint_cutover_control WHERE singleton;
  IF control.writes_frozen AND current_setting('puppyone.entrypoint_migration_token', true)
       IS DISTINCT FROM control.migration_token::text THEN
    RAISE EXCEPTION 'ENTRYPOINT_WRITES_FROZEN: migration in progress' USING ERRCODE='55000';
  END IF;
  IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
END $$;
REVOKE ALL ON FUNCTION public._guard_entrypoint_cutover_write() FROM PUBLIC, anon, authenticated, service_role;
DO $$ DECLARE t text; BEGIN
  FOREACH t IN ARRAY ARRAY['connections','sync_runs','github_sync_bindings','github_sync_log',
                           'access_tools','search_index_tasks','import_database_sources'] LOOP
    EXECUTE format('CREATE TRIGGER a00_entrypoint_cutover_freeze BEFORE INSERT OR UPDATE OR DELETE ON public.%I FOR EACH ROW EXECUTE FUNCTION public._guard_entrypoint_cutover_write()', t);
  END LOOP;
END $$;

CREATE FUNCTION public.entrypoint_source_migration_preflight() RETURNS void
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM public.connections) AND NOT EXISTS (
    SELECT 1 FROM public.entrypoint_cutover_control WHERE singleton AND writes_frozen
  ) THEN RAISE EXCEPTION 'ENTRYPOINT_FREEZE_REQUIRED'; END IF;
  IF EXISTS (SELECT 1 FROM public.sync_runs WHERE status IN ('queued','running')) THEN
    RAISE EXCEPTION 'ENTRYPOINT_RUN_DRAIN_REQUIRED';
  END IF;
  IF EXISTS (SELECT 1 FROM public.connections b FULL JOIN public.entrypoint_source_decisions d ON d.legacy_id=b.id
    WHERE b.id IS NULL OR d.legacy_id IS NULL OR d.project_id IS DISTINCT FROM b.project_id
      OR d.snapshot_sha256 IS DISTINCT FROM public.entrypoint_source_snapshot(b.id)) THEN
    RAISE EXCEPTION 'ENTRYPOINT_CLASSIFICATION_REQUIRED: missing, stale or foreign decision';
  END IF;
  IF EXISTS (SELECT 1 FROM public.connections b JOIN public.projects p ON p.id=b.project_id
              WHERE b.org_id IS DISTINCT FROM p.org_id)
    OR EXISTS (SELECT 1 FROM public.sync_runs r JOIN public.connections b ON b.id=r.connection_id
               WHERE r.project_id IS DISTINCT FROM b.project_id)
    OR EXISTS (SELECT 1 FROM public.connections b JOIN public.sync_runs r ON r.id=b.last_sync_run_id
               WHERE r.connection_id<>b.id OR r.project_id<>b.project_id) THEN
    RAISE EXCEPTION 'ENTRYPOINT_OWNERSHIP_REPAIR_REQUIRED';
  END IF;
  IF EXISTS (SELECT 1 FROM public.connections b JOIN public.entrypoint_source_decisions d ON d.legacy_id=b.id
    WHERE d.disposition='import' AND (
      EXISTS (SELECT 1 FROM public.sync_runs r WHERE r.connection_id=b.id)
      OR b.last_sync_run_id IS NOT NULL OR b.last_sync_commit_id IS NOT NULL
      OR b.remote_hash IS NOT NULL OR b.external_version IS NOT NULL OR b.cursor<>'{}'::jsonb
      OR b.scope_id IS NOT NULL OR b.target_path<>'' OR b.direction<>'inbound'
      OR b.oauth_connection_id IS NOT NULL OR b.credential_ref IS NOT NULL
      OR b.external_resource_id IS NOT NULL OR b.external_resource_label IS NOT NULL OR b.external_url IS NOT NULL
      OR b.trigger_type<>'manual' OR b.trigger_config<>'{}'::jsonb)) THEN
    RAISE EXCEPTION 'ENTRYPOINT_IMPORT_WOULD_LOSE_BINDING_FACTS';
  END IF;
  IF EXISTS (SELECT 1 FROM public.connections b JOIN public.entrypoint_source_decisions d ON d.legacy_id=b.id
    WHERE d.disposition IN ('import','both') AND (
      jsonb_typeof(b.config->'db_config') IS DISTINCT FROM 'object'
      OR b.config->'db_config'='{}'::jsonb OR coalesce(b.config->>'db_provider','')='')) THEN
    RAISE EXCEPTION 'ENTRYPOINT_IMPORT_CONFIGURATION_REPAIR_REQUIRED';
  END IF;
  IF EXISTS (SELECT 1 FROM public.connections b JOIN public.entrypoint_source_decisions d ON d.legacy_id=b.id
    WHERE d.disposition IN ('synchronize','both') AND d.binding_read_only_reason IS NULL
      AND (b.provider='database' OR (d.disposition='synchronize' AND b.config ? 'db_config'))) THEN
    RAISE EXCEPTION 'ENTRYPOINT_AMBIGUOUS_BINDING_REQUIRES_EXPLICIT_RETENTION';
  END IF;
END $$;
REVOKE ALL ON FUNCTION public.entrypoint_source_migration_preflight() FROM PUBLIC, anon, authenticated, service_role;
NOTIFY pgrst, 'reload schema';
COMMIT;
