-- Additive operator-only data migration control plane. No automatic enrollment.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='2min';

CREATE TABLE public.version_repository_migrations (
    project_id text PRIMARY KEY REFERENCES public.projects(id) ON DELETE CASCADE,
    id uuid NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    artifact_id text NOT NULL CHECK(artifact_id='20261007_native_repository_inventory'),
    source jsonb NOT NULL,
    state text NOT NULL DEFAULT 'frozen' CHECK(state IN ('frozen','prepared','activated')),
    refs jsonb,
    logical_bytes bigint CHECK(logical_bytes>=0),
    manifest_sha256 text CHECK(manifest_sha256 ~ '^[0-9a-f]{64}$'),
    prepared_at timestamptz,
    activated_at timestamptz,
    retain_source_objects boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE public.version_repository_migration_objects (
    project_id text NOT NULL REFERENCES public.version_repository_migrations(project_id) ON DELETE CASCADE,
    source_id text NOT NULL,
    source_kind text NOT NULL CHECK(source_kind IN ('blob','tree','commit','tag','snapshot')),
    object_id text NOT NULL CHECK(object_id ~ '^[0-9a-f]{40}$'),
    object_kind text NOT NULL CHECK(object_kind IN ('blob','tree','commit','tag')),
    body_bytes bigint NOT NULL CHECK(body_bytes BETWEEN 0 AND 67108864),
    source_sha256 text NOT NULL CHECK(source_sha256 ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY(project_id,source_id,source_kind)
);
ALTER TABLE public.version_repository_migrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.version_repository_migration_objects ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.version_repository_migrations,public.version_repository_migration_objects
    FROM PUBLIC,anon,authenticated,service_role;
GRANT SELECT ON public.version_repository_migrations,public.version_repository_migration_objects TO service_role;

CREATE FUNCTION public._native_migration_source(p_project text)
RETURNS jsonb LANGUAGE sql STABLE SET search_path=pg_catalog,public,pg_temp AS $$
    SELECT jsonb_build_object('project_id',p.id,'org_id',p.org_id,'root',p.version_root_hash,
      'commits',coalesce((SELECT jsonb_agg(to_jsonb(c) ORDER BY c.id) FROM public.version_commits c WHERE c.project_id=p.id),'[]'::jsonb),
      'scopes',coalesce((SELECT jsonb_agg(to_jsonb(c) ORDER BY c.scope_path) FROM public.version_scope_state c WHERE c.project_id=p.id),'[]'::jsonb),
      'views',coalesce((SELECT jsonb_agg(to_jsonb(c) ORDER BY c.id) FROM public.version_view_commits c WHERE c.project_id=p.id),'[]'::jsonb),
      'refs',coalesce((SELECT jsonb_agg(to_jsonb(c) ORDER BY c.id) FROM public.version_refs c WHERE c.project_id=p.id),'[]'::jsonb))
    FROM public.projects p WHERE p.id=p_project
$$;

-- No service role may mutate a frozen source, including reparenting a row.
CREATE FUNCTION public._native_migration_fence()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE ids text[]; pid text;
BEGIN
    IF TG_TABLE_NAME='projects' THEN
        IF TG_OP='UPDATE' AND NEW.org_id IS NOT DISTINCT FROM OLD.org_id
           AND NEW.lifecycle_status IS NOT DISTINCT FROM OLD.lifecycle_status
           AND NEW.version_root_hash IS NOT DISTINCT FROM OLD.version_root_hash
           AND NEW.mut_root_hash IS NOT DISTINCT FROM OLD.mut_root_hash THEN RETURN NEW; END IF;
        ids:=ARRAY[OLD.id];
    ELSIF TG_OP='INSERT' THEN ids:=ARRAY[NEW.project_id];
    ELSIF TG_OP='DELETE' THEN ids:=ARRAY[OLD.project_id];
    ELSE ids:=ARRAY[OLD.project_id,NEW.project_id]; END IF;
    FOR pid IN SELECT DISTINCT v FROM unnest(ids) v ORDER BY v LOOP
        PERFORM 1 FROM public.projects WHERE id=pid FOR UPDATE;
        IF EXISTS(SELECT 1 FROM public.version_repository_migrations WHERE project_id=pid AND state<>'activated') THEN
            RAISE EXCEPTION 'repository_migration_fenced' USING ERRCODE='55000';
        END IF;
    END LOOP;
    IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
END $$;
CREATE TRIGGER native_migration_project_fence BEFORE UPDATE OR DELETE ON public.projects
    FOR EACH ROW EXECUTE FUNCTION public._native_migration_fence();
CREATE TRIGGER native_migration_commits_fence BEFORE INSERT OR UPDATE OR DELETE ON public.version_commits
    FOR EACH ROW EXECUTE FUNCTION public._native_migration_fence();
CREATE TRIGGER native_migration_scope_fence BEFORE INSERT OR UPDATE OR DELETE ON public.version_scope_state
    FOR EACH ROW EXECUTE FUNCTION public._native_migration_fence();
CREATE TRIGGER native_migration_views_fence BEFORE INSERT OR UPDATE OR DELETE ON public.version_view_commits
    FOR EACH ROW EXECUTE FUNCTION public._native_migration_fence();
CREATE TRIGGER native_migration_refs_fence BEFORE INSERT OR UPDATE OR DELETE ON public.version_refs
    FOR EACH ROW EXECUTE FUNCTION public._native_migration_fence();
CREATE TRIGGER native_migration_leases_fence BEFORE INSERT OR UPDATE ON public.project_write_leases
    FOR EACH ROW EXECUTE FUNCTION public._native_migration_fence();

CREATE FUNCTION public._native_migration_location_fence()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE ids text[]; pid text;
BEGIN
    IF current_user <> pg_get_userbyid((SELECT relowner FROM pg_class
        WHERE oid='public.version_object_locations'::regclass)) THEN
        IF TG_OP='INSERT' THEN ids:=ARRAY[NEW.project_id];
        ELSIF TG_OP='DELETE' THEN ids:=ARRAY[OLD.project_id];
        ELSE ids:=ARRAY[OLD.project_id,NEW.project_id]; END IF;
        FOR pid IN SELECT DISTINCT v FROM unnest(ids) v ORDER BY v LOOP
            PERFORM 1 FROM public.projects WHERE id=pid FOR KEY SHARE;
            IF EXISTS(SELECT 1 FROM public.version_repository_migrations WHERE project_id=pid AND state<>'activated') THEN
                RAISE EXCEPTION 'repository_migration_fenced' USING ERRCODE='55000';
            END IF;
        END LOOP;
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
END $$;
CREATE TRIGGER native_migration_location_fence BEFORE INSERT OR UPDATE OR DELETE ON public.version_object_locations
    FOR EACH ROW EXECUTE FUNCTION public._native_migration_location_fence();

CREATE FUNCTION public.begin_native_repository_migration(p_project text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE p public.projects%ROWTYPE; job public.version_repository_migrations%ROWTYPE;
BEGIN
    SELECT * INTO p FROM public.projects WHERE id=p_project FOR UPDATE;
    IF NOT FOUND OR p.lifecycle_status<>'ready' THEN RAISE EXCEPTION 'migration_project_unavailable'; END IF;
    SELECT * INTO job FROM public.version_repository_migrations WHERE project_id=p_project;
    IF FOUND THEN RETURN to_jsonb(job); END IF;
    IF EXISTS(SELECT 1 FROM public.version_repositories WHERE project_id=p_project) THEN
        RAISE EXCEPTION 'migration_repository_already_enrolled'; END IF;
    IF EXISTS(SELECT 1 FROM public.project_write_leases WHERE project_id=p_project AND expires_at>clock_timestamp()) THEN
        RAISE EXCEPTION 'migration_writers_not_drained'; END IF;
    INSERT INTO public.version_repositories(project_id,authority,write_state)
        VALUES(p_project,'shadow','fenced');
    INSERT INTO public.version_repository_migrations(project_id,artifact_id,source)
        VALUES(p_project,'20261007_native_repository_inventory',public._native_migration_source(p_project))
        RETURNING * INTO job;
    RETURN to_jsonb(job);
END $$;

-- Operator owner access only: these are NOT runtime RPCs or service-role
-- self-enrollment. The trusted immutable artifact verifies actual S3 bytes.
CREATE FUNCTION public.prepare_native_repository_migration(p_project text,p_refs jsonb,p_logical bigint,p_digest text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE job public.version_repository_migrations%ROWTYPE; item record; n bigint; bytes bigint;
BEGIN
    PERFORM 1 FROM public.projects WHERE id=p_project FOR UPDATE;
    SELECT * INTO job FROM public.version_repository_migrations WHERE project_id=p_project FOR UPDATE;
    IF NOT FOUND OR job.state='activated' THEN RAISE EXCEPTION 'migration_not_frozen'; END IF;
    IF job.source IS DISTINCT FROM public._native_migration_source(p_project) THEN RAISE EXCEPTION 'migration_source_changed'; END IF;
    IF p_logical IS NULL OR p_logical<0 OR p_digest IS NULL OR p_digest !~ '^[0-9a-f]{64}$'
       OR jsonb_typeof(p_refs) IS DISTINCT FROM 'object' OR NOT p_refs ? 'refs/heads/main' THEN
        RAISE EXCEPTION 'migration_invalid_manifest'; END IF;
    FOR item IN SELECT * FROM jsonb_each_text(p_refs) LOOP
        IF NOT public._version_ref_name_valid(convert_to(item.key,'UTF8')) OR item.key='HEAD'
           OR NOT public._version_oid_valid(item.value,'sha1') OR NOT EXISTS(
             SELECT 1 FROM public.version_repository_migration_objects WHERE project_id=p_project AND object_id=item.value
               AND (item.key NOT LIKE 'refs/heads/%' OR object_kind='commit')) THEN RAISE EXCEPTION 'migration_invalid_ref'; END IF;
    END LOOP;
    SELECT count(*),coalesce(sum(body_bytes),0) INTO n,bytes FROM (
        SELECT DISTINCT object_id,body_bytes FROM public.version_repository_migration_objects WHERE project_id=p_project) o;
    IF n=0 OR n>100000 OR bytes>268435456 OR (SELECT count(*) FROM jsonb_object_keys(p_refs))>9999 THEN
        RAISE EXCEPTION 'migration_capacity_exceeded'; END IF;
    UPDATE public.version_repository_migrations SET state='prepared',refs=p_refs,logical_bytes=p_logical,
        manifest_sha256=p_digest,prepared_at=clock_timestamp() WHERE project_id=p_project;
END $$;

CREATE FUNCTION public.activate_native_repository_migrations(p_org text)
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE job public.version_repository_migrations%ROWTYPE; n bigint:=0; bytes bigint; objects bigint;
    total bigint; reconciliation uuid:=gen_random_uuid(); entitlement jsonb; item record;
BEGIN
    PERFORM public._version_lock_storage_organization(p_org);
    PERFORM 1 FROM public.projects WHERE org_id=p_org ORDER BY id FOR UPDATE;
    IF EXISTS(SELECT 1 FROM public.projects p LEFT JOIN public.version_repositories r ON r.project_id=p.id
        LEFT JOIN public.version_repository_migrations m ON m.project_id=p.id WHERE p.org_id=p_org
        AND (p.lifecycle_status<>'ready' OR (coalesce(r.authority,'legacy')<>'native' AND m.state IS DISTINCT FROM 'prepared'))) THEN
        RAISE EXCEPTION 'migration_organization_incomplete'; END IF;
    IF EXISTS(SELECT 1 FROM public.version_repositories r JOIN public.projects p ON p.id=r.project_id
        LEFT JOIN public.version_repository_billing b ON b.project_id=r.project_id
        WHERE p.org_id=p_org AND r.authority='native' AND (b.initialized IS DISTINCT FROM true OR b.accounted_bytes IS NULL)) THEN
        RAISE EXCEPTION 'migration_native_billing_incomplete'; END IF;
    entitlement:=public._version_file_policy_limit(p_org);
    FOR job IN SELECT m.* FROM public.version_repository_migrations m JOIN public.projects p ON p.id=m.project_id
        WHERE p.org_id=p_org AND m.state='prepared' ORDER BY m.project_id FOR UPDATE OF m LOOP
        IF job.source IS DISTINCT FROM public._native_migration_source(job.project_id) THEN RAISE EXCEPTION 'migration_source_changed'; END IF;
        IF EXISTS(SELECT 1 FROM public.project_write_leases WHERE project_id=job.project_id AND expires_at>clock_timestamp()) THEN
            RAISE EXCEPTION 'migration_writers_not_drained'; END IF;
        IF EXISTS(SELECT 1 FROM public.version_repository_refs WHERE project_id=job.project_id) THEN RAISE EXCEPTION 'migration_refs_not_empty'; END IF;
        SELECT count(*),sum(body_bytes) INTO objects,bytes FROM (SELECT DISTINCT object_id,body_bytes
            FROM public.version_repository_migration_objects WHERE project_id=job.project_id) o;
        INSERT INTO public.version_organization_capacity(org_id,initialized,max_body_bytes,max_objects)
            VALUES(p_org,true,34359738368,4000000) ON CONFLICT(org_id) DO NOTHING;
        UPDATE public.version_organization_capacity SET used_body_bytes=used_body_bytes+bytes,used_objects=used_objects+objects
            WHERE org_id=p_org AND initialized
              AND (max_body_bytes IS NULL OR used_body_bytes+bytes<=max_body_bytes)
              AND (max_objects IS NULL OR used_objects+objects<=max_objects);
        IF NOT FOUND THEN RAISE EXCEPTION 'migration_organization_capacity_exceeded'; END IF;
        INSERT INTO public.version_repository_capacity(project_id,org_id,initialized,max_body_bytes,max_objects,used_body_bytes,used_objects)
            VALUES(job.project_id,p_org,true,268435456,100000,bytes,objects);
        INSERT INTO public.version_repository_object_capacity(project_id,object_id,object_kind,body_bytes,admitted_pin_id)
            SELECT DISTINCT project_id,object_id,object_kind,body_bytes,job.id FROM public.version_repository_migration_objects WHERE project_id=job.project_id;
        INSERT INTO public.version_repository_billing(project_id,org_id,initialized,accounted_bytes)
            VALUES(job.project_id,p_org,true,job.logical_bytes);
        INSERT INTO public.version_repository_file_policies(project_id,initialized) VALUES(job.project_id,true);
        FOR item IN SELECT * FROM jsonb_each_text(job.refs) LOOP
            INSERT INTO public.version_repository_refs(project_id,name,object_format,target_oid)
                VALUES(job.project_id,convert_to(item.key,'UTF8'),'sha1',item.value);
        END LOOP;
        INSERT INTO public.version_repository_refs(project_id,name,object_format,symbolic_target)
            VALUES(job.project_id,convert_to('HEAD','UTF8'),'sha1',convert_to('refs/heads/main','UTF8'));
        UPDATE public.version_repositories SET authority='native',write_state='active',generation=generation+1
            WHERE project_id=job.project_id AND authority='shadow' AND write_state='fenced' AND gc_token IS NULL;
        IF NOT FOUND THEN RAISE EXCEPTION 'migration_authority_changed'; END IF;
        UPDATE public.version_repository_migrations SET state='activated',activated_at=clock_timestamp() WHERE project_id=job.project_id;
        INSERT INTO public.audit_logs(action,operator_type,operator_id,project_id,status,metadata)
            VALUES('repository_migrated','system','native-data-migration',job.project_id,'committed',
                jsonb_build_object('migration_id',job.id,'manifest_sha256',job.manifest_sha256));
        n:=n+1;
    END LOOP;
    IF n=0 THEN RETURN 0; END IF;
    -- Use the existing reconciliation protocol, including its authoritative
    -- full-organization snapshot and billing event. Never overwrite a counter
    -- with only the migrated project's bytes.
    INSERT INTO public.organization_usage_counters(org_id,metric,value,version)
        VALUES(p_org,'storage.logical_bytes',0,1) ON CONFLICT(org_id,metric) DO NOTHING;
    PERFORM public.begin_version_storage_reconciliation(p_org,reconciliation);
    FOR item IN SELECT b.project_id,b.accounted_bytes FROM public.version_repository_billing b
        JOIN public.projects p ON p.id=b.project_id WHERE p.org_id=p_org LOOP
        PERFORM public.record_version_storage_measurements(p_org,reconciliation,jsonb_build_object(item.project_id,item.accounted_bytes));
    END LOOP;
    PERFORM public.finish_version_storage_reconciliation(p_org,reconciliation);
    RETURN n;
END $$;

-- Existing source-only objects (e.g. private shadow snapshots/conflict inputs)
-- must not become GC candidates merely because native refs replaced old roots.
-- Retention is lifted only by a separate reviewed cleanup, never by this job.
ALTER FUNCTION public.get_version_repository_snapshot(text) RENAME TO _get_version_repository_snapshot_before_migration;
CREATE FUNCTION public.get_version_repository_snapshot(p_project_id text)
RETURNS jsonb LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
    SELECT public._get_version_repository_snapshot_before_migration(p_project_id)
        || jsonb_build_object('migration_source_retained',EXISTS(
            SELECT 1 FROM public.version_repository_migrations WHERE project_id=p_project_id AND retain_source_objects))
$$;
REVOKE ALL ON FUNCTION public._get_version_repository_snapshot_before_migration(text),
    public.get_version_repository_snapshot(text) FROM PUBLIC,anon,authenticated,service_role;
GRANT EXECUTE ON FUNCTION public.get_version_repository_snapshot(text) TO service_role;

ALTER FUNCTION public.begin_version_repository_gc(text,uuid) RENAME TO _begin_version_repository_gc_before_migration;
CREATE FUNCTION public.begin_version_repository_gc(p_project_id text,p_token uuid)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
    PERFORM 1 FROM public.projects WHERE id=p_project_id FOR UPDATE;
    IF EXISTS(SELECT 1 FROM public.version_repository_migrations WHERE project_id=p_project_id AND retain_source_objects) THEN
        RAISE EXCEPTION 'migration_source_retention_required' USING ERRCODE='55000';
    END IF;
    RETURN public._begin_version_repository_gc_before_migration(p_project_id,p_token);
END $$;
REVOKE ALL ON FUNCTION public._begin_version_repository_gc_before_migration(text,uuid),
    public.begin_version_repository_gc(text,uuid) FROM PUBLIC,anon,authenticated,service_role;
GRANT EXECUTE ON FUNCTION public.begin_version_repository_gc(text,uuid) TO service_role;

REVOKE ALL ON FUNCTION public._native_migration_source(text),public._native_migration_fence(),public._native_migration_location_fence(),
    public.begin_native_repository_migration(text),public.prepare_native_repository_migration(text,jsonb,bigint,text),
    public.activate_native_repository_migrations(text) FROM PUBLIC,anon,authenticated,service_role;
NOTIFY pgrst,'reload schema';
COMMIT;
