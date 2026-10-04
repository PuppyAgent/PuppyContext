-- ISSUE-062 Expand: fence delayed native location-index mutations.
-- No enrollment, activation, object rewrite, backfill or external I/O.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '2min';

-- The invoker identity is intentional: narrow SECURITY DEFINER RPCs below
-- validate the coordination token before mutating as the table owner. Direct
-- service-role writes remain compatible for legacy/shadow Projects only.
-- The database owner retains its existing repair/fixture authority.
CREATE FUNCTION public._version_fence_object_locations()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE project text; projects_to_guard text[];
BEGIN
    IF current_user <> pg_get_userbyid((SELECT relowner FROM pg_class
        WHERE oid='public.version_object_locations'::regclass)) THEN
        IF TG_OP='INSERT' THEN projects_to_guard := ARRAY[NEW.project_id];
        ELSIF TG_OP='DELETE' THEN projects_to_guard := ARRAY[OLD.project_id];
        ELSE projects_to_guard := ARRAY[OLD.project_id, NEW.project_id]; END IF;
        FOR project IN SELECT DISTINCT value FROM unnest(projects_to_guard) value ORDER BY value LOOP
            -- Order legacy mutations against a future Project cutover lock.
            -- Parent cascade deletion is not a new storage publication.
            PERFORM 1 FROM public.projects WHERE id=project FOR KEY SHARE;
            IF FOUND AND EXISTS (SELECT 1 FROM public.version_repositories
                                 WHERE project_id=project AND authority='native') THEN
                RAISE EXCEPTION 'native_object_location_coordination_required' USING ERRCODE='55000';
            END IF;
        END LOOP;
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
END $$;
CREATE TRIGGER zz_version_object_location_fence
    BEFORE INSERT OR UPDATE OR DELETE ON public.version_object_locations
    FOR EACH ROW EXECUTE FUNCTION public._version_fence_object_locations();
REVOKE ALL ON FUNCTION public._version_fence_object_locations() FROM PUBLIC, anon, authenticated, service_role;

CREATE FUNCTION public.register_version_object_locations(
    p_project_id text, p_actor text, p_pin_id uuid, p_rows jsonb
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE repo public.version_repositories%ROWTYPE; pin public.version_object_pins%ROWTYPE;
    item jsonb; oid text; key text; prefix text; suffix text; affected integer;
BEGIN
    repo := public._version_lock_native_repository(p_project_id);
    SELECT * INTO pin FROM public.version_object_pins WHERE id=p_pin_id FOR UPDATE;
    IF NOT FOUND OR pin.project_id IS DISTINCT FROM p_project_id OR pin.actor IS DISTINCT FROM p_actor
       OR pin.purpose <> 'publication' OR pin.state <> 'uploading'
       OR pin.object_format <> repo.object_format OR pin.generation <> repo.generation
       OR pin.gc_epoch <> repo.gc_epoch OR pin.expires_at <= clock_timestamp()
       OR repo.gc_token IS NOT NULL THEN
        RAISE EXCEPTION 'publication_pin_unavailable' USING ERRCODE='55000';
    END IF;
    IF jsonb_typeof(p_rows) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION 'invalid_object_locations' USING ERRCODE='22023';
    END IF;
    IF jsonb_array_length(p_rows) NOT BETWEEN 1 AND 200 OR
       (SELECT count(DISTINCT value->>'object_id') FROM jsonb_array_elements(p_rows)) <> jsonb_array_length(p_rows) THEN
        RAISE EXCEPTION 'invalid_object_locations' USING ERRCODE='22023';
    END IF;
    FOR item IN SELECT value FROM jsonb_array_elements(p_rows) LOOP
        oid := item->>'object_id'; key := item->>'pack_key';
        IF item->>'project_id' IS DISTINCT FROM p_project_id
           OR public._version_oid_valid(oid, repo.object_format) IS DISTINCT FROM true
           OR jsonb_typeof(item->'offset_bytes') IS DISTINCT FROM 'number'
           OR jsonb_typeof(item->'size_bytes') IS DISTINCT FROM 'number'
           OR (item->>'offset_bytes')::bigint < 0 OR (item->>'size_bytes')::bigint <= 0 THEN
            RAISE EXCEPTION 'invalid_object_locations' USING ERRCODE='22023';
        END IF;
        IF left(key,8)='chunked:' THEN
            prefix := 'chunked:version/' || p_project_id || '/object-bundles/chunked/' ||
                      left(oid,2) || '/' || oid || '/manifest-';
            suffix := substring(key FROM length(prefix)+1);
            IF left(key,length(prefix)) IS DISTINCT FROM prefix OR suffix !~ '^[0-9a-f]{64}\.json$'
               OR (item->>'offset_bytes')::bigint <> 0 THEN
                RAISE EXCEPTION 'noncanonical_object_location' USING ERRCODE='22023';
            END IF;
        ELSE
            prefix := 'version/' || p_project_id || '/object-bundles/';
            suffix := substring(key FROM length(prefix)+1);
            IF left(key,length(prefix)) IS DISTINCT FROM prefix OR suffix !~ '^[0-9a-f]{2}/[0-9a-f]{64}\.pob$'
               OR left(suffix,2) IS DISTINCT FROM substring(suffix FROM 4 FOR 2) THEN
                RAISE EXCEPTION 'noncanonical_object_location' USING ERRCODE='22023';
            END IF;
        END IF;
    END LOOP;
    -- The trusted backend physically verifies these exact immutable placements
    -- before this RPC. The lock/pin check prevents a queued old request from
    -- installing a location collected in a newer epoch. No network I/O here.
    INSERT INTO public.version_object_locations(project_id,object_id,pack_key,offset_bytes,size_bytes)
    SELECT p_project_id,r.object_id,r.pack_key,r.offset_bytes,r.size_bytes
    FROM jsonb_to_recordset(p_rows) AS r(object_id text,pack_key text,offset_bytes bigint,size_bytes bigint)
    ORDER BY r.object_id
    ON CONFLICT (project_id,object_id) DO UPDATE SET pack_key=EXCLUDED.pack_key,
        offset_bytes=EXCLUDED.offset_bytes,size_bytes=EXCLUDED.size_bytes;
    GET DIAGNOSTICS affected = ROW_COUNT;
    RETURN jsonb_build_object('registered',affected);
END $$;

CREATE FUNCTION public.remove_version_object_locations(
    p_project_id text, p_gc_token uuid, p_object_ids text[]
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE repo public.version_repositories%ROWTYPE; affected integer;
BEGIN
    repo := public._version_lock_native_repository(p_project_id, false);
    IF p_gc_token IS NULL OR repo.gc_token IS DISTINCT FROM p_gc_token THEN
        RAISE EXCEPTION 'repository_gc_token_mismatch' USING ERRCODE='55000';
    END IF;
    IF cardinality(p_object_ids) IS NULL OR cardinality(p_object_ids) NOT BETWEEN 1 AND 200
       OR EXISTS (SELECT 1 FROM unnest(p_object_ids) oid
                  WHERE public._version_oid_valid(oid, repo.object_format) IS DISTINCT FROM true) THEN
        RAISE EXCEPTION 'invalid_object_locations' USING ERRCODE='22023';
    END IF;
    DELETE FROM public.version_object_locations WHERE project_id=p_project_id AND object_id=ANY(p_object_ids);
    GET DIAGNOSTICS affected = ROW_COUNT;
    RETURN jsonb_build_object('removed',affected);
END $$;

-- Check BEFORE physical DELETE, not just when its index row is removed.
-- A copied old context cannot initiate another deletion after GC finishes.
CREATE FUNCTION public.authorize_version_object_deletion(p_project_id text, p_gc_token uuid DEFAULT NULL)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE repo public.version_repositories%ROWTYPE;
BEGIN
    PERFORM 1 FROM public.projects WHERE id=p_project_id FOR KEY SHARE;
    IF NOT FOUND THEN RAISE EXCEPTION 'repository_unavailable' USING ERRCODE='55000'; END IF;
    SELECT * INTO repo FROM public.version_repositories WHERE project_id=p_project_id FOR SHARE;
    IF NOT FOUND THEN RETURN true; END IF; -- Existing legacy collector.
    IF repo.authority <> 'native' OR p_gc_token IS NULL OR repo.gc_token IS DISTINCT FROM p_gc_token THEN
        RAISE EXCEPTION 'repository_gc_token_mismatch' USING ERRCODE='55000';
    END IF;
    RETURN true;
END $$;
REVOKE ALL ON FUNCTION public.authorize_version_object_deletion(text,uuid) FROM PUBLIC, anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.authorize_version_object_deletion(text,uuid) TO service_role;

REVOKE ALL ON FUNCTION public.register_version_object_locations(text,text,uuid,jsonb) FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON FUNCTION public.remove_version_object_locations(text,uuid,text[]) FROM PUBLIC, anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.register_version_object_locations(text,text,uuid,jsonb) TO service_role;
GRANT EXECUTE ON FUNCTION public.remove_version_object_locations(text,uuid,text[]) TO service_role;
NOTIFY pgrst, 'reload schema';
COMMIT;
