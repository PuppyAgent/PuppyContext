-- Compatibility repair, not native-authority activation or a data migration.
-- Keep the existing RPC identity/defaults/ACLs and unguarded legacy calls.
-- Expected source-head identity applies to Project root as well as Scope.
-- Project-first locking serializes absent scope rows and matches the fence.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '2min';

CREATE OR REPLACE FUNCTION public.publish_version_project_update(
    p_project_id text, p_old_root_hash text, p_new_root_hash text,
    p_head_commit_id text, p_who text, p_message text, p_event_type text,
    p_changes jsonb, p_conflicts jsonb, p_created_at text,
    p_audit_agent_id text, p_audit_detail jsonb,
    p_source_channel text DEFAULT '', p_policy text DEFAULT '',
    p_base_commit_id text DEFAULT '', p_client_commit_id text DEFAULT '',
    p_proposed_tree_id text DEFAULT '', p_intent_type text DEFAULT 'operation',
    p_scope_path text DEFAULT '', p_scope_hash text DEFAULT '',
    p_scope_head_commit_id text DEFAULT '',
    p_expected_scope_head_commit_id text DEFAULT NULL
) RETURNS TABLE(published boolean, txn_id bigint)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    rows_affected int;
    v_created_at timestamptz;
    v_txn_id bigint;
    v_scope_path text;
    v_scope_hash text;
    v_scope_head_commit_id text;
    v_current_scope_head_commit_id text;
    v_current_root_hash text;
BEGIN
    v_created_at := COALESCE(NULLIF(p_created_at, '')::timestamptz, NOW());
    v_scope_path := TRIM(BOTH '/' FROM COALESCE(p_scope_path, ''));
    v_scope_hash := COALESCE(NULLIF(p_scope_hash, ''), p_new_root_hash);
    v_scope_head_commit_id := COALESCE(
        NULLIF(p_scope_head_commit_id, ''), NULLIF(p_client_commit_id, ''),
        p_head_commit_id
    );

    -- Lock an existing parent even when the source-head row is absent. A
    -- SELECT FOR UPDATE of an absent scope row alone cannot serialize creation.
    SELECT mut_root_hash INTO v_current_root_hash
      FROM public.projects WHERE id = p_project_id FOR UPDATE;
    IF NOT FOUND OR COALESCE(v_current_root_hash, '') <> COALESCE(p_old_root_hash, '') THEN
        RETURN QUERY SELECT FALSE::boolean, NULL::bigint;
        RETURN;
    END IF;

    IF p_expected_scope_head_commit_id IS NOT NULL THEN
        SELECT head_commit_id INTO v_current_scope_head_commit_id
          FROM public.mut_scope_state
         WHERE project_id = p_project_id AND scope_path = v_scope_path
         FOR UPDATE;
        IF COALESCE(v_current_scope_head_commit_id, '') <>
           COALESCE(p_expected_scope_head_commit_id, '') THEN
            RETURN QUERY SELECT FALSE::boolean, NULL::bigint;
            RETURN;
        END IF;
    END IF;

    UPDATE public.projects
       SET mut_root_hash = p_new_root_hash, updated_at = NOW()
     WHERE id = p_project_id
       AND COALESCE(mut_root_hash, '') = COALESCE(p_old_root_hash, '');
    GET DIAGNOSTICS rows_affected = ROW_COUNT;
    IF rows_affected = 0 THEN
        RETURN QUERY SELECT FALSE::boolean, NULL::bigint;
        RETURN;
    END IF;

    INSERT INTO public.mut_scope_state (project_id, scope_path, scope_hash, head_commit_id)
    VALUES (p_project_id, '', p_new_root_hash, p_head_commit_id)
    ON CONFLICT (project_id, scope_path) DO UPDATE
       SET scope_hash = EXCLUDED.scope_hash, head_commit_id = EXCLUDED.head_commit_id,
           updated_at = NOW();

    IF v_scope_path <> '' THEN
        INSERT INTO public.mut_scope_state (project_id, scope_path, scope_hash, head_commit_id)
        VALUES (p_project_id, v_scope_path, v_scope_hash, v_scope_head_commit_id)
        ON CONFLICT (project_id, scope_path) DO UPDATE
           SET scope_hash = EXCLUDED.scope_hash, head_commit_id = EXCLUDED.head_commit_id,
               updated_at = NOW();
    END IF;

    INSERT INTO public.mut_commits
        (project_id, commit_id, root_hash, scope_path, scope_hash, who, message, changes, conflicts, created_at)
    VALUES
        (p_project_id, p_head_commit_id, p_new_root_hash, v_scope_path, v_scope_hash,
         p_who, COALESCE(p_message, ''), COALESCE(p_changes, '[]'::jsonb), p_conflicts, v_created_at);

    INSERT INTO public.version_transactions
        (project_id, scope_path, source_channel, actor, intent_type, status,
         policy, base_commit_id, client_commit_id, proposed_tree_id,
         current_head_at_start, committed_commit_id, message, audit_detail,
         created_at, updated_at)
    VALUES
        (p_project_id, v_scope_path, COALESCE(NULLIF(p_source_channel, ''), 'papi'),
         COALESCE(p_who, ''), COALESCE(NULLIF(p_intent_type, ''), 'operation'), 'committed',
         COALESCE(p_policy, ''), COALESCE(p_base_commit_id, ''), COALESCE(p_client_commit_id, ''),
         COALESCE(NULLIF(p_proposed_tree_id, ''), v_scope_hash),
         COALESCE(NULLIF(p_base_commit_id, ''), p_old_root_hash, ''), p_head_commit_id,
         COALESCE(p_message, ''), COALESCE(p_audit_detail, '{}'::jsonb), v_created_at, v_created_at)
    RETURNING id INTO v_txn_id;

    INSERT INTO public.audit_logs
        (action, operator_type, operator_id, project_id, metadata,
         transaction_id, canonical_commit_id, scope_path, source_channel, policy, status)
    VALUES
        (p_event_type,
         CASE WHEN p_audit_agent_id LIKE 'agent:%' THEN 'agent'
              WHEN p_audit_agent_id LIKE 'sync:%' THEN 'sync'
              WHEN p_audit_agent_id LIKE 'user:%' THEN 'user' ELSE 'system' END,
         p_audit_agent_id, p_project_id, p_audit_detail, v_txn_id, p_head_commit_id,
         v_scope_path, COALESCE(NULLIF(p_source_channel, ''), 'papi'), COALESCE(p_policy, ''), 'committed');

    INSERT INTO public.mut_version_outbox (project_id, commit_id, event_type, payload)
    VALUES
        (p_project_id, p_head_commit_id, 'project_version_committed',
         jsonb_build_object('scope_path', v_scope_path, 'scope_hash', v_scope_hash,
             'root_hash', p_new_root_hash, 'event_type', p_event_type,
             'transaction_id', v_txn_id, 'source_channel',
             COALESCE(NULLIF(p_source_channel, ''), 'papi'), 'policy', COALESCE(p_policy, '')));

    RETURN QUERY SELECT TRUE::boolean, v_txn_id;
END;
$$;

-- Versioned entrypoints make schema-before-code fail closed. The old RPC
-- already accepts the expected-head argument but silently ignores it for root
-- writes before this repair; new clients must not mistake that for support.
CREATE OR REPLACE FUNCTION public.publish_version_project_update_checked(
    p_project_id text, p_old_root_hash text, p_new_root_hash text,
    p_head_commit_id text, p_who text, p_message text, p_event_type text,
    p_changes jsonb, p_conflicts jsonb, p_created_at text,
    p_audit_agent_id text, p_audit_detail jsonb,
    p_source_channel text DEFAULT '', p_policy text DEFAULT '',
    p_base_commit_id text DEFAULT '', p_client_commit_id text DEFAULT '',
    p_proposed_tree_id text DEFAULT '', p_intent_type text DEFAULT 'operation',
    p_scope_path text DEFAULT '', p_scope_hash text DEFAULT '',
    p_scope_head_commit_id text DEFAULT '',
    p_expected_scope_head_commit_id text DEFAULT NULL
) RETURNS TABLE(published boolean, txn_id bigint)
LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
    IF p_expected_scope_head_commit_id IS NULL THEN
        RAISE EXCEPTION 'expected_source_head_required' USING ERRCODE = '22023';
    END IF;
    RETURN QUERY SELECT * FROM public.publish_version_project_update(
        p_project_id, p_old_root_hash, p_new_root_hash, p_head_commit_id,
        p_who, p_message, p_event_type, p_changes, p_conflicts, p_created_at,
        p_audit_agent_id, p_audit_detail, p_source_channel, p_policy,
        p_base_commit_id, p_client_commit_id, p_proposed_tree_id, p_intent_type,
        p_scope_path, p_scope_hash, p_scope_head_commit_id, p_expected_scope_head_commit_id
    );
END;
$$;

CREATE OR REPLACE FUNCTION public.publish_version_project_update_with_usage_checked(
    p_project_id text, p_old_root_hash text, p_new_root_hash text,
    p_head_commit_id text, p_who text, p_message text, p_event_type text,
    p_changes jsonb, p_conflicts jsonb, p_created_at text,
    p_audit_agent_id text, p_audit_detail jsonb,
    p_source_channel text DEFAULT '', p_policy text DEFAULT '',
    p_base_commit_id text DEFAULT '', p_client_commit_id text DEFAULT '',
    p_proposed_tree_id text DEFAULT '', p_intent_type text DEFAULT 'operation',
    p_scope_path text DEFAULT '', p_scope_hash text DEFAULT '',
    p_scope_head_commit_id text DEFAULT '',
    p_expected_scope_head_commit_id text DEFAULT NULL,
    p_org_id text DEFAULT NULL, p_storage_old_value bigint DEFAULT 0,
    p_storage_delta bigint DEFAULT 0, p_storage_limit bigint DEFAULT NULL,
    p_storage_enforce boolean DEFAULT false,
    p_entitlement_source_revision bigint DEFAULT NULL
) RETURNS TABLE(published boolean, txn_id bigint)
LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
    IF p_expected_scope_head_commit_id IS NULL THEN
        RAISE EXCEPTION 'expected_source_head_required' USING ERRCODE = '22023';
    END IF;
    -- Reuse the existing Org -> Project lock order and atomic usage facts.
    -- Its nested publish call is the repaired function installed above.
    RETURN QUERY SELECT * FROM public.publish_version_project_update_with_usage(
        p_project_id, p_old_root_hash, p_new_root_hash, p_head_commit_id,
        p_who, p_message, p_event_type, p_changes, p_conflicts, p_created_at,
        p_audit_agent_id, p_audit_detail, p_source_channel, p_policy,
        p_base_commit_id, p_client_commit_id, p_proposed_tree_id, p_intent_type,
        p_scope_path, p_scope_hash, p_scope_head_commit_id, p_expected_scope_head_commit_id,
        p_org_id, p_storage_old_value, p_storage_delta, p_storage_limit,
        p_storage_enforce, p_entitlement_source_revision
    );
END;
$$;

REVOKE ALL ON FUNCTION public.publish_version_project_update_checked(
    text,text,text,text,text,text,text,jsonb,jsonb,text,text,jsonb,
    text,text,text,text,text,text,text,text,text,text
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.publish_version_project_update_checked(
    text,text,text,text,text,text,text,jsonb,jsonb,text,text,jsonb,
    text,text,text,text,text,text,text,text,text,text
) TO service_role;
REVOKE ALL ON FUNCTION public.publish_version_project_update_with_usage_checked(
    text,text,text,text,text,text,text,jsonb,jsonb,text,text,jsonb,
    text,text,text,text,text,text,text,text,text,text,text,bigint,bigint,bigint,boolean,bigint
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.publish_version_project_update_with_usage_checked(
    text,text,text,text,text,text,text,jsonb,jsonb,text,text,jsonb,
    text,text,text,text,text,text,text,text,text,text,text,bigint,bigint,bigint,boolean,bigint
) TO service_role;

NOTIFY pgrst, 'reload schema';
COMMIT;
