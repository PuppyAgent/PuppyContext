-- ============================================================
-- Migration: Gateway / Access Point Split
--
-- Separates third-party account bindings (Gateway) from
-- data flow configuration (Access Point).
--
-- Gateway = OAuth/credential binding, lives at org level,
--           survives project deletion, reusable across projects.
-- Access Point = MUT repo binding, lives at project level,
--               deleted with project.
--
-- Reference: docs/architecture/06-gateway-access-point-split.md
-- ============================================================

BEGIN;

-- ── 1. Create gateways table ──────────────────────────────

CREATE TABLE IF NOT EXISTS "public"."gateways" (
    "id"          text PRIMARY KEY DEFAULT gen_random_uuid()::text,
    "org_id"      text NOT NULL,
    "user_id"     uuid NOT NULL,
    "provider"    text NOT NULL,
    "name"        text,
    "status"      text NOT NULL DEFAULT 'active',
    "credentials" jsonb DEFAULT '{}',
    "metadata"    jsonb DEFAULT '{}',
    "created_at"  timestamptz NOT NULL DEFAULT now(),
    "updated_at"  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_gateways_user_provider
    ON gateways(user_id, provider);
CREATE INDEX IF NOT EXISTS idx_gateways_org
    ON gateways(org_id);

ALTER TABLE "public"."gateways" ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "service_role_all_gateways" ON "public"."gateways";
CREATE POLICY "service_role_all_gateways"
    ON "public"."gateways"
    FOR ALL TO service_role
    USING (true) WITH CHECK (true);

-- Grant permissions
GRANT ALL ON TABLE "public"."gateways" TO anon;
GRANT ALL ON TABLE "public"."gateways" TO authenticated;
GRANT ALL ON TABLE "public"."gateways" TO service_role;

-- ── 2. Add gateway_id to access_points ────────────────────

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'access_points'
          AND column_name = 'gateway_id'
    ) THEN
        ALTER TABLE "public"."access_points"
            ADD COLUMN "gateway_id" text REFERENCES gateways(id) ON DELETE SET NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_access_points_gateway
    ON access_points(gateway_id);

-- ── 3. Migrate oauth_connections → gateways ───────────────

-- Only migrate if oauth_connections exists and gateways is empty
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'oauth_connections'
    ) THEN
        INSERT INTO gateways (id, org_id, user_id, provider, name, credentials, metadata)
        SELECT
            gen_random_uuid()::text,
            COALESCE(
                p.default_org_id,
                (SELECT id FROM organizations LIMIT 1),
                'unknown'
            ),
            oc.user_id,
            oc.provider,
            COALESCE(oc.workspace_name, oc.provider),
            jsonb_strip_nulls(jsonb_build_object(
                'access_token', oc.access_token,
                'refresh_token', oc.refresh_token,
                'token_type', oc.token_type,
                'expires_at', oc.expires_at::text
            )),
            jsonb_strip_nulls(jsonb_build_object(
                'workspace_id', oc.workspace_id,
                'workspace_name', oc.workspace_name,
                'bot_id', oc.bot_id
            ))
        FROM oauth_connections oc
        LEFT JOIN profiles p ON p.user_id = oc.user_id
        WHERE NOT EXISTS (
            -- Skip if already migrated
            SELECT 1 FROM gateways g
            WHERE g.user_id = oc.user_id AND g.provider = oc.provider
        );
    END IF;
END $$;

-- ── 4. Backfill access_points.gateway_id ──────────────────

-- Link existing datasource APs to their corresponding gateway
UPDATE access_points ap
SET gateway_id = g.id
FROM gateways g
WHERE ap.gateway_id IS NULL
  AND ap.provider = g.provider
  AND ap.user_id = g.user_id
  AND ap.provider NOT IN ('agent', 'mcp', 'sandbox', 'filesystem', 'direct', 'url');

COMMIT;
