-- Rebuildable projections subscribe to native ref authority. No old outbox.
BEGIN;
CREATE TABLE public.version_projection_jobs (
    project_id text PRIMARY KEY REFERENCES public.version_repositories(project_id) ON DELETE CASCADE,
    requested_sequence bigint NOT NULL CHECK(requested_sequence>=0),
    completed_sequence bigint NOT NULL DEFAULT -1,
    claim_token uuid, claim_until timestamptz, attempts integer NOT NULL DEFAULT 0
);
ALTER TABLE public.version_projection_jobs ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.version_projection_jobs FROM PUBLIC,anon,authenticated,service_role;
CREATE FUNCTION public._enqueue_native_projection() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
    IF NEW.authority='native' THEN
        INSERT INTO public.version_projection_jobs(project_id,requested_sequence) VALUES(NEW.project_id,NEW.ref_sequence)
          ON CONFLICT(project_id) DO UPDATE SET requested_sequence=EXCLUDED.requested_sequence;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER enqueue_native_projection AFTER INSERT OR UPDATE OF authority,ref_sequence ON public.version_repositories
    FOR EACH ROW EXECUTE FUNCTION public._enqueue_native_projection();
INSERT INTO public.version_projection_jobs(project_id,requested_sequence)
    SELECT project_id,ref_sequence FROM public.version_repositories WHERE authority='native';
CREATE FUNCTION public.claim_native_projection_events(p_token uuid,p_limit integer DEFAULT 20)
RETURNS SETOF public.version_projection_jobs LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE p record; job public.version_projection_jobs%ROWTYPE;
BEGIN
    IF p_token IS NULL OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 100 THEN RAISE EXCEPTION 'invalid_projection_claim'; END IF;
    FOR p IN SELECT project.id FROM public.projects project JOIN public.version_projection_jobs j ON j.project_id=project.id
        WHERE project.lifecycle_status='ready' AND j.completed_sequence<j.requested_sequence
          AND (j.claim_until IS NULL OR j.claim_until<=clock_timestamp())
        ORDER BY project.id FOR UPDATE OF project SKIP LOCKED LIMIT p_limit LOOP
        UPDATE public.version_projection_jobs SET claim_token=p_token,claim_until=clock_timestamp()+interval '10 minutes',attempts=attempts+1
          WHERE project_id=p.id AND (claim_until IS NULL OR claim_until<=clock_timestamp()) RETURNING * INTO job;
        IF FOUND THEN RETURN NEXT job; END IF;
    END LOOP;
END $$;
CREATE FUNCTION public.replace_native_projection(p_project text,p_token uuid,p_sequence bigint,p_head text,p_paths jsonb,p_chunks jsonb)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
    PERFORM 1 FROM public.projects WHERE id=p_project AND lifecycle_status='ready' FOR UPDATE;
    IF NOT FOUND OR NOT EXISTS(SELECT 1 FROM public.version_projection_jobs WHERE project_id=p_project
        AND claim_token=p_token AND claim_until>clock_timestamp()) THEN RAISE EXCEPTION 'projection_claim_expired'; END IF;
    IF NOT EXISTS(SELECT 1 FROM public.version_repositories WHERE project_id=p_project AND authority='native' AND ref_sequence=p_sequence) THEN
        RAISE EXCEPTION 'projection_snapshot_changed'; END IF;
    IF jsonb_typeof(p_paths) IS DISTINCT FROM 'array' OR jsonb_typeof(p_chunks) IS DISTINCT FROM 'array'
      OR jsonb_array_length(p_paths)>100000 OR pg_column_size(p_paths)+pg_column_size(p_chunks)>33554432 THEN
        RAISE EXCEPTION 'projection_budget_exceeded'; END IF;
    DELETE FROM public.fs_path_index WHERE project_id=p_project;
    INSERT INTO public.fs_path_index(project_id,scope_path,full_path,blob_hash,size_bytes,mime_type,last_commit_id)
      SELECT p_project,'',r.path,r.oid,r.bytes,r.mime,p_head
      FROM jsonb_to_recordset(p_paths) AS r(path text,oid text,bytes bigint,mime text);
    DELETE FROM public.version_text_index WHERE project_id=p_project;
    INSERT INTO public.version_text_index(project_id,scope_path,file_path,content_hash,chunk_idx,line_start,text)
      SELECT p_project,'',r.path,r.oid,r.chunk_idx,r.line_start,r.text
      FROM jsonb_to_recordset(p_chunks) AS r(path text,oid text,chunk_idx integer,line_start integer,text text);
    DELETE FROM public.version_text_index_state WHERE project_id=p_project;
    INSERT INTO public.version_text_index_state(project_id,scope_path,indexed_commit_id) VALUES(p_project,'',p_head);
END $$;
CREATE FUNCTION public.finish_native_projection_events(p_project text,p_token uuid,p_sequence bigint)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
    PERFORM 1 FROM public.projects WHERE id=p_project FOR UPDATE;
    IF NOT EXISTS(SELECT 1 FROM public.version_projection_jobs WHERE project_id=p_project AND claim_token=p_token AND claim_until>clock_timestamp()) THEN
      RAISE EXCEPTION 'projection_claim_expired'; END IF;
    UPDATE public.version_projection_jobs SET completed_sequence=greatest(completed_sequence,p_sequence),claim_token=NULL,claim_until=NULL
      WHERE project_id=p_project AND requested_sequence>=p_sequence;
    IF NOT FOUND THEN RAISE EXCEPTION 'invalid_projection_sequence'; END IF;
    UPDATE public.version_ref_events SET processed_at=clock_timestamp()
      WHERE project_id=p_project AND processed_at IS NULL AND (payload->'result'->>'ref_sequence')::bigint<=p_sequence;
END $$;
CREATE FUNCTION public.release_native_projection_events(p_project text,p_token uuid)
RETURNS void LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
    UPDATE public.version_projection_jobs SET claim_token=NULL,claim_until=NULL WHERE project_id=p_project AND claim_token=p_token;
$$;
REVOKE ALL ON FUNCTION public._enqueue_native_projection(), public.claim_native_projection_events(uuid,integer),
    public.replace_native_projection(text,uuid,bigint,text,jsonb,jsonb),
    public.finish_native_projection_events(text,uuid,bigint),public.release_native_projection_events(text,uuid)
    FROM PUBLIC,anon,authenticated,service_role;
GRANT EXECUTE ON FUNCTION public.claim_native_projection_events(uuid,integer),
    public.replace_native_projection(text,uuid,bigint,text,jsonb,jsonb),
    public.finish_native_projection_events(text,uuid,bigint),public.release_native_projection_events(text,uuid) TO service_role;
NOTIFY pgrst,'reload schema';
COMMIT;
