-- ============================================================
-- PRODUCTION MIGRATION: Qubits → Main
-- ============================================================
-- GENERATED: 2026-03-08
-- STATUS: ⚠️  FOR REVIEW ONLY — DO NOT RUN WITHOUT READING
-- ============================================================
--
-- This migration brings the production database (vxhyuctgfyxxlhobdpca)
-- in line with the test database (qextonmjqbhxgokmjbio).
--
-- Run each phase separately in Supabase SQL Editor.
-- After each phase, verify before proceeding to the next.
-- ============================================================

-- ============================================================
-- PHASE 0: ADD depth column FIRST (needed by functions below)
-- ============================================================

ALTER TABLE "public"."content_nodes" ADD COLUMN IF NOT EXISTS "depth" integer
    GENERATED ALWAYS AS (array_length(string_to_array(trim(both '/' from id_path), '/'), 1)) STORED;


-- ============================================================
-- PHASE 1: NEW FUNCTIONS (safe — no data impact)
-- ============================================================

CREATE OR REPLACE FUNCTION "public"."parent_path"("p_id_path" "text") RETURNS "text"
    LANGUAGE "sql" IMMUTABLE
    AS $_$
    SELECT CASE
        WHEN p_id_path ~ '^/[^/]+$' THEN '__root__'
        ELSE regexp_replace(p_id_path, '/[^/]+$', '')
    END;
$_$;

CREATE OR REPLACE FUNCTION "public"."check_no_cycle"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    AS $$
DECLARE
    parts TEXT[];
    node_id TEXT;
    i INT;
BEGIN
    IF NEW.id_path IS NULL OR NEW.id_path = '' THEN
        RETURN NEW;
    END IF;
    parts := string_to_array(trim(both '/' from NEW.id_path), '/');
    IF array_length(parts, 1) IS NULL THEN
        RETURN NEW;
    END IF;
    node_id := parts[array_length(parts, 1)];
    FOR i IN 1..array_length(parts, 1) - 1 LOOP
        IF parts[i] = node_id THEN
            RAISE EXCEPTION 'Circular reference: node % cannot be its own ancestor', node_id;
        END IF;
    END LOOP;
    IF array_length(parts, 1) > 100 THEN
        RAISE EXCEPTION 'Tree depth exceeded maximum (100) for node %', node_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION "public"."count_children_batch"("p_parent_ids" "text"[]) RETURNS TABLE("parent_id" "text", "child_count" bigint)
    LANGUAGE "sql" STABLE
    AS $$
    SELECT p.id AS parent_id, COUNT(c.id) AS child_count
    FROM content_nodes p
    LEFT JOIN content_nodes c
        ON c.project_id = p.project_id
        AND c.id_path LIKE p.id_path || '/%'
        AND c.depth = p.depth + 1
    WHERE p.id = ANY(p_parent_ids)
    GROUP BY p.id;
$$;

CREATE OR REPLACE FUNCTION "public"."move_node_atomic"("p_node_id" "text", "p_project_id" "text", "p_new_id_path" "text") RETURNS "void"
    LANGUAGE "plpgsql"
    AS $$
DECLARE
    v_old_id_path TEXT;
BEGIN
    SELECT id_path INTO v_old_id_path
    FROM content_nodes WHERE id = p_node_id FOR UPDATE;
    IF v_old_id_path IS NULL THEN
        RAISE EXCEPTION 'Node not found: %', p_node_id;
    END IF;
    UPDATE content_nodes SET id_path = p_new_id_path WHERE id = p_node_id;
    UPDATE content_nodes
    SET id_path = p_new_id_path || substring(id_path from length(v_old_id_path) + 1)
    WHERE project_id = p_project_id AND id_path LIKE v_old_id_path || '/%';
END;
$$;

CREATE OR REPLACE FUNCTION "public"."move_node_atomic"("p_node_id" "text", "p_project_id" "text", "p_new_parent_id" "text", "p_new_id_path" "text") RETURNS "void"
    LANGUAGE "plpgsql"
    AS $$
DECLARE
    v_old_id_path TEXT;
BEGIN
    SELECT id_path INTO v_old_id_path
    FROM content_nodes WHERE id = p_node_id FOR UPDATE;
    IF v_old_id_path IS NULL THEN
        RAISE EXCEPTION 'Node not found: %', p_node_id;
    END IF;
    UPDATE content_nodes SET id_path = p_new_id_path WHERE id = p_node_id;
    UPDATE content_nodes
    SET id_path = p_new_id_path || substring(id_path from length(v_old_id_path) + 1)
    WHERE project_id = p_project_id AND id_path LIKE v_old_id_path || '/%';
END;
$$;

CREATE OR REPLACE FUNCTION "public"."next_version"("p_node_id" "text") RETURNS integer
    LANGUAGE "plpgsql"
    AS $$
DECLARE
    v INT;
BEGIN
    UPDATE content_nodes
    SET current_version = current_version + 1, updated_at = NOW()
    WHERE id = p_node_id
    RETURNING current_version INTO v;
    IF v IS NULL THEN
        RAISE EXCEPTION 'Node % not found', p_node_id;
    END IF;
    RETURN v;
END;
$$;


-- ============================================================
-- PHASE 2: RENAME TABLES (singular → plural)
-- These preserve all data, indexes, and constraints.
-- ============================================================

ALTER TABLE IF EXISTS "public"."project" RENAME TO "projects";
ALTER TABLE IF EXISTS "public"."tool" RENAME TO "tools";
ALTER TABLE IF EXISTS "public"."etl_rule" RENAME TO "etl_rules";
ALTER TABLE IF EXISTS "public"."oauth_connection" RENAME TO "oauth_connections";
ALTER TABLE IF EXISTS "public"."agent_execution_log" RENAME TO "agent_execution_logs";
ALTER TABLE IF EXISTS "public"."connection_access" RENAME TO "connection_accesses";
ALTER TABLE IF EXISTS "public"."connection_tool" RENAME TO "connection_tools";


-- ============================================================
-- PHASE 3: RENAME COLUMNS (user_id → created_by where needed)
-- ============================================================

DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='context_publish' AND column_name='user_id') THEN
    ALTER TABLE "public"."context_publish" RENAME COLUMN "user_id" TO "created_by";
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='db_connections' AND column_name='user_id') THEN
    ALTER TABLE "public"."db_connections" RENAME COLUMN "user_id" TO "created_by";
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='mcp' AND column_name='user_id') THEN
    ALTER TABLE "public"."mcp" RENAME COLUMN "user_id" TO "created_by";
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='tools' AND column_name='user_id') THEN
    ALTER TABLE "public"."tools" RENAME COLUMN "user_id" TO "created_by";
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='etl_rules' AND column_name='user_id') THEN
    ALTER TABLE "public"."etl_rules" RENAME COLUMN "user_id" TO "created_by";
  END IF;
  IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='projects' AND column_name='user_id') THEN
    ALTER TABLE "public"."projects" RENAME COLUMN "user_id" TO "created_by";
  END IF;
END $$;


-- ============================================================
-- PHASE 4: ALTER content_nodes (add new columns, remove old sync columns)
-- ============================================================

-- Add new columns
ALTER TABLE "public"."content_nodes" ADD COLUMN IF NOT EXISTS "content_hash" text;
ALTER TABLE "public"."content_nodes" ADD COLUMN IF NOT EXISTS "current_version" integer DEFAULT 0 NOT NULL;

-- depth column already added in Phase 0

-- Normalize any legacy type values before adding constraint
UPDATE "public"."content_nodes" SET type = 'json' WHERE type IS NOT NULL AND type NOT IN ('folder','json','markdown','file');
UPDATE "public"."content_nodes" SET type = 'folder' WHERE type IS NULL;

-- Add type constraint
ALTER TABLE "public"."content_nodes" DROP CONSTRAINT IF EXISTS "chk_content_nodes_type";
ALTER TABLE "public"."content_nodes" ADD CONSTRAINT "chk_content_nodes_type"
    CHECK (type = ANY (ARRAY['folder','json','markdown','file']));

-- Drop old sync-related columns (moved to connections table)
ALTER TABLE "public"."content_nodes" DROP COLUMN IF EXISTS "parent_id";
ALTER TABLE "public"."content_nodes" DROP COLUMN IF EXISTS "last_synced_at";
ALTER TABLE "public"."content_nodes" DROP COLUMN IF EXISTS "sync_config";
ALTER TABLE "public"."content_nodes" DROP COLUMN IF EXISTS "sync_id";
ALTER TABLE "public"."content_nodes" DROP COLUMN IF EXISTS "sync_oauth_user_id";
ALTER TABLE "public"."content_nodes" DROP COLUMN IF EXISTS "sync_status";
ALTER TABLE "public"."content_nodes" DROP COLUMN IF EXISTS "sync_url";

-- Add trigger for cycle detection
CREATE OR REPLACE TRIGGER "trg_check_no_cycle"
    BEFORE INSERT OR UPDATE OF "id_path" ON "public"."content_nodes"
    FOR EACH ROW EXECUTE FUNCTION "public"."check_no_cycle"();

-- Add new indexes for content_nodes
CREATE INDEX IF NOT EXISTS "idx_content_nodes_children_lookup"
    ON "public"."content_nodes" USING btree ("project_id", "depth", "id_path" text_pattern_ops);
CREATE INDEX IF NOT EXISTS "idx_content_nodes_project_depth"
    ON "public"."content_nodes" USING btree ("project_id", "depth");
CREATE UNIQUE INDEX IF NOT EXISTS "idx_content_nodes_unique_name_v2"
    ON "public"."content_nodes" USING btree ("project_id", "public"."parent_path"("id_path"), "name");


-- ============================================================
-- PHASE 5: ALTER profiles (add new columns, remove old ones)
-- ============================================================

ALTER TABLE "public"."profiles" ADD COLUMN IF NOT EXISTS "avatar_url" text;
ALTER TABLE "public"."profiles" ADD COLUMN IF NOT EXISTS "display_name" text;
ALTER TABLE "public"."profiles" ADD COLUMN IF NOT EXISTS "default_org_id" text;

-- Remove old columns (plan/role moved to organizations)
ALTER TABLE "public"."profiles" DROP COLUMN IF EXISTS "plan";
ALTER TABLE "public"."profiles" DROP COLUMN IF EXISTS "role";
ALTER TABLE "public"."profiles" DROP COLUMN IF EXISTS "stripe_customer_id";
ALTER TABLE "public"."profiles" DROP CONSTRAINT IF EXISTS "profiles_plan_check";
ALTER TABLE "public"."profiles" DROP CONSTRAINT IF EXISTS "profiles_role_check";


-- ============================================================
-- PHASE 5b: ALTER tools, etl_rules, projects (add missing columns)
-- ============================================================

ALTER TABLE "public"."tools" ADD COLUMN IF NOT EXISTS "org_id" text;
ALTER TABLE "public"."etl_rules" ADD COLUMN IF NOT EXISTS "org_id" text;
ALTER TABLE "public"."projects" ADD COLUMN IF NOT EXISTS "updated_at" timestamptz DEFAULT now() NOT NULL;

-- Add permission column to connection_accesses
ALTER TABLE "public"."connection_accesses" ADD COLUMN IF NOT EXISTS "permission" text DEFAULT 'r' NOT NULL;
ALTER TABLE "public"."connection_accesses" DROP CONSTRAINT IF EXISTS "chk_agent_bash_permission";
ALTER TABLE "public"."connection_accesses" ADD CONSTRAINT "chk_agent_bash_permission"
    CHECK (permission = ANY (ARRAY['r','ra','rw-','rw']));


-- ============================================================
-- PHASE 6: ALTER connections (add constraints)
-- ============================================================

ALTER TABLE "public"."connections" DROP CONSTRAINT IF EXISTS "chk_syncs_authority";
ALTER TABLE "public"."connections" ADD CONSTRAINT "chk_syncs_authority"
    CHECK (authority = ANY (ARRAY['authoritative','mirror']));

ALTER TABLE "public"."connections" DROP CONSTRAINT IF EXISTS "chk_syncs_conflict_strategy";
ALTER TABLE "public"."connections" ADD CONSTRAINT "chk_syncs_conflict_strategy"
    CHECK (conflict_strategy = ANY (ARRAY['source_wins','three_way_merge','lww']));

ALTER TABLE "public"."connections" DROP CONSTRAINT IF EXISTS "chk_syncs_direction";
ALTER TABLE "public"."connections" ADD CONSTRAINT "chk_syncs_direction"
    CHECK (direction = ANY (ARRAY['inbound','outbound','bidirectional']));

ALTER TABLE "public"."connections" DROP CONSTRAINT IF EXISTS "chk_syncs_status";
ALTER TABLE "public"."connections" ADD CONSTRAINT "chk_syncs_status"
    CHECK (status = ANY (ARRAY['active','paused','error','syncing']));

-- Make conflict_strategy NOT NULL (with default already set)
ALTER TABLE "public"."connections" ALTER COLUMN "conflict_strategy" SET NOT NULL;

-- Remove default for direction (test schema has no default)
ALTER TABLE "public"."connections" ALTER COLUMN "direction" DROP DEFAULT;


-- ============================================================
-- PHASE 7: CREATE NEW TABLES — Organizations & Team
-- ============================================================

CREATE TABLE IF NOT EXISTS "public"."organizations" (
    "id" text DEFAULT (extensions.uuid_generate_v4())::text NOT NULL,
    "name" text NOT NULL,
    "slug" text NOT NULL,
    "avatar_url" text,
    "type" text DEFAULT 'personal' NOT NULL,
    "plan" text DEFAULT 'free' NOT NULL,
    "seat_limit" integer DEFAULT 1 NOT NULL,
    "stripe_customer_id" text,
    "stripe_subscription_id" text,
    "created_by" uuid NOT NULL,
    "created_at" timestamptz DEFAULT now() NOT NULL,
    "updated_at" timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT "organizations_plan_check" CHECK (plan = ANY (ARRAY['free','plus','pro','team','enterprise'])),
    CONSTRAINT "organizations_type_check" CHECK (type = ANY (ARRAY['personal','team'])),
    PRIMARY KEY ("id"),
    UNIQUE ("slug")
);

CREATE TABLE IF NOT EXISTS "public"."org_members" (
    "id" text DEFAULT (extensions.uuid_generate_v4())::text NOT NULL,
    "org_id" text NOT NULL REFERENCES "public"."organizations"("id") ON DELETE CASCADE,
    "user_id" uuid NOT NULL REFERENCES "public"."profiles"("user_id") ON DELETE CASCADE,
    "role" text DEFAULT 'member' NOT NULL,
    "joined_at" timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT "org_members_role_check" CHECK (role = ANY (ARRAY['owner','member','viewer'])),
    PRIMARY KEY ("id"),
    UNIQUE ("org_id", "user_id")
);

CREATE TABLE IF NOT EXISTS "public"."org_invitations" (
    "id" text DEFAULT (extensions.uuid_generate_v4())::text NOT NULL,
    "org_id" text NOT NULL REFERENCES "public"."organizations"("id") ON DELETE CASCADE,
    "email" text NOT NULL,
    "role" text DEFAULT 'member' NOT NULL,
    "token" text NOT NULL,
    "status" text DEFAULT 'pending' NOT NULL,
    "invited_by" uuid NOT NULL REFERENCES auth.users("id"),
    "expires_at" timestamptz NOT NULL,
    "created_at" timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT "org_invitations_role_check" CHECK (role = ANY (ARRAY['member','viewer'])),
    CONSTRAINT "org_invitations_status_check" CHECK (status = ANY (ARRAY['pending','accepted','expired','revoked'])),
    PRIMARY KEY ("id"),
    UNIQUE ("token")
);

-- Add org_id FK to profiles (after organizations exists)
ALTER TABLE "public"."profiles" DROP CONSTRAINT IF EXISTS "profiles_default_org_id_fkey";
ALTER TABLE "public"."profiles" ADD CONSTRAINT "profiles_default_org_id_fkey"
    FOREIGN KEY ("default_org_id") REFERENCES "public"."organizations"("id") ON DELETE SET NULL;

-- Add org_id to projects (required column)
ALTER TABLE "public"."projects" ADD COLUMN IF NOT EXISTS "org_id" text;
ALTER TABLE "public"."projects" ADD COLUMN IF NOT EXISTS "visibility" text DEFAULT 'org' NOT NULL;
ALTER TABLE "public"."projects" DROP CONSTRAINT IF EXISTS "projects_visibility_check";
ALTER TABLE "public"."projects" ADD CONSTRAINT "projects_visibility_check"
    CHECK (visibility = ANY (ARRAY['org','private']));

CREATE TABLE IF NOT EXISTS "public"."project_members" (
    "id" text DEFAULT (extensions.uuid_generate_v4())::text NOT NULL,
    "project_id" text NOT NULL REFERENCES "public"."projects"("id") ON DELETE CASCADE,
    "user_id" uuid NOT NULL REFERENCES auth.users("id") ON DELETE CASCADE,
    "role" text DEFAULT 'editor' NOT NULL,
    "created_at" timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT "project_members_role_check" CHECK (role = ANY (ARRAY['admin','editor','viewer'])),
    PRIMARY KEY ("id"),
    UNIQUE ("project_id", "user_id")
);


-- ============================================================
-- PHASE 8: CREATE NEW TABLES — Version Control & Audit
-- ============================================================

CREATE TABLE IF NOT EXISTS "public"."file_versions" (
    "id" bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    "node_id" text NOT NULL REFERENCES "public"."content_nodes"("id") ON DELETE CASCADE,
    "version" integer NOT NULL,
    "content_json" jsonb,
    "content_text" text,
    "s3_key" text,
    "content_hash" text NOT NULL,
    "size_bytes" bigint DEFAULT 0 NOT NULL,
    "snapshot_id" bigint,
    "operator_type" text NOT NULL,
    "operator_id" text,
    "session_id" text,
    "operation" text NOT NULL,
    "merge_strategy" text,
    "summary" text,
    "created_at" timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT "chk_file_versions_operation" CHECK (operation = ANY (ARRAY['create','update','delete','rollback','merge'])),
    CONSTRAINT "chk_file_versions_operator_type" CHECK (operator_type = ANY (ARRAY['user','agent','system','sync'])),
    UNIQUE ("node_id", "version")
);

CREATE TABLE IF NOT EXISTS "public"."folder_snapshots" (
    "id" bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    "folder_node_id" text NOT NULL REFERENCES "public"."content_nodes"("id") ON DELETE CASCADE,
    "file_versions_map" jsonb NOT NULL,
    "changed_files" jsonb,
    "files_count" integer DEFAULT 0 NOT NULL,
    "changed_count" integer DEFAULT 0 NOT NULL,
    "operator_type" text NOT NULL,
    "operator_id" text,
    "session_id" text,
    "operation" text NOT NULL,
    "summary" text,
    "base_snapshot_id" bigint REFERENCES "public"."folder_snapshots"("id") ON DELETE SET NULL,
    "created_at" timestamptz DEFAULT now() NOT NULL
);

-- Add FK from file_versions to folder_snapshots
ALTER TABLE "public"."file_versions" DROP CONSTRAINT IF EXISTS "fk_file_versions_snapshot";
ALTER TABLE "public"."file_versions" ADD CONSTRAINT "fk_file_versions_snapshot"
    FOREIGN KEY ("snapshot_id") REFERENCES "public"."folder_snapshots"("id") ON DELETE SET NULL;

CREATE SEQUENCE IF NOT EXISTS "public"."audit_logs_id_seq";

CREATE TABLE IF NOT EXISTS "public"."audit_logs" (
    "id" bigint NOT NULL DEFAULT nextval('audit_logs_id_seq'),
    "created_at" timestamptz DEFAULT now() NOT NULL,
    "action" text NOT NULL,
    "node_id" uuid NOT NULL,
    "old_version" integer,
    "new_version" integer,
    "operator_type" text DEFAULT 'user' NOT NULL,
    "operator_id" text,
    "status" text,
    "strategy" text,
    "conflict_details" text,
    "metadata" jsonb,
    PRIMARY KEY ("id")
);

ALTER SEQUENCE "public"."audit_logs_id_seq" OWNED BY "public"."audit_logs"."id";


-- ============================================================
-- PHASE 9: CREATE NEW TABLES — Sync & Endpoints
-- ============================================================

CREATE SEQUENCE IF NOT EXISTS "public"."sync_changelog_id_seq";

CREATE TABLE IF NOT EXISTS "public"."sync_changelog" (
    "id" bigint NOT NULL DEFAULT nextval('sync_changelog_id_seq'),
    "project_id" text NOT NULL,
    "node_id" text NOT NULL,
    "action" text DEFAULT 'update' NOT NULL,
    "node_type" text,
    "version" integer DEFAULT 0 NOT NULL,
    "hash" text,
    "size_bytes" bigint DEFAULT 0,
    "created_at" timestamptz DEFAULT now(),
    "folder_id" text,
    "filename" text,
    CONSTRAINT "chk_sync_changelog_action" CHECK (action = ANY (ARRAY['create','update','delete'])),
    PRIMARY KEY ("id")
);

ALTER SEQUENCE "public"."sync_changelog_id_seq" OWNED BY "public"."sync_changelog"."id";

CREATE TABLE IF NOT EXISTS "public"."sync_runs" (
    "id" text DEFAULT (gen_random_uuid())::text NOT NULL,
    "sync_id" text NOT NULL REFERENCES "public"."connections"("id") ON DELETE CASCADE,
    "status" text DEFAULT 'running' NOT NULL,
    "started_at" timestamptz DEFAULT now() NOT NULL,
    "finished_at" timestamptz,
    "duration_ms" integer,
    "exit_code" integer,
    "stdout" text,
    "error" text,
    "trigger_type" text DEFAULT 'manual',
    "result_summary" text,
    "created_at" timestamptz DEFAULT now() NOT NULL,
    PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS "public"."syncs" (
    "id" text DEFAULT (gen_random_uuid())::text NOT NULL,
    "project_id" text NOT NULL,
    "node_id" text NOT NULL,
    "direction" text DEFAULT 'bidirectional' NOT NULL,
    "provider" text NOT NULL,
    "authority" text DEFAULT 'mirror' NOT NULL,
    "config" jsonb DEFAULT '{}' NOT NULL,
    "credentials_ref" text,
    "access_key" text,
    "trigger" jsonb DEFAULT '{"type": "manual"}' NOT NULL,
    "conflict_strategy" text DEFAULT 'three_way_merge' NOT NULL,
    "status" text DEFAULT 'active' NOT NULL,
    "cursor" bigint DEFAULT 0,
    "last_synced_at" timestamptz,
    "error_message" text,
    "remote_hash" text,
    "last_sync_version" integer DEFAULT 0 NOT NULL,
    "created_at" timestamptz DEFAULT now() NOT NULL,
    "updated_at" timestamptz DEFAULT now() NOT NULL,
    PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS "public"."uploads" (
    "id" text DEFAULT (gen_random_uuid())::text NOT NULL,
    "created_by" uuid,
    "project_id" text NOT NULL REFERENCES "public"."projects"("id") ON DELETE CASCADE,
    "node_id" text,
    "type" text NOT NULL,
    "config" jsonb DEFAULT '{}' NOT NULL,
    "status" text DEFAULT 'pending' NOT NULL,
    "progress" integer DEFAULT 0 NOT NULL,
    "message" text,
    "error" text,
    "result_node_id" text,
    "result" jsonb,
    "created_at" timestamptz DEFAULT now() NOT NULL,
    "updated_at" timestamptz DEFAULT now() NOT NULL,
    "started_at" timestamptz,
    "completed_at" timestamptz,
    CONSTRAINT "chk_uploads_status" CHECK (status = ANY (ARRAY['pending','running','completed','failed','cancelled'])),
    CONSTRAINT "chk_uploads_type" CHECK (type = ANY (ARRAY['file_ocr','file_postprocess','import','search_index'])),
    CONSTRAINT "uploads_progress_check" CHECK (progress >= 0 AND progress <= 100),
    PRIMARY KEY ("id")
);

CREATE TABLE IF NOT EXISTS "public"."sandbox_endpoints" (
    "id" text DEFAULT (gen_random_uuid())::text NOT NULL,
    "project_id" text NOT NULL REFERENCES "public"."projects"("id") ON DELETE CASCADE,
    "node_id" text REFERENCES "public"."content_nodes"("id") ON DELETE SET NULL,
    "name" text DEFAULT 'Sandbox' NOT NULL,
    "description" text,
    "access_key" text NOT NULL,
    "mounts" jsonb DEFAULT '[]' NOT NULL,
    "runtime" text DEFAULT 'alpine' NOT NULL,
    "provider" text DEFAULT 'docker' NOT NULL,
    "timeout_seconds" integer DEFAULT 30 NOT NULL,
    "resource_limits" jsonb DEFAULT '{"memory_mb": 128, "cpu_shares": 0.5}' NOT NULL,
    "status" text DEFAULT 'active' NOT NULL,
    "created_at" timestamptz DEFAULT now() NOT NULL,
    "updated_at" timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT "sandbox_endpoints_provider_check" CHECK (provider = ANY (ARRAY['docker','e2b'])),
    CONSTRAINT "sandbox_endpoints_runtime_check" CHECK (runtime = ANY (ARRAY['alpine','python','node'])),
    CONSTRAINT "sandbox_endpoints_status_check" CHECK (status = ANY (ARRAY['active','paused','error'])),
    PRIMARY KEY ("id"),
    UNIQUE ("access_key")
);

CREATE TABLE IF NOT EXISTS "public"."mcp_endpoints" (
    "id" text DEFAULT (gen_random_uuid())::text NOT NULL,
    "project_id" text NOT NULL REFERENCES "public"."projects"("id") ON DELETE CASCADE,
    "node_id" text REFERENCES "public"."content_nodes"("id") ON DELETE SET NULL,
    "name" text DEFAULT 'MCP Endpoint' NOT NULL,
    "description" text,
    "api_key" text NOT NULL,
    "tools_config" jsonb DEFAULT '[]' NOT NULL,
    "accesses" jsonb DEFAULT '[]' NOT NULL,
    "config" jsonb DEFAULT '{}' NOT NULL,
    "status" text DEFAULT 'active' NOT NULL,
    "created_at" timestamptz DEFAULT now() NOT NULL,
    "updated_at" timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT "mcp_endpoints_status_check" CHECK (status = ANY (ARRAY['active','paused','error'])),
    PRIMARY KEY ("id"),
    UNIQUE ("api_key")
);

CREATE TABLE IF NOT EXISTS "public"."mcps" (
    "id" bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    "api_key" text NOT NULL,
    "user_id" uuid NOT NULL REFERENCES auth.users("id") ON DELETE CASCADE,
    "project_id" text NOT NULL REFERENCES "public"."projects"("id") ON DELETE CASCADE,
    "table_id" text REFERENCES "public"."content_nodes"("id") ON DELETE SET NULL,
    "name" text,
    "json_path" text DEFAULT '' NOT NULL,
    "status" integer DEFAULT 0 NOT NULL,
    "port" integer,
    "docker_info" jsonb,
    "tools_definition" jsonb,
    "register_tools" jsonb,
    "preview_keys" jsonb,
    "created_at" timestamptz DEFAULT now() NOT NULL,
    "updated_at" timestamptz DEFAULT now() NOT NULL
);

CREATE TABLE IF NOT EXISTS "public"."mcp_bindings" (
    "id" bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    "mcp_id" bigint NOT NULL REFERENCES "public"."mcps"("id") ON DELETE CASCADE,
    "tool_id" text NOT NULL REFERENCES "public"."tools"("id") ON DELETE CASCADE,
    "status" boolean DEFAULT true NOT NULL,
    "created_at" timestamptz DEFAULT now() NOT NULL
);


-- ============================================================
-- PHASE 10: CREATE KEY INDEXES
-- ============================================================

CREATE INDEX IF NOT EXISTS "idx_org_members_org" ON "public"."org_members" USING btree ("org_id");
CREATE INDEX IF NOT EXISTS "idx_org_members_user" ON "public"."org_members" USING btree ("user_id");
CREATE INDEX IF NOT EXISTS "idx_project_members_project" ON "public"."project_members" USING btree ("project_id");
CREATE INDEX IF NOT EXISTS "idx_project_members_user" ON "public"."project_members" USING btree ("user_id");
CREATE INDEX IF NOT EXISTS "idx_project_org" ON "public"."projects" USING btree ("org_id");
CREATE INDEX IF NOT EXISTS "idx_tool_org" ON "public"."tools" USING btree ("org_id");
CREATE INDEX IF NOT EXISTS "idx_etl_rule_org" ON "public"."etl_rules" USING btree ("org_id");
CREATE INDEX IF NOT EXISTS "idx_file_versions_node_id" ON "public"."file_versions" USING btree ("node_id");
CREATE INDEX IF NOT EXISTS "idx_file_versions_created_at" ON "public"."file_versions" USING btree ("created_at");
CREATE INDEX IF NOT EXISTS "idx_file_versions_content_hash" ON "public"."file_versions" USING btree ("node_id", "content_hash");
CREATE INDEX IF NOT EXISTS "idx_file_versions_operator" ON "public"."file_versions" USING btree ("operator_type", "operator_id");
CREATE INDEX IF NOT EXISTS "idx_file_versions_snapshot_id" ON "public"."file_versions" USING btree ("snapshot_id");
CREATE INDEX IF NOT EXISTS "idx_folder_snapshots_folder" ON "public"."folder_snapshots" USING btree ("folder_node_id");
CREATE INDEX IF NOT EXISTS "idx_folder_snapshots_created_at" ON "public"."folder_snapshots" USING btree ("created_at");
CREATE INDEX IF NOT EXISTS "idx_folder_snapshots_operator" ON "public"."folder_snapshots" USING btree ("operator_type", "operator_id");
CREATE INDEX IF NOT EXISTS "idx_audit_logs_node_id" ON "public"."audit_logs" USING btree ("node_id", "created_at" DESC);
CREATE INDEX IF NOT EXISTS "idx_audit_logs_action" ON "public"."audit_logs" USING btree ("action", "created_at" DESC);
CREATE INDEX IF NOT EXISTS "idx_audit_logs_operator" ON "public"."audit_logs" USING btree ("operator_type", "operator_id", "created_at" DESC);
CREATE INDEX IF NOT EXISTS "idx_audit_logs_created_at" ON "public"."audit_logs" USING btree ("created_at" DESC);
CREATE INDEX IF NOT EXISTS "idx_sync_changelog_project_seq" ON "public"."sync_changelog" USING btree ("project_id", "id");
CREATE INDEX IF NOT EXISTS "idx_sync_changelog_folder_seq" ON "public"."sync_changelog" USING btree ("folder_id", "id") WHERE folder_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS "idx_sync_changelog_cleanup" ON "public"."sync_changelog" USING btree ("created_at");
CREATE INDEX IF NOT EXISTS "idx_sync_runs_sync_id" ON "public"."sync_runs" USING btree ("sync_id");
CREATE INDEX IF NOT EXISTS "idx_sync_runs_started_at" ON "public"."sync_runs" USING btree ("started_at" DESC);
CREATE INDEX IF NOT EXISTS "idx_syncs_access_key" ON "public"."connections" USING btree ("access_key") WHERE access_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS "idx_syncs_node" ON "public"."connections" USING btree ("node_id");
CREATE INDEX IF NOT EXISTS "idx_syncs_project" ON "public"."connections" USING btree ("project_id");
CREATE INDEX IF NOT EXISTS "idx_syncs_provider" ON "public"."connections" USING btree ("provider");
CREATE INDEX IF NOT EXISTS "idx_syncs_provider_agent" ON "public"."connections" USING btree ("project_id") WHERE provider = 'agent';
CREATE INDEX IF NOT EXISTS "idx_syncs_status" ON "public"."connections" USING btree ("status") WHERE status = 'active';
CREATE INDEX IF NOT EXISTS "idx_syncs_user_id" ON "public"."connections" USING btree ("user_id") WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS "idx_mcp_endpoints_project" ON "public"."mcp_endpoints" USING btree ("project_id");
CREATE INDEX IF NOT EXISTS "idx_mcp_endpoints_node" ON "public"."mcp_endpoints" USING btree ("node_id");
CREATE INDEX IF NOT EXISTS "idx_mcp_endpoints_api_key" ON "public"."mcp_endpoints" USING btree ("api_key");
CREATE INDEX IF NOT EXISTS "idx_sandbox_endpoints_project" ON "public"."sandbox_endpoints" USING btree ("project_id");
CREATE INDEX IF NOT EXISTS "idx_sandbox_endpoints_node" ON "public"."sandbox_endpoints" USING btree ("node_id");
CREATE INDEX IF NOT EXISTS "idx_sandbox_endpoints_access_key" ON "public"."sandbox_endpoints" USING btree ("access_key");
CREATE INDEX IF NOT EXISTS "idx_uploads_project" ON "public"."uploads" USING btree ("project_id");
CREATE INDEX IF NOT EXISTS "idx_uploads_status" ON "public"."uploads" USING btree ("status");
CREATE INDEX IF NOT EXISTS "idx_uploads_type" ON "public"."uploads" USING btree ("type");
CREATE INDEX IF NOT EXISTS "idx_uploads_node" ON "public"."uploads" USING btree ("node_id") WHERE node_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS "idx_uploads_created" ON "public"."uploads" USING btree ("created_at" DESC);
CREATE INDEX IF NOT EXISTS "idx_mcp_bindings_mcp_id" ON "public"."mcp_bindings" USING btree ("mcp_id");
CREATE INDEX IF NOT EXISTS "idx_oauth_connection_user_id" ON "public"."oauth_connections" USING btree ("user_id");
CREATE INDEX IF NOT EXISTS "idx_oauth_connection_provider" ON "public"."oauth_connections" USING btree ("provider");
CREATE INDEX IF NOT EXISTS "idx_agent_execution_log_agent_id" ON "public"."agent_execution_logs" USING btree ("agent_id");
CREATE INDEX IF NOT EXISTS "idx_tool_node_id" ON "public"."tools" USING btree ("node_id");
CREATE INDEX IF NOT EXISTS "idx_tool_project_id" ON "public"."tools" USING btree ("project_id");


-- ============================================================
-- PHASE 11: ENABLE RLS & ADD POLICIES FOR NEW TABLES
-- ============================================================

ALTER TABLE "public"."organizations" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "public"."org_members" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "public"."org_invitations" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "public"."project_members" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "public"."file_versions" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "public"."folder_snapshots" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "public"."mcp_endpoints" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "public"."sandbox_endpoints" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "public"."uploads" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "public"."etl_rules" ENABLE ROW LEVEL SECURITY;

-- Service role policies (backend uses service_role key)
-- Use DROP IF EXISTS + CREATE to be idempotent for Supabase Preview
DROP POLICY IF EXISTS "service_role_all_organizations" ON "public"."organizations";
CREATE POLICY "service_role_all_organizations" ON "public"."organizations" TO service_role USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS "service_role_all_org_members" ON "public"."org_members";
CREATE POLICY "service_role_all_org_members" ON "public"."org_members" TO service_role USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS "service_role_all_org_invitations" ON "public"."org_invitations";
CREATE POLICY "service_role_all_org_invitations" ON "public"."org_invitations" TO service_role USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS "service_role_all_project_members" ON "public"."project_members";
CREATE POLICY "service_role_all_project_members" ON "public"."project_members" TO service_role USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS "service_role_all_file_versions" ON "public"."file_versions";
CREATE POLICY "service_role_all_file_versions" ON "public"."file_versions" TO service_role USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS "service_role_all_folder_snapshots" ON "public"."folder_snapshots";
CREATE POLICY "service_role_all_folder_snapshots" ON "public"."folder_snapshots" TO service_role USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS "service_role_all_mcp_endpoints" ON "public"."mcp_endpoints";
CREATE POLICY "service_role_all_mcp_endpoints" ON "public"."mcp_endpoints" TO service_role USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS "service_role_all_sandbox_endpoints" ON "public"."sandbox_endpoints";
CREATE POLICY "service_role_all_sandbox_endpoints" ON "public"."sandbox_endpoints" TO service_role USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS "service_role_all_uploads" ON "public"."uploads";
CREATE POLICY "service_role_all_uploads" ON "public"."uploads" TO service_role USING (true) WITH CHECK (true);
-- These policies already exist from the pre-rename tables; skip re-creation
-- service_role_all_etl_rule, service_role_all_project, service_role_all_tool,
-- service_role_all_agent_execution_log, service_role_all_oauth_connection
-- carry over automatically after ALTER TABLE RENAME.

DROP POLICY IF EXISTS "service_role_all_sync_changelog" ON "public"."sync_changelog";
CREATE POLICY "service_role_all_sync_changelog" ON "public"."sync_changelog" TO service_role USING (true) WITH CHECK (true);


-- ============================================================
-- END OF MIGRATION
-- ============================================================
-- IMPORTANT: After running, verify with:
--   SELECT table_name FROM information_schema.tables
--   WHERE table_schema = 'public' ORDER BY table_name;
--
-- Also run: backend/sql/diagnostics/check_migration_completeness.sql
-- ============================================================
