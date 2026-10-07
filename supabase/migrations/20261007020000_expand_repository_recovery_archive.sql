-- Operator-only recovery archive. No public Git refs or runtime enrollment.
BEGIN;
SET LOCAL lock_timeout='5s';
CREATE TABLE public.version_repository_archives (
    project_id text PRIMARY KEY REFERENCES public.projects(id) ON DELETE CASCADE,
    id uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    source jsonb NOT NULL,
    state text NOT NULL DEFAULT 'frozen' CHECK(state IN ('frozen','verified')),
    manifest_sha256 text CHECK(manifest_sha256 ~ '^[0-9a-f]{64}$'),
    verified_at timestamptz,
    source_deleted_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE public.version_repository_archive_objects (
    project_id text NOT NULL REFERENCES public.version_repository_archives(project_id) ON DELETE CASCADE,
    source_key text NOT NULL,
    destination_key text NOT NULL,
    body_sha256 text NOT NULL CHECK(body_sha256 ~ '^[0-9a-f]{64}$'),
    size_bytes bigint NOT NULL CHECK(size_bytes>=0),
    PRIMARY KEY(project_id,source_key)
);
CREATE TABLE public.version_repository_archive_roots (
    project_id text NOT NULL REFERENCES public.version_repository_archives(project_id) ON DELETE CASCADE,
    source_table text NOT NULL,
    source_row text NOT NULL,
    source_field text NOT NULL,
    source_id text NOT NULL,
    object_id text NOT NULL CHECK(object_id ~ '^[0-9a-f]{40}$'),
    object_kind text NOT NULL CHECK(object_kind IN ('blob','tree','commit','tag')),
    PRIMARY KEY(project_id,source_table,source_row,source_field)
);
CREATE TABLE public.version_repository_archive_git_objects (
    LIKE public.version_repository_migration_objects INCLUDING DEFAULTS INCLUDING CONSTRAINTS,
    PRIMARY KEY(project_id,source_id,source_kind),
    FOREIGN KEY(project_id) REFERENCES public.version_repository_archives(project_id) ON DELETE CASCADE
);
ALTER TABLE public.version_repository_archives ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.version_repository_archive_objects ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.version_repository_archive_roots ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.version_repository_archive_git_objects ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.version_repository_archives, public.version_repository_archive_objects,
    public.version_repository_archive_roots, public.version_repository_archive_git_objects FROM PUBLIC,anon,authenticated,service_role;

CREATE FUNCTION public._repository_recovery_source(p_project text)
RETURNS jsonb LANGUAGE plpgsql STABLE SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE result jsonb; relation text; rows jsonb;
BEGIN
    SELECT jsonb_build_object('project_prompt_template',prompt_template) INTO result
      FROM public.projects WHERE id=p_project;
    FOREACH relation IN ARRAY ARRAY['version_commits','version_scope_state','version_view_commits',
        'version_refs','version_conflicts','version_outbox','local_shadow_snapshots'] LOOP
        EXECUTE format('SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY t.id),''[]''::jsonb) FROM public.%I t WHERE project_id=$1',relation)
            INTO rows USING p_project;
        result:=result||jsonb_build_object(relation,rows);
    END LOOP;
    SELECT coalesce(jsonb_agg(to_jsonb(l) ORDER BY l.object_id),'[]'::jsonb) INTO rows
      FROM public.version_object_locations l WHERE l.project_id=p_project
        AND (l.pack_key LIKE 'mut/'||p_project||'/%' OR l.pack_key LIKE 'chunked:mut/'||p_project||'/%');
    RETURN result||jsonb_build_object('object_locations',rows);
END $$;

CREATE FUNCTION public._repository_recovery_fence()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE ids text[]; pid text;
BEGIN
    IF TG_OP='INSERT' THEN ids:=ARRAY[NEW.project_id];
    ELSIF TG_OP='DELETE' THEN ids:=ARRAY[OLD.project_id];
    ELSE ids:=ARRAY[OLD.project_id,NEW.project_id]; END IF;
    FOREACH pid IN ARRAY ids LOOP
        PERFORM 1 FROM public.projects WHERE id=pid FOR KEY SHARE;
        IF EXISTS(SELECT 1 FROM public.version_repository_archives WHERE project_id=pid
            AND (source_deleted_at IS NULL OR TG_TABLE_NAME NOT IN ('project_write_leases','version_repository_refs'))) THEN
            -- Project deletion owns all relational cascades after it drains writers.
            IF TG_OP='DELETE' AND NOT EXISTS(SELECT 1 FROM public.projects WHERE id=pid) THEN CONTINUE; END IF;
            RAISE EXCEPTION 'repository_recovery_source_frozen' USING ERRCODE='55000';
        END IF;
    END LOOP;
    IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
END $$;
DO $$ DECLARE relation text; BEGIN
    FOREACH relation IN ARRAY ARRAY['version_commits','version_scope_state','version_view_commits',
        'version_refs','version_conflicts','version_outbox','local_shadow_snapshots',
        'project_write_leases','version_repository_refs'] LOOP
        EXECUTE format('CREATE TRIGGER repository_recovery_source_fence BEFORE INSERT OR UPDATE OR DELETE ON public.%I FOR EACH ROW EXECUTE FUNCTION public._repository_recovery_fence()',relation);
    END LOOP;
END $$;

CREATE FUNCTION public.begin_repository_recovery_archive(p_project text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE job public.version_repository_archives%ROWTYPE;
BEGIN
    PERFORM 1 FROM public.projects p JOIN public.version_repositories r ON r.project_id=p.id
      WHERE p.id=p_project AND p.lifecycle_status='ready' AND r.authority='native' FOR UPDATE OF p,r;
    IF NOT FOUND THEN RAISE EXCEPTION 'native_repository_required'; END IF;
    SELECT * INTO job FROM public.version_repository_archives WHERE project_id=p_project;
    IF FOUND THEN RETURN to_jsonb(job); END IF;
    IF EXISTS(SELECT 1 FROM public.project_write_leases WHERE project_id=p_project AND expires_at>clock_timestamp()) THEN
        RAISE EXCEPTION 'migration_writers_not_drained'; END IF;
    IF EXISTS(SELECT 1 FROM public.version_repositories WHERE project_id=p_project AND gc_token IS NOT NULL)
      OR EXISTS(SELECT 1 FROM public.version_object_pins WHERE project_id=p_project AND state<>'released' AND expires_at>clock_timestamp()) THEN
        RAISE EXCEPTION 'migration_gc_or_publication_not_drained'; END IF;
    INSERT INTO public.version_repository_archives(project_id,source)
      VALUES(p_project,public._repository_recovery_source(p_project)) RETURNING * INTO job;
    RETURN to_jsonb(job);
END $$;
REVOKE ALL ON FUNCTION public._repository_recovery_source(text),public._repository_recovery_fence(),
    public.begin_repository_recovery_archive(text) FROM PUBLIC,anon,authenticated,service_role;
-- Recovery objects are retained without making them readable Git roots.
CREATE OR REPLACE FUNCTION public.get_version_repository_gc_roots(p_project_id text)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
    SELECT coalesce(jsonb_agg(oid ORDER BY oid),'[]'::jsonb) FROM (
        SELECT target_oid AS oid FROM public.version_repository_refs WHERE project_id=p_project_id
        UNION SELECT old_state->>'oid' FROM public.version_reflog_entries WHERE project_id=p_project_id
        UNION SELECT new_state->>'oid' FROM public.version_reflog_entries WHERE project_id=p_project_id
        UNION SELECT jsonb_object_keys(r.roots) FROM public.version_publication_receipts r
            WHERE r.project_id=p_project_id AND r.expires_at>clock_timestamp()
        UNION SELECT object_id FROM public.version_repository_archive_roots WHERE project_id=p_project_id
    ) roots WHERE oid IS NOT NULL
$$;
CREATE OR REPLACE FUNCTION public.begin_version_repository_gc(p_project_id text,p_token uuid)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
    PERFORM 1 FROM public.projects WHERE id=p_project_id FOR UPDATE;
    IF EXISTS(SELECT 1 FROM public.version_repository_migrations WHERE project_id=p_project_id AND retain_source_objects)
      OR EXISTS(SELECT 1 FROM public.version_repository_archives WHERE project_id=p_project_id AND source_deleted_at IS NULL) THEN
        RAISE EXCEPTION 'migration_source_retention_required' USING ERRCODE='55000'; END IF;
    RETURN public._begin_version_repository_gc_before_migration(p_project_id,p_token);
END $$;

CREATE FUNCTION public.finish_repository_recovery_archive(p_project text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE org text; bytes bigint; objects bigint; job public.version_repository_archives%ROWTYPE;
BEGIN
    SELECT org_id INTO org FROM public.projects WHERE id=p_project;
    PERFORM public._version_lock_storage_organization(org);
    PERFORM 1 FROM public.projects WHERE id=p_project FOR UPDATE;
    SELECT * INTO job FROM public.version_repository_archives WHERE project_id=p_project FOR UPDATE;
    IF NOT FOUND OR job.state<>'verified' OR job.manifest_sha256 IS NULL THEN RAISE EXCEPTION 'verified_archive_required'; END IF;
    IF job.source_deleted_at IS NOT NULL THEN RETURN to_jsonb(job); END IF;
    -- Physical accounting is separate from user-visible logical content size.
    -- Do not bill private recovery snapshots as live files.
    WITH added AS (
      INSERT INTO public.version_repository_object_capacity(project_id,object_id,object_kind,body_bytes,admitted_pin_id)
      SELECT DISTINCT project_id,object_id,object_kind,body_bytes,job.id
        FROM (SELECT DISTINCT project_id,object_id,object_kind,body_bytes FROM public.version_repository_archive_git_objects WHERE project_id=p_project) x
      ON CONFLICT(project_id,object_id) DO NOTHING RETURNING body_bytes
    ) SELECT count(*),coalesce(sum(body_bytes),0) INTO objects,bytes FROM added;
    UPDATE public.version_repository_capacity SET used_body_bytes=used_body_bytes+bytes,used_objects=used_objects+objects
      WHERE project_id=p_project AND initialized
        AND (max_body_bytes IS NULL OR used_body_bytes+bytes<=max_body_bytes)
        AND (max_objects IS NULL OR used_objects+objects<=max_objects);
    IF NOT FOUND THEN RAISE EXCEPTION 'archive_repository_capacity_exceeded'; END IF;
    UPDATE public.version_organization_capacity SET used_body_bytes=used_body_bytes+bytes,used_objects=used_objects+objects
      WHERE org_id=org AND initialized
        AND (max_body_bytes IS NULL OR used_body_bytes+bytes<=max_body_bytes)
        AND (max_objects IS NULL OR used_objects+objects<=max_objects);
    IF NOT FOUND THEN RAISE EXCEPTION 'archive_organization_capacity_exceeded'; END IF;
    -- The archive retains the exact old index metadata; runtime no longer
    -- retains locators for removed physical keys.
    DELETE FROM public.version_object_locations WHERE project_id=p_project
      AND (pack_key LIKE 'mut/'||p_project||'/%' OR pack_key LIKE 'chunked:mut/'||p_project||'/%');
    UPDATE public.version_repository_archives SET source_deleted_at=clock_timestamp() WHERE project_id=p_project RETURNING * INTO job;
    UPDATE public.version_repository_migrations SET retain_source_objects=false WHERE project_id=p_project;
    RETURN to_jsonb(job);
END $$;
REVOKE ALL ON FUNCTION public.finish_repository_recovery_archive(text) FROM PUBLIC,anon,authenticated,service_role;
CREATE OR REPLACE FUNCTION public._version_fence_new_blob_size()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE org text; quota jsonb; maximum bigint;
BEGIN
    -- This private, owner-written archive capability authorizes accounting of
    -- already preserved bytes, never publication. Ordinary pins cannot match.
    IF EXISTS(SELECT 1 FROM public.version_repository_archives a
        JOIN public.version_repository_archive_git_objects o ON o.project_id=a.project_id
        WHERE a.project_id=NEW.project_id AND a.id=NEW.admitted_pin_id AND a.state='verified'
          AND a.source_deleted_at IS NULL AND o.object_id=NEW.object_id
          AND o.object_kind=NEW.object_kind AND o.body_bytes=NEW.body_bytes) THEN RETURN NEW; END IF;
    IF EXISTS(SELECT 1 FROM public.version_repository_file_policies WHERE project_id=NEW.project_id) THEN
        PERFORM 1 FROM public.version_repository_file_policies WHERE project_id=NEW.project_id AND initialized FOR SHARE;
        IF NOT FOUND THEN RAISE EXCEPTION 'repository_file_policy_uninitialized'; END IF;
        org:=public._version_assert_native_pin_admission(NEW.project_id,NEW.admitted_pin_id);
        quota:=public._version_file_policy_limit(org);
        maximum:=(quota->>'file_limit')::bigint;
        IF NEW.object_kind='blob' AND maximum IS NOT NULL AND NEW.body_bytes>maximum
           AND NOT EXISTS(SELECT 1 FROM public.version_repository_object_capacity
               WHERE project_id=NEW.project_id AND object_id=NEW.object_id) THEN
            RAISE EXCEPTION 'file_size_limit_exceeded' USING ERRCODE='P0001';
        END IF;
        PERFORM public._version_assert_native_pin_admission(NEW.project_id,NEW.admitted_pin_id);
    END IF;
    RETURN NEW;
END $$;
NOTIFY pgrst,'reload schema';
COMMIT;
