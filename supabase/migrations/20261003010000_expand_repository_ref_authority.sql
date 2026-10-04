-- ISSUE-062 / Expand only. No repository activation, backfill or S3 operation.
-- Empty, backend-only authority foundation. Runtime consumers and the receipt
-- issuer remain disconnected until storage/GC and migration gates pass.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '2min';

CREATE FUNCTION public._version_ref_name_valid(p_name bytea)
RETURNS boolean LANGUAGE plpgsql IMMUTABLE STRICT SET search_path = pg_catalog AS $$
DECLARE
    value text;
    component text;
    i integer;
    b integer;
BEGIN
    IF p_name = decode('48454144', 'hex') THEN RETURN true; END IF;
    IF octet_length(p_name) NOT BETWEEN 6 AND 1024 THEN RETURN false; END IF;
    FOR i IN 0..octet_length(p_name)-1 LOOP
        b := get_byte(p_name, i);
        IF b <= 32 OR b = ANY(ARRAY[127,126,94,58,63,42,91,92]) THEN RETURN false; END IF;
    END LOOP;
    -- LATIN1 is a lossless inspection mapping, NOT the stored representation.
    value := convert_from(p_name, 'LATIN1');
    IF left(value, 5) <> 'refs/' OR right(value, 1) IN ('/', '.')
       OR position('..' IN value) > 0 OR position('@{' IN value) > 0 THEN
        RETURN false;
    END IF;
    FOREACH component IN ARRAY string_to_array(value, '/') LOOP
        IF component = '' OR left(component, 1) = '.' OR right(component, 5) = '.lock' THEN
            RETURN false;
        END IF;
    END LOOP;
    RETURN true;
END $$;

CREATE FUNCTION public._version_oid_valid(p_oid text, p_format text)
RETURNS boolean LANGUAGE sql IMMUTABLE SET search_path = pg_catalog AS $$
    SELECT coalesce(p_oid ~ '^[0-9a-f]+$' AND p_oid !~ '^0+$'
        AND ((p_format = 'sha1' AND length(p_oid) = 40)
          OR (p_format = 'sha256' AND length(p_oid) = 64)), false)
$$;

CREATE FUNCTION public._version_ref_state_valid(p_state jsonb, p_format text)
RETURNS boolean LANGUAGE plpgsql IMMUTABLE SET search_path = pg_catalog AS $$
DECLARE target bytea;
BEGIN
    IF p_state = '{"kind":"absent"}'::jsonb THEN RETURN true; END IF;
    IF p_state->>'kind' = 'oid' THEN
        RETURN coalesce(p_state = jsonb_build_object('kind','oid','oid',p_state->>'oid')
            AND public._version_oid_valid(p_state->>'oid', p_format), false);
    END IF;
    IF p_state->>'kind' = 'symbolic' THEN
        target := decode(p_state->>'target_b64', 'base64');
        RETURN coalesce(p_state = jsonb_build_object('kind','symbolic','target_b64',
                replace(encode(target,'base64'), E'\n', ''))
            AND public._version_ref_name_valid(target)
            AND substring(target FROM 1 FOR 11) = convert_to('refs/heads/','UTF8'), false);
    END IF;
    RETURN false;
EXCEPTION WHEN invalid_parameter_value OR invalid_text_representation THEN
    RETURN false;
END $$;

CREATE FUNCTION public._version_receipt_roots_valid(p_roots jsonb, p_format text)
RETURNS boolean LANGUAGE plpgsql IMMUTABLE SET search_path = pg_catalog AS $$
DECLARE entry record;
BEGIN
    IF jsonb_typeof(p_roots) IS DISTINCT FROM 'object' OR p_roots = '{}'::jsonb THEN
        RETURN false;
    END IF;
    FOR entry IN SELECT * FROM jsonb_each(p_roots) LOOP
        IF NOT public._version_oid_valid(entry.key, p_format)
           OR entry.value NOT IN ('"blob"'::jsonb,'"tree"'::jsonb,'"commit"'::jsonb,'"tag"'::jsonb) THEN
            RETURN false;
        END IF;
    END LOOP;
    RETURN true;
END $$;

CREATE TABLE public.version_repositories (
    project_id text PRIMARY KEY REFERENCES public.projects(id) ON DELETE CASCADE,
    object_format text NOT NULL DEFAULT 'sha1' CHECK (object_format IN ('sha1','sha256')),
    authority text NOT NULL DEFAULT 'shadow' CHECK (authority IN ('shadow','native')),
    write_state text NOT NULL DEFAULT 'active' CHECK (write_state IN ('active','fenced')),
    generation bigint NOT NULL DEFAULT 1 CHECK (generation > 0),
    gc_epoch bigint NOT NULL DEFAULT 1 CHECK (gc_epoch > 0),
    ref_sequence bigint NOT NULL DEFAULT 0 CHECK (ref_sequence >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, object_format)
);

CREATE TABLE public.version_repository_refs (
    project_id text NOT NULL,
    name bytea NOT NULL CHECK (public._version_ref_name_valid(name)),
    object_format text NOT NULL,
    target_oid text,
    symbolic_target bytea,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, name),
    FOREIGN KEY (project_id, object_format)
        REFERENCES public.version_repositories(project_id, object_format) ON DELETE CASCADE,
    CHECK ((target_oid IS NOT NULL)::integer + (symbolic_target IS NOT NULL)::integer = 1),
    CHECK (target_oid IS NULL OR public._version_oid_valid(target_oid, object_format)),
    CHECK (symbolic_target IS NULL OR (
        name = decode('48454144','hex') AND public._version_ref_name_valid(symbolic_target)
        AND substring(symbolic_target FROM 1 FOR 11) = convert_to('refs/heads/','UTF8')))
);

CREATE TABLE public.version_publication_receipts (
    id uuid PRIMARY KEY,
    project_id text NOT NULL,
    object_format text NOT NULL,
    generation bigint NOT NULL CHECK (generation > 0),
    gc_epoch bigint NOT NULL CHECK (gc_epoch > 0),
    roots jsonb NOT NULL CHECK (public._version_receipt_roots_valid(roots, object_format)),
    manifest_sha256 text NOT NULL CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
    verified_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    FOREIGN KEY (project_id, object_format)
        REFERENCES public.version_repositories(project_id, object_format) ON DELETE CASCADE,
    CHECK (expires_at > verified_at AND expires_at <= verified_at + interval '1 hour')
);
CREATE INDEX version_publication_receipts_retention_idx
    ON public.version_publication_receipts(project_id, expires_at);

CREATE TABLE public.version_ref_transactions (
    id uuid PRIMARY KEY,
    project_id text NOT NULL REFERENCES public.version_repositories(project_id) ON DELETE CASCADE,
    actor text NOT NULL CHECK (length(actor) BETWEEN 1 AND 512),
    request_key uuid NOT NULL,
    request_sha256 text NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
    result jsonb NOT NULL CHECK (result->>'status' IN ('committed','rejected')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, actor, request_key),
    UNIQUE (id, project_id)
);

CREATE TABLE public.version_reflog_entries (
    transaction_id uuid NOT NULL,
    project_id text NOT NULL,
    name bytea NOT NULL,
    old_state jsonb NOT NULL,
    new_state jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (transaction_id, name),
    FOREIGN KEY (transaction_id, project_id)
        REFERENCES public.version_ref_transactions(id, project_id) ON DELETE CASCADE
);
CREATE INDEX version_reflog_entries_project_idx ON public.version_reflog_entries(project_id, created_at);

CREATE TABLE public.version_ref_events (
    transaction_id uuid PRIMARY KEY,
    project_id text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz,
    FOREIGN KEY (transaction_id, project_id)
        REFERENCES public.version_ref_transactions(id, project_id) ON DELETE CASCADE
);
CREATE INDEX version_ref_events_pending_idx ON public.version_ref_events(created_at)
    WHERE processed_at IS NULL;

CREATE FUNCTION public.apply_version_ref_transaction(
    p_project_id text, p_actor text, p_request_key uuid, p_generation bigint,
    p_updates jsonb, p_receipt_id uuid DEFAULT NULL, p_message text DEFAULT ''
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
    repo public.version_repositories%ROWTYPE;
    receipt public.version_publication_receipts%ROWTYPE;
    previous public.version_ref_transactions%ROWTYPE;
    lifecycle text;
    request_hash text;
    transaction_id uuid := gen_random_uuid();
    item jsonb;
    ref_name bytea;
    names bytea[] := ARRAY[]::bytea[];
    observed jsonb;
    desired jsonb;
    states jsonb := '[]'::jsonb;
    reason text;
    changed boolean := false;
    needs_receipt boolean := false;
    result jsonb;
BEGIN
    IF p_request_key IS NULL OR p_generation IS NULL OR p_generation < 1
       OR p_actor IS NULL OR length(p_actor) NOT BETWEEN 1 AND 512
       OR p_message IS NULL OR octet_length(p_message) > 8192
       OR jsonb_typeof(p_updates) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION 'invalid_ref_request' USING ERRCODE = '22023';
    END IF;
    IF jsonb_array_length(p_updates) NOT BETWEEN 1 AND 256 THEN
        RAISE EXCEPTION 'invalid_ref_request' USING ERRCODE = '22023';
    END IF;
    request_hash := encode(sha256(convert_to(jsonb_build_object(
        'generation', p_generation, 'updates', p_updates,
        'receipt', p_receipt_id, 'message', p_message)::text, 'UTF8')), 'hex');

    -- Shared lifecycle ordering. No object I/O occurs while these locks are held.
    SELECT lifecycle_status INTO lifecycle FROM public.projects
        WHERE id = p_project_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'repository_unavailable' USING ERRCODE = '55000'; END IF;
    SELECT * INTO repo FROM public.version_repositories
        WHERE project_id = p_project_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'repository_unavailable' USING ERRCODE = '55000'; END IF;
    SELECT * INTO previous FROM public.version_ref_transactions
        WHERE project_id = p_project_id AND actor = p_actor AND request_key = p_request_key;
    IF FOUND THEN
        IF previous.request_sha256 <> request_hash THEN
            RAISE EXCEPTION 'request_key_reused' USING ERRCODE = '22023';
        END IF;
        RETURN previous.result;
    END IF;
    IF lifecycle <> 'ready' OR repo.authority <> 'native' OR repo.write_state <> 'active' THEN
        RAISE EXCEPTION 'repository_unavailable' USING ERRCODE = '55000';
    END IF;
    IF repo.generation <> p_generation THEN
        RAISE EXCEPTION 'generation_mismatch' USING ERRCODE = '55000';
    END IF;

    FOR item IN SELECT value FROM jsonb_array_elements(p_updates) LOOP
        IF jsonb_typeof(item) IS DISTINCT FROM 'object'
           OR NOT item ? 'expected' OR (item - ARRAY['name_b64','expected','new']) <> '{}'::jsonb THEN
            RAISE EXCEPTION 'invalid_ref_request' USING ERRCODE = '22023';
        END IF;
        BEGIN
            ref_name := decode(item->>'name_b64', 'base64');
        EXCEPTION WHEN invalid_parameter_value OR invalid_text_representation THEN
            RAISE EXCEPTION 'invalid_ref_name' USING ERRCODE = '22023';
        END;
        IF ref_name IS NULL OR NOT public._version_ref_name_valid(ref_name)
           OR item->'name_b64' <> to_jsonb(replace(encode(ref_name,'base64'), E'\n', '')) THEN
            RAISE EXCEPTION 'invalid_ref_name' USING ERRCODE = '22023';
        END IF;
        IF ref_name = ANY(names) THEN RAISE EXCEPTION 'duplicate_ref' USING ERRCODE = '22023'; END IF;
        names := array_append(names, ref_name);
        desired := CASE WHEN item ? 'new' THEN item->'new' ELSE item->'expected' END;
        IF NOT public._version_ref_state_valid(item->'expected', repo.object_format)
           OR NOT public._version_ref_state_valid(desired, repo.object_format)
           OR (ref_name <> decode('48454144','hex') AND (
               item->'expected'->>'kind' = 'symbolic' OR desired->>'kind' = 'symbolic'))
           OR (ref_name = decode('48454144','hex') AND desired->>'kind' = 'absent') THEN
            RAISE EXCEPTION 'invalid_ref_state' USING ERRCODE = '22023';
        END IF;
        SELECT CASE WHEN r.target_oid IS NOT NULL
            THEN jsonb_build_object('kind','oid','oid',r.target_oid)
            ELSE jsonb_build_object('kind','symbolic','target_b64',
                 replace(encode(r.symbolic_target,'base64'), E'\n', '')) END
          INTO observed FROM public.version_repository_refs r
          WHERE r.project_id = p_project_id AND r.name = ref_name;
        IF NOT FOUND THEN observed := '{"kind":"absent"}'::jsonb; END IF;
        IF observed <> item->'expected' THEN reason := 'stale_ref'; END IF;
        states := states || jsonb_build_array(jsonb_build_object(
            'name_b64', item->>'name_b64', 'observed', observed, 'new', desired,
            'changed', item ? 'new' AND desired <> observed));
    END LOOP;

    IF reason IS NULL THEN
        -- Compare the final namespace, permitting an atomic parent deletion plus
        -- child creation. HEAD is stored without dereferencing and has no slash.
        IF NOT EXISTS (
            SELECT 1 FROM public.version_repository_refs r
            WHERE r.project_id = p_project_id AND r.name = decode('48454144','hex')
              AND NOT r.name = ANY(names)
            UNION ALL
            SELECT 1 FROM jsonb_array_elements(states) s
            WHERE decode(s->>'name_b64','base64') = decode('48454144','hex')
              AND s->'new'->>'kind' <> 'absent'
        ) THEN reason := 'missing_head';
        ELSIF EXISTS (
            WITH final_names AS (
                SELECT r.name FROM public.version_repository_refs r
                WHERE r.project_id = p_project_id AND NOT r.name = ANY(names)
                UNION ALL
                SELECT decode(s->>'name_b64','base64') FROM jsonb_array_elements(states) s
                WHERE s->'new'->>'kind' <> 'absent'
            )
            SELECT 1 FROM final_names a JOIN final_names b
              ON substring(b.name FROM 1 FOR octet_length(a.name)+1) = a.name || decode('2f','hex')
        ) THEN reason := 'ref_namespace_conflict'; END IF;
    END IF;

    IF reason IS NULL THEN
        SELECT coalesce(bool_or((s->>'changed')::boolean), false),
               coalesce(bool_or((s->>'changed')::boolean AND s->'new'->>'kind' = 'oid'), false)
          INTO changed, needs_receipt FROM jsonb_array_elements(states) s;
        IF needs_receipt THEN
            SELECT * INTO receipt FROM public.version_publication_receipts
                WHERE id = p_receipt_id AND project_id = p_project_id FOR SHARE;
            IF NOT FOUND OR receipt.object_format <> repo.object_format
               OR receipt.generation <> repo.generation OR receipt.gc_epoch <> repo.gc_epoch
               OR receipt.verified_at > clock_timestamp() OR receipt.expires_at <= clock_timestamp() THEN
                RAISE EXCEPTION 'invalid_receipt' USING ERRCODE = '55000';
            END IF;
            FOR item IN SELECT value FROM jsonb_array_elements(states) LOOP
                IF NOT (item->>'changed')::boolean OR item->'new'->>'kind' <> 'oid' THEN CONTINUE; END IF;
                ref_name := decode(item->>'name_b64','base64');
                IF NOT receipt.roots ? (item->'new'->>'oid') THEN
                    RAISE EXCEPTION 'unverified_target' USING ERRCODE = '55000';
                END IF;
                IF (ref_name = decode('48454144','hex')
                    OR substring(ref_name FROM 1 FOR 11) = convert_to('refs/heads/','UTF8'))
                   AND receipt.roots->>(item->'new'->>'oid') <> 'commit' THEN
                    RAISE EXCEPTION 'invalid_target_type' USING ERRCODE = '22023';
                END IF;
            END LOOP;
        END IF;
    ELSE
        SELECT jsonb_agg(s || '{"changed":false}'::jsonb) INTO states FROM jsonb_array_elements(states) s;
    END IF;

    IF changed THEN
        UPDATE public.version_repositories SET ref_sequence = ref_sequence + 1
            WHERE project_id = p_project_id RETURNING ref_sequence INTO repo.ref_sequence;
    END IF;
    result := jsonb_build_object('transaction_id', transaction_id, 'project_id', p_project_id,
        'status', CASE WHEN reason IS NULL THEN 'committed' ELSE 'rejected' END,
        'reason', reason, 'generation', repo.generation, 'ref_sequence', repo.ref_sequence,
        'receipt_id', CASE WHEN needs_receipt THEN p_receipt_id ELSE NULL END, 'refs', states);
    INSERT INTO public.version_ref_transactions(id, project_id, actor, request_key, request_sha256, result)
        VALUES (transaction_id, p_project_id, p_actor, p_request_key, request_hash, result);

    IF changed THEN
        FOR item IN SELECT value FROM jsonb_array_elements(states) LOOP
            IF NOT (item->>'changed')::boolean THEN CONTINUE; END IF;
            ref_name := decode(item->>'name_b64','base64');
            desired := item->'new';
            IF desired->>'kind' = 'absent' THEN
                DELETE FROM public.version_repository_refs WHERE project_id = p_project_id AND name = ref_name;
            ELSE
                INSERT INTO public.version_repository_refs(project_id, name, object_format, target_oid, symbolic_target)
                    VALUES (p_project_id, ref_name, repo.object_format, desired->>'oid', decode(desired->>'target_b64','base64'))
                    ON CONFLICT (project_id, name) DO UPDATE SET target_oid = EXCLUDED.target_oid,
                        symbolic_target = EXCLUDED.symbolic_target, updated_at = now();
            END IF;
            INSERT INTO public.version_reflog_entries(transaction_id, project_id, name, old_state, new_state)
                VALUES (transaction_id, p_project_id, ref_name, item->'observed', desired);
        END LOOP;
        INSERT INTO public.audit_logs(action, operator_type, operator_id, project_id, status, metadata)
            VALUES ('repository_refs_changed', CASE
                        WHEN p_actor LIKE 'user:%' THEN 'user'
                        WHEN p_actor LIKE 'agent:%' THEN 'agent'
                        WHEN p_actor LIKE 'sync:%' THEN 'sync'
                        ELSE 'system' END, p_actor, p_project_id, 'committed',
                    jsonb_build_object('ref_transaction_id',transaction_id,'message',p_message,'refs',states));
        INSERT INTO public.version_ref_events(transaction_id, project_id, payload)
            VALUES (transaction_id, p_project_id, jsonb_build_object(
                'schema_version',1,'event_type','repository_refs_changed','result',result));
    END IF;
    RETURN result;
END $$;

CREATE FUNCTION public.get_version_ref_transaction(p_project_id text, p_actor text, p_request_key uuid)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
    SELECT result FROM public.version_ref_transactions
    WHERE project_id = p_project_id AND actor = p_actor AND request_key = p_request_key
$$;

-- No old publisher may acknowledge writes to a second authority after cutover.
-- These triggers are inert for all existing repositories (metadata is empty).
CREATE FUNCTION public._version_fence_legacy_publication()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE
    project text;
    guarded_projects text[];
BEGIN
    IF TG_TABLE_NAME = 'projects' THEN
        IF NEW.version_root_hash IS NOT DISTINCT FROM OLD.version_root_hash
           AND NEW.mut_root_hash IS NOT DISTINCT FROM OLD.mut_root_hash THEN RETURN NEW; END IF;
        guarded_projects := ARRAY[OLD.id, NEW.id];
    ELSIF TG_OP = 'UPDATE' THEN guarded_projects := ARRAY[OLD.project_id, NEW.project_id];
    ELSIF TG_OP = 'DELETE' THEN guarded_projects := ARRAY[OLD.project_id];
    ELSE guarded_projects := ARRAY[NEW.project_id];
    END IF;
    -- Guard both sides of row reparenting; NEW alone permits escape from a
    -- native repository. Deterministic ordering also covers cross-Project moves.
    FOR project IN SELECT DISTINCT value FROM unnest(guarded_projects) value ORDER BY value LOOP
        PERFORM 1 FROM public.projects WHERE id = project FOR UPDATE;
        -- Parent deletion may cascade through old history; it is not publication.
        IF FOUND AND EXISTS (SELECT 1 FROM public.version_repositories
                             WHERE project_id = project AND authority = 'native') THEN
            RAISE EXCEPTION 'legacy_repository_publication_fenced' USING ERRCODE = '55000';
        END IF;
    END LOOP;
    IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
END $$;

CREATE TRIGGER zz_version_repository_root_fence
    BEFORE UPDATE OF version_root_hash, mut_root_hash ON public.projects
    FOR EACH ROW EXECUTE FUNCTION public._version_fence_legacy_publication();
CREATE TRIGGER version_repository_scope_fence BEFORE INSERT OR UPDATE OR DELETE ON public.version_scope_state
    FOR EACH ROW EXECUTE FUNCTION public._version_fence_legacy_publication();
CREATE TRIGGER version_repository_commit_fence BEFORE INSERT OR UPDATE OR DELETE ON public.version_commits
    FOR EACH ROW EXECUTE FUNCTION public._version_fence_legacy_publication();
CREATE TRIGGER version_repository_old_ref_fence BEFORE INSERT OR UPDATE OR DELETE ON public.version_refs
    FOR EACH ROW EXECUTE FUNCTION public._version_fence_legacy_publication();

ALTER TABLE public.version_repositories ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.version_repository_refs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.version_publication_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.version_ref_transactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.version_reflog_entries ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.version_ref_events ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.version_repositories, public.version_repository_refs,
    public.version_publication_receipts, public.version_ref_transactions,
    public.version_reflog_entries, public.version_ref_events FROM PUBLIC, anon, authenticated, service_role;
GRANT SELECT ON TABLE public.version_repositories, public.version_repository_refs,
    public.version_publication_receipts, public.version_ref_transactions,
    public.version_reflog_entries, public.version_ref_events TO service_role;

REVOKE ALL ON FUNCTION public._version_ref_name_valid(bytea), public._version_oid_valid(text,text),
    public._version_ref_state_valid(jsonb,text), public._version_receipt_roots_valid(jsonb,text),
    public._version_fence_legacy_publication(),
    public.apply_version_ref_transaction(text,text,uuid,bigint,jsonb,uuid,text),
    public.get_version_ref_transaction(text,text,uuid) FROM PUBLIC, anon, authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.apply_version_ref_transaction(text,text,uuid,bigint,jsonb,uuid,text),
    public.get_version_ref_transaction(text,text,uuid) TO service_role;

COMMENT ON TABLE public.version_repositories IS
    'ISSUE-062 dormant authority expansion; only reviewed migration may activate after storage/GC/consumer gates.';
COMMENT ON TABLE public.version_publication_receipts IS
    'Owner-issued closure metadata prerequisite; no runtime issuer until durable S3 and GC coordination are verified.';
NOTIFY pgrst, 'reload schema';
COMMIT;
