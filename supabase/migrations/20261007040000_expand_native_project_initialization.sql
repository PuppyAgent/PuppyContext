-- Native empty and seeded Projects share one repository initialization path.
BEGIN;
CREATE OR REPLACE FUNCTION public.create_native_project_repository(
    p_operation_key text, p_payload_hash text, p_project_id text, p_name text,
    p_description text, p_org_id text, p_created_by uuid, p_share_token text,
    p_publication_mode text, p_project_limit integer DEFAULT NULL,
    p_request_hash text DEFAULT NULL, p_result_metadata jsonb DEFAULT '{}'::jsonb,
    p_object_format text DEFAULT 'sha1', p_default_branch text DEFAULT 'main'
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE outcome jsonb; project_row public.projects%ROWTYPE; head bytea;
BEGIN
    head:=convert_to('refs/heads/'||p_default_branch,'UTF8');
    IF p_publication_mode IS NULL OR p_publication_mode NOT IN ('empty','deferred')
       OR p_object_format IS NULL OR p_object_format NOT IN ('sha1','sha256')
       OR p_default_branch IS NULL OR p_default_branch='HEAD' OR left(p_default_branch,1)='-'
       OR NOT public._version_ref_name_valid(head) THEN
        RETURN jsonb_build_object('outcome','invalid');
    END IF;
    -- The existing coordinator owns membership, quota, operation identity and
    -- creator Admin. This wrapper publishes the empty native aggregate in the
    -- SAME transaction: a crash cannot leave a legacy-initializable Project.
    PERFORM pg_advisory_xact_lock(hashtextextended(p_org_id,0));
    PERFORM pg_advisory_xact_lock(hashtextextended(p_org_id || ':storage.logical_bytes',0));
    outcome:=public.create_project_idempotent(
        p_operation_key,p_payload_hash,p_project_id,p_name,p_description,p_org_id,
        p_created_by,p_share_token,p_publication_mode,p_project_limit,p_request_hash,p_result_metadata);
    IF outcome->>'outcome' IS DISTINCT FROM 'initializing_created' THEN RETURN outcome; END IF;

    -- Never manufacture billing entitlements or reset an existing counter.
    -- An absent nonempty legacy baseline requires the existing reconciler.
    PERFORM public._version_file_policy_limit(p_org_id);
    IF NOT EXISTS (SELECT 1 FROM public.organization_usage_counters
                   WHERE org_id=p_org_id AND metric='storage.logical_bytes' AND version>0) THEN
        IF EXISTS(SELECT 1 FROM public.projects WHERE org_id=p_org_id AND id<>p_project_id
                  AND coalesce(version_root_hash,'') NOT IN ('','4b825dc642cb6eb9a060e54bf8d69288fbee4904'))
           OR EXISTS(SELECT 1 FROM public.version_repository_billing WHERE org_id=p_org_id) THEN
            RAISE EXCEPTION 'storage_billing_usage_uninitialized' USING ERRCODE='55000';
        END IF;
        INSERT INTO public.organization_usage_counters(org_id,metric,value,version)
            VALUES(p_org_id,'storage.logical_bytes',0,1);
    END IF;
    INSERT INTO public.version_organization_capacity(org_id,initialized,max_body_bytes,max_objects)
        VALUES(p_org_id,true,34359738368,4000000) ON CONFLICT(org_id) DO NOTHING;
    IF NOT EXISTS(SELECT 1 FROM public.version_organization_capacity WHERE org_id=p_org_id AND initialized) THEN
        RAISE EXCEPTION 'repository_capacity_uninitialized' USING ERRCODE='55000';
    END IF;
    INSERT INTO public.version_repositories(project_id,authority,object_format)
        VALUES(p_project_id,'native',p_object_format);
    INSERT INTO public.version_repository_refs(project_id,name,object_format,symbolic_target)
        VALUES(p_project_id,convert_to('HEAD','UTF8'),p_object_format,head);
    INSERT INTO public.version_repository_capacity(project_id,org_id,initialized,max_body_bytes,max_objects)
        VALUES(p_project_id,p_org_id,true,268435456,100000);
    INSERT INTO public.version_repository_billing(project_id,org_id,initialized,accounted_bytes) VALUES(p_project_id,p_org_id,true,0);
    INSERT INTO public.version_repository_file_policies(project_id,initialized) VALUES(p_project_id,true);
    UPDATE public.projects SET bound_git_branch=p_default_branch WHERE id=p_project_id;
    IF p_publication_mode='deferred' THEN RETURN outcome; END IF;
    UPDATE public.projects SET lifecycle_status='ready',bound_git_branch=p_default_branch,updated_at=now()
        WHERE id=p_project_id AND lifecycle_status='initializing' RETURNING * INTO project_row;
    IF NOT FOUND THEN RAISE EXCEPTION 'native_creation_lifecycle_conflict'; END IF;
    UPDATE public.project_create_operations SET status='ready',project_snapshot=to_jsonb(project_row),
        ready_at=now(),initialization_claimed_at=NULL,initialization_claimed_by=NULL,
        initialization_last_error=NULL
        WHERE actor_user_id=p_created_by AND operation_key=p_operation_key;
    RETURN jsonb_build_object('outcome','native_created','project',to_jsonb(project_row));
END $$;

CREATE OR REPLACE FUNCTION public._version_lock_admission_project(p_project_id text)
RETURNS public.projects LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE project public.projects%ROWTYPE; selected_org text;
BEGIN
    SELECT org_id INTO selected_org FROM public.projects WHERE id=p_project_id;
    IF NOT FOUND THEN RAISE EXCEPTION 'repository_unavailable' USING ERRCODE='55000'; END IF;
    PERFORM 1 FROM public.organizations WHERE id=selected_org FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'repository_unavailable' USING ERRCODE='55000'; END IF;
    SELECT * INTO project FROM public.projects WHERE id=p_project_id FOR UPDATE;
    IF NOT FOUND OR project.org_id IS DISTINCT FROM selected_org OR project.lifecycle_status NOT IN ('ready','initializing') THEN
        RAISE EXCEPTION 'repository_unavailable' USING ERRCODE='55000';
    END IF;
    RETURN project;
END $$;

CREATE OR REPLACE FUNCTION public._version_assert_current_repository_actor(p_project_id text,p_org_id text,p_actor text,p_write boolean)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE subject_user_id uuid; hint public.access_surface_credentials%ROWTYPE;
    credential public.access_surface_credentials%ROWTYPE; allowed boolean; resolved record;
BEGIN
    -- Call only after Organization -> Project locking. Lock membership before
    -- Surface -> credential, matching membership/surface deletion cascades.
    -- The platform's canonical resolvers still decide roles and narrowed modes.
    IF p_actor LIKE 'initialize:%' THEN
        -- Only the already admitted initializer lease may see/write the hidden
        -- repository. Human and Runtime credentials remain denied until ready.
        IF NOT EXISTS(SELECT 1 FROM public.projects p
            JOIN public.project_create_operations o ON o.project_id=p.id AND o.actor_user_id=p.created_by
            JOIN public.project_write_leases l ON l.project_id=p.id
            JOIN public.org_members om ON om.org_id=p.org_id AND om.user_id=o.actor_user_id
            JOIN public.project_members pm ON pm.project_id=p.id AND pm.user_id=o.actor_user_id AND pm.org_id=p.org_id
            WHERE p.id=p_project_id AND p.org_id=p_org_id AND p.lifecycle_status='initializing'
              AND o.status='initializing' AND l.id::text=substring(p_actor FROM 12)
              AND l.expires_at>clock_timestamp() AND pm.role='admin') THEN
            RAISE EXCEPTION 'repository_action_denied' USING ERRCODE='42501';
        END IF;
        PERFORM set_config('puppyone.native_initializer',p_actor,true);
        RETURN;
    END IF;
    IF NOT EXISTS(SELECT 1 FROM public.projects WHERE id=p_project_id AND lifecycle_status='ready') THEN
        RAISE EXCEPTION 'repository_action_denied' USING ERRCODE='42501'; END IF;
    IF p_actor LIKE 'user:%' THEN
        BEGIN subject_user_id := substring(p_actor FROM 6)::uuid;
        EXCEPTION WHEN invalid_text_representation THEN
            RAISE EXCEPTION 'repository_action_denied' USING ERRCODE='42501';
        END;
    ELSIF p_actor LIKE 'runtime:%' THEN
        SELECT * INTO hint FROM public.access_surface_credentials
            WHERE id=substring(p_actor FROM 9) AND project_id=p_project_id AND org_id=p_org_id;
        IF NOT FOUND THEN RAISE EXCEPTION 'repository_action_denied' USING ERRCODE='42501'; END IF;
        subject_user_id := hint.user_id;
    ELSE
        RAISE EXCEPTION 'repository_action_denied' USING ERRCODE='42501';
    END IF;
    IF subject_user_id IS NOT NULL THEN
        PERFORM 1 FROM public.org_members m WHERE m.org_id=p_org_id AND m.user_id=subject_user_id FOR SHARE;
        PERFORM 1 FROM public.project_members m WHERE m.project_id=p_project_id AND m.user_id=subject_user_id FOR SHARE;
        SELECT (r.effective_role IN ('admin','editor')) INTO allowed FROM public.resolve_project_role(p_project_id,subject_user_id) r;
        IF NOT FOUND OR (p_write AND NOT allowed) THEN
            RAISE EXCEPTION 'repository_action_denied' USING ERRCODE='42501';
        END IF;
    END IF;
    IF p_actor LIKE 'runtime:%' THEN
        PERFORM 1 FROM public.access_surfaces s WHERE s.id=hint.access_surface_id
            AND s.project_id=p_project_id AND s.org_id=p_org_id FOR SHARE;
        IF NOT FOUND THEN RAISE EXCEPTION 'repository_action_denied' USING ERRCODE='42501'; END IF;
        SELECT * INTO credential FROM public.access_surface_credentials WHERE id=hint.id FOR SHARE;
        IF NOT FOUND OR credential.project_id IS DISTINCT FROM p_project_id
           OR credential.org_id IS DISTINCT FROM p_org_id
           OR credential.user_id IS DISTINCT FROM hint.user_id
           OR credential.access_surface_id IS DISTINCT FROM hint.access_surface_id
           OR credential.status <> 'active'
           OR (credential.expires_at IS NOT NULL AND credential.expires_at <= clock_timestamp()) THEN
            RAISE EXCEPTION 'repository_action_denied' USING ERRCODE='42501';
        END IF;
        SELECT * INTO resolved FROM public.resolve_git_runtime_credential(credential.key_hash) r
            WHERE r.credential_id=credential.id;
        IF NOT FOUND OR resolved.project_id IS DISTINCT FROM p_project_id
           OR resolved.org_id IS DISTINCT FROM p_org_id OR resolved.target_kind <> 'project_root'
           OR (p_write AND resolved.effective_mode <> 'rw') THEN
            RAISE EXCEPTION 'repository_action_denied' USING ERRCODE='42501';
        END IF;
    END IF;
END $$;

CREATE OR REPLACE FUNCTION "public"."complete_project_initialization"("p_operation_key" "text", "p_project_id" "text", "p_actor_user_id" "uuid") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
DECLARE
    operation public.project_create_operations%ROWTYPE;
    project_row public.projects%ROWTYPE;
    empty_tree constant text := '4b825dc642cb6eb9a060e54bf8d69288fbee4904';
BEGIN
    PERFORM pg_advisory_xact_lock(
        hashtextextended(
            'project-create:' || p_actor_user_id::text || ':' || p_operation_key,
            0
        )
    );
    SELECT * INTO operation
    FROM public.project_create_operations
    WHERE actor_user_id = p_actor_user_id
      AND operation_key = p_operation_key
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('outcome', 'not_found');
    END IF;
    IF operation.project_id IS DISTINCT FROM p_project_id THEN
        RETURN jsonb_build_object('outcome', 'conflict');
    END IF;
    IF operation.status = 'deleted' THEN
        RETURN jsonb_build_object('outcome', 'gone');
    END IF;

    SELECT * INTO project_row
    FROM public.projects
    WHERE id = p_project_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('outcome', 'gone');
    END IF;
    IF EXISTS(SELECT 1 FROM public.version_repositories WHERE project_id=p_project_id AND authority='native') THEN
        IF NOT EXISTS(SELECT 1 FROM public.version_repository_refs WHERE project_id=p_project_id AND name=convert_to('HEAD','UTF8')) THEN
            RETURN jsonb_build_object('outcome','root_not_initialized'); END IF;
    ELSIF COALESCE(project_row.version_root_hash, '') = ''
       OR project_row.version_root_hash IS DISTINCT FROM project_row.mut_root_hash
       OR (
           operation.publication_mode = 'empty'
           AND project_row.version_root_hash <> empty_tree
       ) THEN
        RETURN jsonb_build_object('outcome', 'root_not_initialized');
    END IF;

    IF operation.status = 'ready' THEN
        IF project_row.lifecycle_status IS DISTINCT FROM 'ready' THEN
            RETURN jsonb_build_object('outcome', 'lifecycle_conflict');
        END IF;
        UPDATE public.project_create_operations
        SET replayed_at = now()
        WHERE actor_user_id = p_actor_user_id
          AND operation_key = p_operation_key;
        RETURN jsonb_build_object(
            'outcome', 'replayed',
            'project', operation.project_snapshot
        );
    END IF;

    UPDATE public.projects
    SET lifecycle_status = 'ready',
        updated_at = now()
    WHERE id = p_project_id
      AND lifecycle_status = 'initializing'
    RETURNING * INTO project_row;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('outcome', 'lifecycle_conflict');
    END IF;

    UPDATE public.project_create_operations
    SET status = 'ready',
        project_snapshot = to_jsonb(project_row),
        ready_at = now(),
        initialization_claimed_at = NULL,
        initialization_claimed_by = NULL,
        initialization_last_error = NULL
    WHERE actor_user_id = p_actor_user_id
      AND operation_key = p_operation_key;

    RETURN jsonb_build_object(
        'outcome', 'completed',
        'project', to_jsonb(project_row)
    );
END;
$$;

CREATE FUNCTION public._version_initialization_is_admitted(p_project text)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
    SELECT EXISTS(SELECT 1 FROM public.projects p
        JOIN public.project_create_operations o ON o.project_id=p.id AND o.actor_user_id=p.created_by
        JOIN public.project_write_leases l ON l.project_id=p.id
        WHERE p.id=p_project AND p.lifecycle_status='initializing' AND o.status='initializing'
          AND l.expires_at>clock_timestamp()
          AND current_setting('puppyone.native_initializer',true)='initialize:'||l.id::text)
$$;
REVOKE ALL ON FUNCTION public._version_initialization_is_admitted(text) FROM PUBLIC,anon,authenticated,service_role;

CREATE OR REPLACE FUNCTION public._version_lock_native_repository(p_project_id text, p_write boolean DEFAULT true)
RETURNS public.version_repositories LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE repo public.version_repositories%ROWTYPE; lifecycle text;
BEGIN
    SELECT lifecycle_status INTO lifecycle FROM public.projects WHERE id=p_project_id FOR UPDATE;
    -- Internal storage primitives also serve hidden initialization. Every external
    -- path first executes _version_assert_current_repository_actor; actual ref
    -- publication still requires its transaction-local initializer proof.
    IF NOT FOUND OR lifecycle NOT IN ('ready','initializing') THEN
        RAISE EXCEPTION 'repository_unavailable' USING ERRCODE='55000';
    END IF;
    SELECT * INTO repo FROM public.version_repositories WHERE project_id=p_project_id FOR UPDATE;
    IF NOT FOUND OR repo.authority <> 'native' OR (p_write AND repo.write_state <> 'active') THEN
        RAISE EXCEPTION 'repository_unavailable' USING ERRCODE='55000';
    END IF;
    RETURN repo;
END $$;

CREATE OR REPLACE FUNCTION public.apply_version_ref_transaction(
    p_project_id text, p_actor text, p_request_key uuid, p_generation bigint,
    p_updates jsonb, p_receipt_id uuid DEFAULT NULL, p_message text DEFAULT ''
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
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
    IF (lifecycle <> 'ready' AND NOT public._version_initialization_is_admitted(p_project_id)) OR repo.authority <> 'native' OR repo.write_state <> 'active' THEN
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

NOTIFY pgrst,'reload schema';
COMMIT;
