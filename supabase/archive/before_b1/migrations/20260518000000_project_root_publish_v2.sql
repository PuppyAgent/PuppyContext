-- ============================================================================
-- publish_mut_project_update v2: product-root CAS + history + transaction +
-- audit + outbox in one SQL transaction.
-- ============================================================================
-- Product/Web writes are one project-root commit. The request path should
-- publish that commit atomically and return; child-scope ref projection,
-- path indexes, and websocket fanout are derived work handled by the
-- post-commit hook/outbox worker.
-- ============================================================================

BEGIN;

DROP FUNCTION IF EXISTS public.publish_mut_project_update(
    TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, JSONB, JSONB, TEXT, TEXT, JSONB
);

DROP FUNCTION IF EXISTS public.publish_mut_project_update(
    TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, JSONB, JSONB, TEXT, TEXT, JSONB,
    TEXT, TEXT, TEXT, TEXT, TEXT, TEXT
);

CREATE OR REPLACE FUNCTION public.publish_mut_project_update(
    p_project_id       TEXT,
    p_old_root_hash    TEXT,
    p_new_root_hash    TEXT,
    p_head_commit_id   TEXT,
    p_who              TEXT,
    p_message          TEXT,
    p_event_type       TEXT,
    p_changes          JSONB,
    p_conflicts        JSONB,
    p_created_at       TEXT,
    p_audit_agent_id   TEXT,
    p_audit_detail     JSONB,
    p_source_channel   TEXT DEFAULT '',
    p_policy           TEXT DEFAULT '',
    p_base_commit_id   TEXT DEFAULT '',
    p_client_commit_id TEXT DEFAULT '',
    p_proposed_tree_id TEXT DEFAULT '',
    p_intent_type      TEXT DEFAULT 'operation'
) RETURNS TABLE (
    published BOOLEAN,
    txn_id    BIGINT
)
LANGUAGE plpgsql
AS $$
DECLARE
    rows_affected INT;
    v_created_at  TIMESTAMPTZ;
    v_txn_id      BIGINT;
BEGIN
    v_created_at := COALESCE(NULLIF(p_created_at, '')::TIMESTAMPTZ, NOW());

    UPDATE public.projects
       SET mut_root_hash = p_new_root_hash,
           updated_at = NOW()
     WHERE id = p_project_id
       AND COALESCE(mut_root_hash, '') = COALESCE(p_old_root_hash, '');

    GET DIAGNOSTICS rows_affected = ROW_COUNT;
    IF rows_affected = 0 THEN
        RETURN QUERY SELECT FALSE::BOOLEAN, NULL::BIGINT;
        RETURN;
    END IF;

    INSERT INTO public.mut_scope_state
        (project_id, scope_path, scope_hash, head_commit_id)
    VALUES
        (p_project_id, '', p_new_root_hash, p_head_commit_id)
    ON CONFLICT (project_id, scope_path) DO UPDATE
       SET scope_hash = EXCLUDED.scope_hash,
           head_commit_id = EXCLUDED.head_commit_id,
           updated_at = NOW();

    INSERT INTO public.mut_commits
        (project_id, commit_id, root_hash, scope_path, scope_hash, who, message, changes, conflicts, created_at)
    VALUES
        (
            p_project_id,
            p_head_commit_id,
            p_new_root_hash,
            '',
            p_new_root_hash,
            p_who,
            COALESCE(p_message, ''),
            COALESCE(p_changes, '[]'::JSONB),
            p_conflicts,
            v_created_at
        );

    INSERT INTO public.version_transactions
        (project_id, scope_path, source_channel, actor, intent_type, status,
         policy, base_commit_id, client_commit_id, proposed_tree_id,
         current_head_at_start, committed_commit_id, message, audit_detail,
         created_at, updated_at)
    VALUES
        (
            p_project_id,
            '',
            COALESCE(NULLIF(p_source_channel, ''), 'papi'),
            COALESCE(p_who, ''),
            COALESCE(NULLIF(p_intent_type, ''), 'operation'),
            'committed',
            COALESCE(p_policy, ''),
            COALESCE(p_base_commit_id, ''),
            COALESCE(p_client_commit_id, ''),
            COALESCE(NULLIF(p_proposed_tree_id, ''), p_new_root_hash),
            COALESCE(p_old_root_hash, ''),
            p_head_commit_id,
            COALESCE(p_message, ''),
            COALESCE(p_audit_detail, '{}'::JSONB),
            v_created_at,
            v_created_at
        )
    RETURNING id INTO v_txn_id;

    INSERT INTO public.audit_logs
        (action, operator_type, operator_id, project_id, metadata,
         transaction_id, canonical_commit_id, scope_path, source_channel,
         policy, status)
    VALUES
        (
            p_event_type,
            CASE
                WHEN p_audit_agent_id LIKE 'agent:%' THEN 'agent'
                WHEN p_audit_agent_id LIKE 'sync:%' THEN 'sync'
                WHEN p_audit_agent_id LIKE 'user:%' THEN 'user'
                ELSE 'system'
            END,
            p_audit_agent_id,
            p_project_id,
            p_audit_detail,
            v_txn_id,
            p_head_commit_id,
            '',
            COALESCE(NULLIF(p_source_channel, ''), 'papi'),
            COALESCE(p_policy, ''),
            'committed'
        );

    INSERT INTO public.mut_version_outbox
        (project_id, commit_id, event_type, payload)
    VALUES
        (
            p_project_id,
            p_head_commit_id,
            'project_version_committed',
            jsonb_build_object(
                'scope_path', '',
                'scope_hash', p_new_root_hash,
                'root_hash', p_new_root_hash,
                'event_type', p_event_type,
                'transaction_id', v_txn_id,
                'source_channel', COALESCE(NULLIF(p_source_channel, ''), 'papi'),
                'policy', COALESCE(p_policy, '')
            )
        );

    RETURN QUERY SELECT TRUE::BOOLEAN, v_txn_id;
END;
$$;

COMMIT;
