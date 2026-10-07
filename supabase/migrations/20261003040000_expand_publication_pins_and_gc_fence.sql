-- ISSUE-062 Expand: coordination for verified physical objects, not activation.
-- No existing repository is enrolled or switched. No network/data work in DDL.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '2min';

-- Widen the existing quarantine ledger; SHA-1 rows and RPC identities remain
-- valid. No object/data conversion and no change to legacy ref constraints.
ALTER TABLE public.version_object_gc_candidates
    DROP CONSTRAINT version_object_gc_candidates_object_id_check,
    ADD CONSTRAINT version_object_gc_candidates_object_id_check
        CHECK (object_id ~ '^([0-9a-f]{40}|[0-9a-f]{64})$');

ALTER TABLE public.version_repositories ADD COLUMN gc_token uuid;
CREATE TABLE public.version_object_pins (
    id uuid PRIMARY KEY,
    project_id text NOT NULL REFERENCES public.version_repositories(project_id) ON DELETE CASCADE,
    actor text NOT NULL CHECK (length(actor) BETWEEN 1 AND 512),
    object_format text NOT NULL CHECK (object_format IN ('sha1','sha256')),
    generation bigint NOT NULL,
    gc_epoch bigint NOT NULL,
    purpose text NOT NULL DEFAULT 'publication' CHECK (purpose IN ('publication','read')),
    roots jsonb NOT NULL CHECK (public._version_receipt_roots_valid(roots, object_format) OR (purpose='read' AND roots='{}'::jsonb)),
    state text NOT NULL DEFAULT 'uploading' CHECK (state IN ('uploading','verified','released')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL
);
CREATE INDEX version_object_pins_active_idx ON public.version_object_pins(project_id, expires_at)
    WHERE state <> 'released';
CREATE TABLE public.version_repository_gc_runs (
    token uuid PRIMARY KEY,
    project_id text NOT NULL REFERENCES public.version_repositories(project_id) ON DELETE CASCADE,
    snapshot jsonb NOT NULL,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz
);

CREATE TABLE public.version_repository_root_metadata (
    project_id text NOT NULL REFERENCES public.version_repositories(project_id) ON DELETE CASCADE,
    oid text NOT NULL,
    object_format text NOT NULL CHECK (object_format IN ('sha1','sha256')),
    kind text NOT NULL CHECK (kind IN ('commit','tree','blob','tag')),
    peeled_oid text,
    PRIMARY KEY (project_id,oid),
    CHECK (public._version_oid_valid(oid, object_format)),
    CHECK ((kind='tag') = (peeled_oid IS NOT NULL)),
    CHECK (peeled_oid IS NULL OR public._version_oid_valid(peeled_oid, object_format))
);

-- One lock order for publication admission, sealing, release and GC. Helpers
-- remain owner-only; callers use the narrow backend RPCs below.
CREATE FUNCTION public._version_lock_native_repository(p_project_id text, p_write boolean DEFAULT true)
RETURNS public.version_repositories LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE repo public.version_repositories%ROWTYPE; lifecycle text;
BEGIN
    SELECT lifecycle_status INTO lifecycle FROM public.projects WHERE id=p_project_id FOR UPDATE;
    IF NOT FOUND OR lifecycle <> 'ready' THEN
        RAISE EXCEPTION 'repository_unavailable' USING ERRCODE='55000';
    END IF;
    SELECT * INTO repo FROM public.version_repositories WHERE project_id=p_project_id FOR UPDATE;
    IF NOT FOUND OR repo.authority <> 'native' OR (p_write AND repo.write_state <> 'active') THEN
        RAISE EXCEPTION 'repository_unavailable' USING ERRCODE='55000';
    END IF;
    RETURN repo;
END $$;

CREATE FUNCTION public.begin_version_object_publication(
    p_project_id text, p_actor text, p_pin_id uuid, p_generation bigint, p_roots jsonb
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE repo public.version_repositories%ROWTYPE; pin public.version_object_pins%ROWTYPE;
BEGIN
    repo := public._version_lock_native_repository(p_project_id);
    IF p_pin_id IS NULL OR p_actor IS NULL OR length(p_actor) NOT BETWEEN 1 AND 512
       OR p_generation IS DISTINCT FROM repo.generation
       OR NOT public._version_receipt_roots_valid(p_roots, repo.object_format) THEN
        RAISE EXCEPTION 'invalid_publication_pin' USING ERRCODE='22023';
    END IF;
    IF (SELECT count(*) FROM jsonb_object_keys(p_roots)) > 256 THEN
        RAISE EXCEPTION 'too_many_publication_roots' USING ERRCODE='22023';
    END IF;
    IF repo.gc_token IS NOT NULL THEN RAISE EXCEPTION 'repository_gc_in_progress' USING ERRCODE='55000'; END IF;
    SELECT * INTO pin FROM public.version_object_pins WHERE id=p_pin_id FOR UPDATE;
    IF FOUND THEN
        IF pin.project_id <> p_project_id OR pin.actor <> p_actor
           OR pin.generation <> p_generation OR pin.roots <> p_roots OR pin.purpose <> 'publication' THEN
            RAISE EXCEPTION 'publication_pin_reused' USING ERRCODE='22023';
        END IF;
        IF pin.gc_epoch <> repo.gc_epoch OR pin.state='released' OR pin.expires_at <= clock_timestamp() THEN
            RAISE EXCEPTION 'publication_pin_unavailable' USING ERRCODE='55000';
        END IF;
        RETURN to_jsonb(pin);
    END IF;
    INSERT INTO public.version_object_pins(id,project_id,actor,object_format,generation,gc_epoch,roots,expires_at)
    VALUES (p_pin_id,p_project_id,p_actor,repo.object_format,repo.generation,repo.gc_epoch,p_roots,
            clock_timestamp()+interval '30 minutes') RETURNING * INTO pin;
    RETURN to_jsonb(pin);
END $$;

CREATE FUNCTION public.renew_version_object_publication(p_project_id text, p_actor text, p_pin_id uuid)
RETURNS timestamptz LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE repo public.version_repositories%ROWTYPE; expiration timestamptz;
BEGIN
    repo := public._version_lock_native_repository(p_project_id, false);
    UPDATE public.version_object_pins SET expires_at=clock_timestamp()+interval '30 minutes'
    WHERE id=p_pin_id AND project_id=p_project_id AND actor=p_actor AND state='uploading'
      AND generation=repo.generation AND gc_epoch=repo.gc_epoch
      AND (purpose='read' OR (repo.gc_token IS NULL AND repo.write_state='active'))
      AND expires_at > clock_timestamp() RETURNING expires_at INTO expiration;
    IF NOT FOUND THEN RAISE EXCEPTION 'publication_pin_unavailable' USING ERRCODE='55000'; END IF;
    RETURN expiration;
END $$;

CREATE FUNCTION public.seal_version_object_publication(
    p_project_id text, p_actor text, p_pin_id uuid, p_manifest_sha256 text, p_root_details jsonb DEFAULT NULL
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE repo public.version_repositories%ROWTYPE; pin public.version_object_pins%ROWTYPE;
        receipt public.version_publication_receipts%ROWTYPE; item record; detail jsonb;
        metadata public.version_repository_root_metadata%ROWTYPE;
BEGIN
    repo := public._version_lock_native_repository(p_project_id);
    SELECT * INTO pin FROM public.version_object_pins WHERE id=p_pin_id AND project_id=p_project_id
        AND actor=p_actor FOR UPDATE;
    IF NOT FOUND OR pin.state='released' OR pin.purpose <> 'publication' OR pin.expires_at <= clock_timestamp()
       OR pin.generation <> repo.generation OR pin.gc_epoch <> repo.gc_epoch OR repo.gc_token IS NOT NULL THEN
        RAISE EXCEPTION 'publication_pin_unavailable' USING ERRCODE='55000';
    END IF;
    IF p_manifest_sha256 IS NULL OR p_manifest_sha256 !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'invalid_manifest' USING ERRCODE='22023';
    END IF;
    IF p_root_details IS NOT NULL AND (jsonb_typeof(p_root_details) IS DISTINCT FROM 'object'
        OR (SELECT count(*) FROM jsonb_object_keys(p_root_details)) <> (SELECT count(*) FROM jsonb_object_keys(pin.roots))
        OR NOT p_root_details ?& ARRAY(SELECT jsonb_object_keys(pin.roots))) THEN
        RAISE EXCEPTION 'invalid_root_details' USING ERRCODE='22023';
    END IF;
    FOR item IN SELECT key,value FROM jsonb_each_text(pin.roots) LOOP
        detail := coalesce(p_root_details->item.key,jsonb_build_object('kind',item.value,'peeled_oid',NULL));
        IF detail->>'kind' IS DISTINCT FROM item.value OR (item.value='tag') <> (detail->>'peeled_oid' IS NOT NULL)
           OR (detail->>'peeled_oid' IS NOT NULL AND NOT public._version_oid_valid(detail->>'peeled_oid',pin.object_format)) THEN
            RAISE EXCEPTION 'invalid_root_details' USING ERRCODE='22023';
        END IF;
        INSERT INTO public.version_repository_root_metadata(project_id,oid,object_format,kind,peeled_oid)
            VALUES(p_project_id,item.key,pin.object_format,item.value,detail->>'peeled_oid') ON CONFLICT DO NOTHING;
        SELECT * INTO metadata FROM public.version_repository_root_metadata WHERE project_id=p_project_id AND oid=item.key;
        IF metadata.kind IS DISTINCT FROM item.value OR metadata.peeled_oid IS DISTINCT FROM detail->>'peeled_oid' THEN
            RAISE EXCEPTION 'root_metadata_mismatch' USING ERRCODE='22023';
        END IF;
    END LOOP;
    SELECT * INTO receipt FROM public.version_publication_receipts WHERE id=p_pin_id;
    IF FOUND THEN
        IF receipt.project_id <> pin.project_id OR receipt.generation <> pin.generation
           OR receipt.gc_epoch <> pin.gc_epoch OR receipt.roots <> pin.roots
           OR receipt.manifest_sha256 <> p_manifest_sha256 THEN
            RAISE EXCEPTION 'receipt_mismatch' USING ERRCODE='22023';
        END IF;
        RETURN to_jsonb(receipt);
    END IF;
    INSERT INTO public.version_publication_receipts
        (id,project_id,object_format,generation,gc_epoch,roots,manifest_sha256,verified_at,expires_at)
    VALUES (pin.id,pin.project_id,pin.object_format,pin.generation,pin.gc_epoch,pin.roots,
            p_manifest_sha256,clock_timestamp(),pin.expires_at) RETURNING * INTO receipt;
    UPDATE public.version_object_pins SET state='verified' WHERE id=pin.id;
    RETURN to_jsonb(receipt);
END $$;

CREATE FUNCTION public.release_version_object_publication(p_project_id text, p_actor text, p_pin_id uuid)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    -- Release is allowed after fencing; it cannot create objects or authority.
    PERFORM 1 FROM public.projects WHERE id=p_project_id FOR UPDATE;
    PERFORM 1 FROM public.version_repositories WHERE project_id=p_project_id FOR UPDATE;
    UPDATE public.version_object_pins SET state='released'
        WHERE id=p_pin_id AND project_id=p_project_id AND actor=p_actor;
END $$;

CREATE FUNCTION public.get_version_repository_gc_roots(p_project_id text)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
    SELECT coalesce(jsonb_agg(oid ORDER BY oid),'[]'::jsonb) FROM (
        SELECT target_oid AS oid FROM public.version_repository_refs WHERE project_id=p_project_id
        UNION SELECT old_state->>'oid' FROM public.version_reflog_entries WHERE project_id=p_project_id
        UNION SELECT new_state->>'oid' FROM public.version_reflog_entries WHERE project_id=p_project_id
        UNION SELECT jsonb_object_keys(r.roots) FROM public.version_publication_receipts r
            WHERE r.project_id=p_project_id AND r.expires_at > clock_timestamp()
    ) all_roots WHERE oid IS NOT NULL
$$;
REVOKE ALL ON FUNCTION public.get_version_repository_gc_roots(text) FROM PUBLIC,anon,authenticated,service_role;
GRANT EXECUTE ON FUNCTION public.get_version_repository_gc_roots(text) TO service_role;

-- Read authority is narrower than GC retention: uncommitted/rejected proposal
-- receipts are NOT readable roots. Reflog facts were actually published.
CREATE FUNCTION public.get_version_repository_published_roots(p_project_id text)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
    SELECT coalesce(jsonb_object_agg(r.oid,m.kind),'{}'::jsonb) FROM (
        SELECT target_oid AS oid FROM public.version_repository_refs WHERE project_id=p_project_id
        UNION SELECT old_state->>'oid' FROM public.version_reflog_entries WHERE project_id=p_project_id
        UNION SELECT new_state->>'oid' FROM public.version_reflog_entries WHERE project_id=p_project_id
    ) r LEFT JOIN public.version_repository_root_metadata m ON m.project_id=p_project_id AND m.oid=r.oid
    WHERE r.oid IS NOT NULL
$$;
REVOKE ALL ON FUNCTION public.get_version_repository_published_roots(text) FROM PUBLIC,anon,authenticated,service_role;
GRANT EXECUTE ON FUNCTION public.get_version_repository_published_roots(text) TO service_role;

CREATE FUNCTION public.begin_version_repository_gc(p_project_id text, p_token uuid)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE repo public.version_repositories%ROWTYPE; roots jsonb; snapshot jsonb;
BEGIN
    repo := public._version_lock_native_repository(p_project_id);
    IF p_token IS NULL THEN RAISE EXCEPTION 'invalid_gc_token' USING ERRCODE='22023'; END IF;
    IF NOT EXISTS (SELECT 1 FROM public.version_repository_refs WHERE project_id=p_project_id AND name=convert_to('HEAD','UTF8')) THEN
        RAISE EXCEPTION 'repository_metadata_incomplete' USING ERRCODE='55000';
    END IF;
    -- Never expire/take over a collector: a paused old process could resume an
    -- S3 DELETE after a new writer has published. Recovery requires quiescence.
    IF repo.gc_token IS NOT NULL THEN RAISE EXCEPTION 'repository_gc_in_progress' USING ERRCODE='55000'; END IF;
    IF EXISTS (SELECT 1 FROM public.version_object_pins WHERE project_id=p_project_id
               AND state <> 'released' AND expires_at > clock_timestamp()) THEN
        RAISE EXCEPTION 'publication_in_progress' USING ERRCODE='55000';
    END IF;
    UPDATE public.version_repositories SET gc_token=p_token,gc_epoch=gc_epoch+1
        WHERE project_id=p_project_id RETURNING * INTO repo;
    roots := public.get_version_repository_gc_roots(p_project_id);
    snapshot := jsonb_build_object('project_id',p_project_id,'token',p_token,'generation',repo.generation,
        'gc_epoch',repo.gc_epoch,'ref_sequence',repo.ref_sequence,'object_format',repo.object_format,'roots',roots);
    INSERT INTO public.version_repository_gc_runs(token,project_id,snapshot) VALUES (p_token,p_project_id,snapshot);
    RETURN snapshot;
END $$;

CREATE FUNCTION public.finish_version_repository_gc(p_project_id text, p_token uuid)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    PERFORM 1 FROM public.projects WHERE id=p_project_id FOR UPDATE;
    UPDATE public.version_repositories SET gc_token=NULL
        WHERE project_id=p_project_id AND gc_token=p_token;
    IF NOT FOUND THEN RAISE EXCEPTION 'gc_token_mismatch' USING ERRCODE='55000'; END IF;
    UPDATE public.version_repository_gc_runs SET finished_at=clock_timestamp()
        WHERE project_id=p_project_id AND token=p_token;
END $$;

CREATE FUNCTION public.get_version_repository_snapshot(p_project_id text)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
    SELECT to_jsonb(r) || jsonb_build_object('refs', coalesce((
        SELECT jsonb_agg(jsonb_build_object('name_b64',replace(encode(f.name,'base64'),E'\n',''),
            'state',CASE WHEN f.target_oid IS NOT NULL THEN jsonb_build_object('kind','oid','oid',f.target_oid)
                ELSE jsonb_build_object('kind','symbolic','target_b64',replace(encode(f.symbolic_target,'base64'),E'\n','')) END)
            || jsonb_build_object('kind',m.kind,'peeled_oid',m.peeled_oid)
            ORDER BY f.name) FROM public.version_repository_refs f
            LEFT JOIN public.version_repository_root_metadata m ON m.project_id=f.project_id AND m.oid=f.target_oid
            WHERE f.project_id=r.project_id
    ),'[]'::jsonb)) FROM public.version_repositories r WHERE r.project_id=p_project_id
$$;

CREATE FUNCTION public.begin_version_repository_read(p_project_id text,p_actor text,p_pin_id uuid)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE repo public.version_repositories%ROWTYPE;
BEGIN
    repo := public._version_lock_native_repository(p_project_id, false);
    -- During a sweep, publications cannot add roots: admission is fenced and
    -- the epoch rejects older receipts. Current refs are a subset of the sweep
    -- roots, so readers may proceed even after an uncertain orphan DELETE.
    -- This pin prevents a subsequent sweep from starting until copying ends.
    INSERT INTO public.version_object_pins(id,project_id,actor,object_format,generation,gc_epoch,purpose,roots,expires_at)
    VALUES(p_pin_id,p_project_id,p_actor,repo.object_format,repo.generation,repo.gc_epoch,'read','{}',clock_timestamp()+interval '30 minutes');
    RETURN public.get_version_repository_snapshot(p_project_id) || jsonb_build_object('pin_id',p_pin_id);
END $$;

ALTER TABLE public.version_repository_root_metadata ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.version_repository_root_metadata FROM PUBLIC,anon,authenticated,service_role;
GRANT SELECT ON TABLE public.version_repository_root_metadata TO service_role;
ALTER TABLE public.version_object_pins ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.version_repository_gc_runs ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.version_object_pins,public.version_repository_gc_runs FROM PUBLIC,anon,authenticated,service_role;
GRANT SELECT ON TABLE public.version_object_pins,public.version_repository_gc_runs TO service_role;
REVOKE ALL ON FUNCTION public._version_lock_native_repository(text,boolean) FROM PUBLIC,anon,authenticated,service_role;
REVOKE ALL ON FUNCTION public.begin_version_object_publication(text,text,uuid,bigint,jsonb),
    public.renew_version_object_publication(text,text,uuid),
    public.seal_version_object_publication(text,text,uuid,text,jsonb),
    public.release_version_object_publication(text,text,uuid),
    public.begin_version_repository_gc(text,uuid),public.finish_version_repository_gc(text,uuid),
    public.get_version_repository_snapshot(text),public.begin_version_repository_read(text,text,uuid) FROM PUBLIC,anon,authenticated,service_role;
GRANT EXECUTE ON FUNCTION public.begin_version_object_publication(text,text,uuid,bigint,jsonb),
    public.renew_version_object_publication(text,text,uuid),
    public.seal_version_object_publication(text,text,uuid,text,jsonb),
    public.release_version_object_publication(text,text,uuid),
    public.begin_version_repository_gc(text,uuid),public.finish_version_repository_gc(text,uuid),
    public.get_version_repository_snapshot(text),public.begin_version_repository_read(text,text,uuid) TO service_role;
COMMENT ON TABLE public.version_object_pins IS 'Backend publication/verification leases. Cache presence is never verification. Expired producers cannot seal.';
COMMENT ON TABLE public.version_repository_gc_runs IS 'Exclusive non-expiring sweep fences. Never release after uncertain/in-flight object deletion; recover only after worker AND storage/index I/O quiescence.';
NOTIFY pgrst, 'reload schema';
COMMIT;
