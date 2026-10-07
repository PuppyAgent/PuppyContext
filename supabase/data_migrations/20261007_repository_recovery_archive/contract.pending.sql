-- Promote unchanged in a separate release after native consumer cutover and drain.
-- requires-data-migration: 20261007_repository_recovery_archive
-- data-migration-checksum: 0dfffa3bf880b8d1450e63f7fba45ca25b52fecf2f03d5ad2d573de6a9c922ee
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='5min';
LOCK TABLE public.projects,public.synchronize_github_logs IN ACCESS EXCLUSIVE MODE;
DO $$ BEGIN
    IF EXISTS(SELECT 1 FROM public.projects p LEFT JOIN public.version_repositories r ON r.project_id=p.id
        WHERE p.lifecycle_status<>'ready' OR r.authority IS DISTINCT FROM 'native') THEN
        RAISE EXCEPTION 'native_repository_required'; END IF;
    IF EXISTS(SELECT 1 FROM public.projects) AND NOT EXISTS(
        SELECT 1 FROM public.migration_log WHERE name='20261007_repository_recovery_archive'
          AND summary->>'artifact_checksum'='0dfffa3bf880b8d1450e63f7fba45ca25b52fecf2f03d5ad2d573de6a9c922ee'
          AND coalesce((summary->>'verified')::boolean,false)) THEN
        RAISE EXCEPTION 'DATA_MIGRATION_REQUIRED:20261007_repository_recovery_archive'; END IF;
    IF EXISTS(SELECT 1 FROM public.projects p LEFT JOIN public.version_repository_archives a ON a.project_id=p.id
        WHERE a.state IS DISTINCT FROM 'verified' OR a.manifest_sha256 IS NULL OR a.verified_at IS NULL OR a.source_deleted_at IS NULL) THEN
        RAISE EXCEPTION 'repository_recovery_archive_required'; END IF;
    IF EXISTS(SELECT 1 FROM public.project_write_leases WHERE expires_at>clock_timestamp()) THEN
        RAISE EXCEPTION 'migration_writers_not_drained'; END IF;
    IF EXISTS(SELECT 1 FROM public.projects WHERE version_root_hash IS DISTINCT FROM mut_root_hash)
      OR EXISTS(SELECT 1 FROM public.synchronize_github_logs WHERE version_commit_id IS DISTINCT FROM mut_commit_id) THEN
        RAISE EXCEPTION 'duplicate_version_field_mismatch'; END IF;
END $$;

-- This RPC still targets the removed content_nodes table; no runtime caller exists.
DROP FUNCTION public.count_children_batch(text[]);
-- Completed operator entrypoints must not remain callable in the runtime release.
DROP FUNCTION public.finish_repository_recovery_archive(text);
DROP FUNCTION public.begin_repository_recovery_archive(text);
DROP FUNCTION public._repository_recovery_source(text);

-- Retire the dual-write triggers before their duplicate fields disappear.
DROP TRIGGER projects_sync_version_columns ON public.projects;
DO $$ DECLARE item record; BEGIN
    FOR item IN SELECT tgname FROM pg_trigger WHERE tgrelid='public.synchronize_github_logs'::regclass
      AND tgfoid='public.sync_github_version_columns()'::regprocedure LOOP
        EXECUTE format('DROP TRIGGER %I ON public.synchronize_github_logs',item.tgname);
    END LOOP;
END $$;
DROP FUNCTION public.sync_project_version_columns();
DROP FUNCTION public.sync_github_version_columns();
DROP TRIGGER zz_version_repository_root_fence ON public.projects;

-- Canonical functions still containing historical SQL are rewritten in-place,
-- preserving their owner, SECURITY mode and ACL. Explicit token mappings cover
-- the known baseline only; the final catalog assertion rejects unknown remnants.
DO $$ DECLARE item record; definition text; old_name text; new_name text; BEGIN
    FOR item IN SELECT p.oid::regprocedure AS identity,p.proname
      FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
      WHERE n.nspname='public' AND p.prokind='f' AND p.proname ~ '(^|_)mut(_|$)' LOOP
        EXECUTE format('DROP FUNCTION %s',item.identity);
    END LOOP;
    FOR item IN SELECT p.oid,pg_get_functiondef(p.oid) AS definition
      FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
      WHERE n.nspname='public' AND p.prokind='f' AND p.prosrc ~ '(^|[^a-zA-Z0-9])mut_' LOOP
        definition:=item.definition;
        FOR old_name,new_name IN SELECT * FROM (VALUES
          ('mut_root_hash','version_root_hash'),('mut_commit_id','version_commit_id'),
          ('mut_version_index','version_view_commits'),('mut_version_outbox','version_outbox'),
          ('mut_commits','version_commits'),('mut_conflicts','version_conflicts'),
          ('mut_object_locations','version_object_locations'),('mut_scope_state','version_scope_state')) AS names(old_name,new_name) LOOP
            definition:=replace(definition,old_name,new_name);
        END LOOP;
        EXECUTE definition;
    END LOOP;
END $$;
-- No service executable root-first publisher remains after all consumers cut over.
-- No CASCADE: unexpected dependencies abort the release for review.
DO $$ DECLARE f record; BEGIN
    FOR f IN SELECT p.oid::regprocedure AS identity FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
      WHERE n.nspname='public' AND (p.proname LIKE 'publish_version_project_update%'
        OR p.proname IN ('cas_update_root_hash','cas_update_scope_hash','initialize_legacy_version_project_root',
          'claim_version_outbox_batch','complete_version_outbox','fail_version_outbox',
          'get_version_project_history_refs','get_version_project_write_state')) LOOP
        EXECUTE format('DROP FUNCTION %s',f.identity);
    END LOOP;
END $$;
REVOKE INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER ON
    public.version_commits,public.version_scope_state,public.version_view_commits,
    public.version_outbox,public.version_conflicts,public.local_shadow_snapshots FROM service_role;

CREATE TRIGGER zz_version_repository_root_fence BEFORE UPDATE OF version_root_hash ON public.projects
    FOR EACH ROW EXECUTE FUNCTION public._version_fence_legacy_publication();

-- The native creator may insert the Project before its repository in the same
-- transaction. At commit, no old creator may leave a root-only Project behind.
CREATE FUNCTION public._require_native_project_repository() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
    IF EXISTS(SELECT 1 FROM public.projects WHERE id=NEW.id)
      AND NOT EXISTS(SELECT 1 FROM public.version_repositories
                     WHERE project_id=NEW.id AND authority='native') THEN
        RAISE EXCEPTION 'native_repository_required' USING ERRCODE='55000';
    END IF;
    RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION public._require_native_project_repository() FROM PUBLIC,anon,authenticated,service_role;
CREATE CONSTRAINT TRIGGER version_project_requires_native_repository
    AFTER INSERT ON public.projects DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION public._require_native_project_repository();

DROP VIEW public.mut_commits,public.mut_conflicts,public.mut_object_locations,
    public.mut_scope_state,public.mut_version_index,public.mut_version_outbox;
ALTER TABLE public.projects DROP COLUMN mut_root_hash;
ALTER TABLE public.synchronize_github_logs DROP COLUMN mut_commit_id;

-- Rename identities without resetting sequence values or deleting row history.
DO $$ DECLARE item record; replacement text; BEGIN
    FOR item IN SELECT c.conname,c.conrelid::regclass AS relation FROM pg_constraint c
      JOIN pg_namespace n ON n.oid=c.connamespace WHERE n.nspname='public' AND c.conname ~ '(^|_)mut_' LOOP
        replacement:=replace(replace(replace(item.conname,'mut_version_index','version_view_commits'),
          'mut_version_outbox','version_outbox'),'mut_','version_');
        EXECUTE format('ALTER TABLE %s RENAME CONSTRAINT %I TO %I',item.relation,item.conname,replacement);
    END LOOP;
    FOR item IN SELECT c.oid::regclass AS identity,c.relname,c.relkind FROM pg_class c
      JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relname ~ '(^|_)mut_' LOOP
        replacement:=replace(replace(replace(item.relname,'mut_version_index','version_view_commits'),
          'mut_version_outbox','version_outbox'),'mut_','version_');
        IF item.relkind='S' THEN EXECUTE format('ALTER SEQUENCE %s RENAME TO %I',item.identity,replacement);
        ELSIF item.relkind='i' THEN EXECUTE format('ALTER INDEX %s RENAME TO %I',item.identity,replacement);
        ELSE RAISE EXCEPTION 'unexpected_retired_relation'; END IF;
    END LOOP;
    FOR item IN SELECT polname,polrelid::regclass AS relation FROM pg_policy WHERE polname ~ '(^|_)mut_' LOOP
        replacement:=replace(replace(replace(item.polname,'mut_version_index','version_view_commits'),
          'mut_version_outbox','version_outbox'),'mut_','version_');
        EXECUTE format('ALTER POLICY %I ON %s RENAME TO %I',item.polname,item.relation,replacement);
    END LOOP;
END $$;
DO $$ BEGIN
    IF EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='public' AND (p.proname ~ '(^|_)mut(_|$)' OR p.prosrc ~ '(^|[^a-zA-Z0-9])mut_' OR p.prosrc LIKE '%mut/%'))
      OR EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relname ~ '(^|_)mut(_|$)')
      OR EXISTS(SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND column_name ~ '^mut_')
      OR EXISTS(SELECT 1 FROM pg_attrdef d JOIN pg_class c ON c.oid=d.adrelid
          JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public'
          AND pg_get_expr(d.adbin,d.adrelid) ~* '\mmut\M') THEN
        RAISE EXCEPTION 'retired_version_schema_dependency_remaining: %', (SELECT string_agg(p.proname,',') FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND (p.proname ~ '(^|_)mut(_|$)' OR p.prosrc ~ '(^|[^a-zA-Z0-9])mut_' OR p.prosrc LIKE '%mut/%')); END IF;
END $$;
NOTIFY pgrst,'reload schema';
COMMIT;
