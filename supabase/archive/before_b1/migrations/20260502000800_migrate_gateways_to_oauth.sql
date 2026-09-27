-- ============================================================================
-- Migrate gateways rows into oauth_connections, then rewire connectors (Q6)
-- ============================================================================
-- Why
--   `gateways` was an org-level OAuth abstraction. Per Q6 the OAuth model is
--   "always per-user-per-provider, even for team accounts" (the team account
--   is just one of the team's users). So gateways was the wrong shape.
--
--   This migration:
--     1. Copies each gateways row into oauth_connections (attributed to org
--        owner, since that's who originally authorized).
--     2. Rewires connectors.oauth_connection_id so the new BIGINT id replaces
--        the dropped TEXT gateway_id link. The Python data-migration script
--        (migrate_access_points_to_v2.py) stashes the original gateway_id
--        into connectors.config['_legacy_gateway_id'] precisely for this step.
--     3. Strips the transient _legacy_gateway_id breadcrumb from config.
--
--   See docs/design/access-point-redesign-2026-05-02.md (sections 5.5, 5.6).
--
-- Why we don't preserve gateway.id as oauth_connections.id
--   oauth_connections.id is BIGINT GENERATED ALWAYS AS IDENTITY (inherited
--   from the original qubits schema; see 20260306085814_qubits_schema.sql:944).
--   You cannot INSERT a non-default value into a GENERATED ALWAYS column,
--   and gateways.id is a TEXT uuid anyway. We let IDENTITY mint a fresh
--   BIGINT id, and use a metadata breadcrumb (_legacy_gateway_id) to make
--   the insert idempotent and to enable the connectors UPDATE below.
--
-- Idempotency
--   - oauth_connections insert is gated on metadata->>'_legacy_gateway_id'
--     not already present. Re-running won't double-insert.
--   - The connectors UPDATE is naturally idempotent (running twice produces
--     the same final oauth_connection_id and the same config sans marker).
-- ============================================================================

BEGIN;

-- Skip cleanly if gateways was never created (fresh-DB case).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
         WHERE table_schema = 'public' AND table_name = 'gateways'
    ) THEN
        RAISE NOTICE 'gateways table absent; nothing to migrate.';
        RETURN;
    END IF;

    -- 1. Copy each gateway → oauth_connections (attributed to org owner).
    --    We tag metadata with _legacy_gateway_id for idempotency AND so the
    --    connectors UPDATE below can find each row's new id.
    INSERT INTO public.oauth_connections (
        user_id, provider, access_token, refresh_token,
        token_type, expires_at, workspace_id, workspace_name, bot_id,
        metadata, created_at, updated_at
    )
    SELECT
        -- Resolve org owner: first member of the gateway's org with owner role.
        -- org_members tracks join time as `joined_at`, not `created_at`
        -- (cf. 20260306085814_qubits_schema.sql:997).
        COALESCE(
            (SELECT om.user_id::UUID FROM public.org_members om
              WHERE om.org_id = g.org_id AND om.role = 'owner'
              ORDER BY om.joined_at ASC LIMIT 1),
            (SELECT om.user_id::UUID FROM public.org_members om
              WHERE om.org_id = g.org_id
              ORDER BY om.joined_at ASC LIMIT 1)
        ) AS user_id,
        g.provider,
        (g.credentials->>'access_token')::TEXT,
        (g.credentials->>'refresh_token')::TEXT,
        COALESCE((g.credentials->>'token_type')::TEXT, 'bearer'),
        CASE
            WHEN g.credentials->>'expires_at' ~ '^\d+$'
                THEN to_timestamp((g.credentials->>'expires_at')::BIGINT)
            ELSE NULL
        END,
        (g.metadata->>'workspace_id')::TEXT,
        (g.metadata->>'workspace_name')::TEXT,
        (g.metadata->>'bot_id')::TEXT,
        COALESCE(g.metadata, '{}'::JSONB) || jsonb_build_object('_legacy_gateway_id', g.id),
        g.created_at,
        g.updated_at
    FROM public.gateways g
    WHERE NOT EXISTS (
        SELECT 1 FROM public.oauth_connections oc
         WHERE oc.metadata->>'_legacy_gateway_id' = g.id
    )
      -- oauth_connections.access_token is NOT NULL. Skip gateways with no
      -- credentials (broken historical rows) so the migration doesn't abort
      -- on a single bad record. They'll be visible in the post-migration
      -- audit because their connectors still have _legacy_gateway_id but no
      -- linked oauth_connection.
      AND (g.credentials->>'access_token') IS NOT NULL;

    -- 2. Rewire connectors: for each connector that has _legacy_gateway_id in
    --    its config, look up the oauth_connections row we just created (or
    --    one that already existed from a prior run) and set the FK.
    UPDATE public.connectors c
       SET oauth_connection_id = oc.id,
           config              = c.config - '_legacy_gateway_id'
      FROM public.oauth_connections oc
     WHERE c.config ? '_legacy_gateway_id'
       AND oc.metadata->>'_legacy_gateway_id' = c.config->>'_legacy_gateway_id';
END $$;

COMMIT;
