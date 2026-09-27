-- ============================================================================
-- publish_mut_scope_update v2: atomic write of scope head + commits +
-- audit + outbox + version_transactions in one SQL transaction.
-- ============================================================================
-- 01-version-engine.md §7.4 requires that the accepted-write
-- ref/head, version index, transaction state, audit row, and outbox
-- event publish atomically. Migration 20260513 already covered the
-- first four; this migration extends the RPC so a committed row in
-- version_transactions is inserted in the same plpgsql transaction.
-- ============================================================================

BEGIN;

DROP FUNCTION IF EXISTS public.publish_mut_scope_update(
    TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, JSONB, JSONB, TEXT, TEXT, JSONB
);

CREATE OR REPLACE FUNCTION public.publish_mut_scope_update(
    p_project_id      TEXT,
    p_scope_path      TEXT,
    p_old_hash        TEXT,
    p_new_hash        TEXT,
    p_head_commit_id  TEXT,
    p_who             TEXT,
    p_message         TEXT,
    p_event_type      TEXT,
    p_changes         JSONB,
    p_conflicts       JSONB,
    p_created_at      TEXT,
    p_audit_agent_id  TEXT,
    p_audit_detail    JSONB,
    p_source_channel  TEXT DEFAULT '',
    p_policy          TEXT DEFAULT '',
    p_base_commit_id  TEXT DEFAULT '',
    p_client_commit_id TEXT DEFAULT '',
    p_proposed_tree_id TEXT DEFAULT '',
    p_intent_type      TEXT DEFAULT 'operation'
) RETURNS TABLE (
    published    BOOLEAN,
    txn_id       BIGINT
)
LANGUAGE plpgsql
AS $$
DECLARE
    rows_affected INT;
    v_created_at  TIMESTAMPTZ;
    v_txn_id      BIGINT;
BEGIN
    v_created_at := COALESCE(NULLIF(p_created_at, '')::TIMESTAMPTZ, NOW());

    IF p_old_hash = '' OR p_old_hash IS NULL THEN
        INSERT INTO public.mut_scope_state
            (project_id, scope_path, scope_hash, head_commit_id)
        VALUES
            (p_project_id, p_scope_path, p_new_hash, p_head_commit_id)
        ON CONFLICT (project_id, scope_path) DO NOTHING;

        GET DIAGNOSTICS rows_affected = ROW_COUNT;
        IF rows_affected = 0 THEN
            UPDATE public.mut_scope_state
               SET scope_hash = p_new_hash,
                   head_commit_id = p_head_commit_id,
                   updated_at = NOW()
             WHERE project_id = p_project_id
               AND scope_path = p_scope_path
               AND (scope_hash = p_old_hash OR (scope_hash IS NULL AND p_old_hash = ''));
            GET DIAGNOSTICS rows_affected = ROW_COUNT;
        END IF;
    ELSE
        UPDATE public.mut_scope_state
           SET scope_hash = p_new_hash,
               head_commit_id = p_head_commit_id,
               updated_at = NOW()
         WHERE project_id = p_project_id
           AND scope_path = p_scope_path
           AND scope_hash = p_old_hash;
        GET DIAGNOSTICS rows_affected = ROW_COUNT;
    END IF;

    IF rows_affected = 0 THEN
        RETURN QUERY SELECT FALSE::BOOLEAN, NULL::BIGINT;
        RETURN;
    END IF;

    INSERT INTO public.mut_commits
        (project_id, commit_id, root_hash, scope_path, scope_hash, who, message, changes, conflicts, created_at)
    VALUES
        (
            p_project_id,
            p_head_commit_id,
            '',
            p_scope_path,
            p_new_hash,
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
            p_scope_path,
            COALESCE(NULLIF(p_source_channel, ''), 'papi'),
            COALESCE(p_who, ''),
            COALESCE(NULLIF(p_intent_type, ''), 'operation'),
            'committed',
            COALESCE(p_policy, ''),
            COALESCE(p_base_commit_id, ''),
            COALESCE(p_client_commit_id, ''),
            COALESCE(p_proposed_tree_id, ''),
            COALESCE(p_old_hash, ''),
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
            p_scope_path,
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
            'version_committed',
            jsonb_build_object(
                'scope_path', p_scope_path,
                'scope_hash', p_new_hash,
                'event_type', p_event_type,
                'transaction_id', v_txn_id
            )
        );

    RETURN QUERY SELECT TRUE::BOOLEAN, v_txn_id;
END;
$$;

COMMIT;
