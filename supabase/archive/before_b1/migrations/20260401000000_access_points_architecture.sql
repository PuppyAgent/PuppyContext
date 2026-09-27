-- ============================================================
-- Migration: Unified Access Points Architecture
--
-- Phase 1: Rename tables + create satellite tables (additive)
--   - connections → access_points
--   - connection_tools → access_tools
--   - connection_accesses → access_permissions
--   - sync_runs.sync_id → sync_runs.access_point_id
--   - Create sync_state + agent_profiles satellite tables
--   - Populate satellite tables from existing data
--
-- Columns are NOT dropped from access_points yet; satellite
-- tables are additive so existing code keeps working while
-- repositories are gradually migrated.
-- ============================================================

BEGIN;

-- 1. Rename base table
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'connections') THEN
        ALTER TABLE "public"."connections" RENAME TO "access_points";
    END IF;
END $$;

-- 2. Rename junction tables (skip if they don't exist)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'connection_accesses') THEN
        ALTER TABLE "public"."connection_accesses" RENAME TO "access_permissions";
    END IF;
END $$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'connection_tools') THEN
        ALTER TABLE "public"."connection_tools" RENAME TO "access_tools";
    END IF;
END $$;

-- 3. Rename FK columns in junction tables
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'access_permissions'
        AND column_name = 'connection_id'
    ) THEN
        ALTER TABLE "public"."access_permissions"
            RENAME COLUMN "connection_id" TO "access_point_id";
    END IF;
END $$;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'access_tools'
        AND column_name = 'connection_id'
    ) THEN
        ALTER TABLE "public"."access_tools"
            RENAME COLUMN "connection_id" TO "access_point_id";
    END IF;
END $$;

-- 4. Rename sync_runs FK column
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'sync_runs'
        AND column_name = 'sync_id'
    ) THEN
        ALTER TABLE "public"."sync_runs"
            RENAME COLUMN "sync_id" TO "access_point_id";
    END IF;
END $$;

-- 5. Create sync_state satellite table
CREATE TABLE IF NOT EXISTS "public"."sync_state" (
    "access_point_id" TEXT NOT NULL,
    "direction" TEXT NOT NULL DEFAULT 'inbound',
    "authority" TEXT NOT NULL DEFAULT 'mirror',
    "trigger" JSONB NOT NULL DEFAULT '{"type": "manual"}'::jsonb,
    "conflict_strategy" TEXT NOT NULL DEFAULT 'source_wins',
    "cursor" BIGINT DEFAULT 0,
    "last_synced_at" TIMESTAMPTZ,
    "remote_hash" TEXT,
    "last_sync_version" INTEGER NOT NULL DEFAULT 0,
    "credentials_ref" TEXT,

    CONSTRAINT "sync_state_pkey" PRIMARY KEY ("access_point_id"),
    CONSTRAINT "sync_state_access_point_id_fkey"
        FOREIGN KEY ("access_point_id")
        REFERENCES "public"."access_points"("id")
        ON DELETE CASCADE
);

ALTER TABLE "public"."sync_state" OWNER TO "postgres";
ALTER TABLE "public"."sync_state" ENABLE ROW LEVEL SECURITY;
CREATE POLICY "service_role_all_sync_state"
    ON "public"."sync_state" TO "service_role"
    USING (true) WITH CHECK (true);

-- 6. Populate sync_state from access_points
INSERT INTO "public"."sync_state" (
    access_point_id, direction, authority, trigger,
    conflict_strategy, cursor, last_synced_at,
    remote_hash, last_sync_version, credentials_ref
)
SELECT
    id,
    COALESCE(direction, 'inbound'),
    COALESCE(authority, 'mirror'),
    COALESCE(trigger, '{"type": "manual"}'::jsonb),
    COALESCE(conflict_strategy, 'source_wins'),
    COALESCE(cursor, 0),
    last_synced_at,
    remote_hash,
    COALESCE(last_sync_version, 0),
    credentials_ref
FROM "public"."access_points"
WHERE provider NOT IN ('agent', 'mcp', 'sandbox', 'direct')
  AND direction IS NOT NULL
ON CONFLICT (access_point_id) DO NOTHING;

-- 7. Create agent_profiles satellite table
CREATE TABLE IF NOT EXISTS "public"."agent_profiles" (
    "access_point_id" TEXT NOT NULL,
    "model" TEXT,
    "system_prompt" TEXT,
    "agent_type" TEXT DEFAULT 'chat',
    "temperature" REAL,
    "max_tokens" INTEGER,

    CONSTRAINT "agent_profiles_pkey" PRIMARY KEY ("access_point_id"),
    CONSTRAINT "agent_profiles_access_point_id_fkey"
        FOREIGN KEY ("access_point_id")
        REFERENCES "public"."access_points"("id")
        ON DELETE CASCADE
);

ALTER TABLE "public"."agent_profiles" OWNER TO "postgres";
ALTER TABLE "public"."agent_profiles" ENABLE ROW LEVEL SECURITY;
CREATE POLICY "service_role_all_agent_profiles"
    ON "public"."agent_profiles" TO "service_role"
    USING (true) WITH CHECK (true);

-- 8. Populate agent_profiles from access_points
INSERT INTO "public"."agent_profiles" (
    access_point_id, model, system_prompt, agent_type,
    temperature, max_tokens
)
SELECT
    id,
    config->>'model',
    config->>'system_prompt',
    COALESCE(config->>'type', 'chat'),
    (config->>'temperature')::real,
    (config->>'max_tokens')::integer
FROM "public"."access_points"
WHERE provider = 'agent'
ON CONFLICT (access_point_id) DO NOTHING;

-- 9. Indexes on new tables
CREATE INDEX IF NOT EXISTS "idx_sync_state_last_synced"
    ON "public"."sync_state" ("last_synced_at" DESC);

CREATE INDEX IF NOT EXISTS "idx_agent_profiles_model"
    ON "public"."agent_profiles" ("model");

-- 10. Grants
GRANT ALL ON TABLE "public"."sync_state" TO "anon";
GRANT ALL ON TABLE "public"."sync_state" TO "authenticated";
GRANT ALL ON TABLE "public"."sync_state" TO "service_role";

GRANT ALL ON TABLE "public"."agent_profiles" TO "anon";
GRANT ALL ON TABLE "public"."agent_profiles" TO "authenticated";
GRANT ALL ON TABLE "public"."agent_profiles" TO "service_role";

COMMIT;
