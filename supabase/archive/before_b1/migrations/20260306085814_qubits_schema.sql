


SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;


CREATE EXTENSION IF NOT EXISTS "pg_net" WITH SCHEMA "extensions";






COMMENT ON SCHEMA "public" IS 'standard public schema';



CREATE EXTENSION IF NOT EXISTS "pg_graphql" WITH SCHEMA "graphql";






CREATE EXTENSION IF NOT EXISTS "pg_stat_statements" WITH SCHEMA "extensions";






CREATE EXTENSION IF NOT EXISTS "pgcrypto" WITH SCHEMA "extensions";






CREATE EXTENSION IF NOT EXISTS "supabase_vault" WITH SCHEMA "vault";






CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA "extensions";






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

    -- Last segment is the node itself
    node_id := parts[array_length(parts, 1)];

    -- Check: node's ID must not appear in ancestor segments
    FOR i IN 1..array_length(parts, 1) - 1 LOOP
        IF parts[i] = node_id THEN
            RAISE EXCEPTION 'Circular reference: node % cannot be its own ancestor', node_id;
        END IF;
    END LOOP;

    -- Depth sanity check
    IF array_length(parts, 1) > 100 THEN
        RAISE EXCEPTION 'Tree depth exceeded maximum (100) for node %', node_id;
    END IF;

    RETURN NEW;
END;
$$;


ALTER FUNCTION "public"."check_no_cycle"() OWNER TO "postgres";


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


ALTER FUNCTION "public"."count_children_batch"("p_parent_ids" "text"[]) OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."handle_new_user"() RETURNS "trigger"
    LANGUAGE "plpgsql" SECURITY DEFINER
    AS $$
BEGIN
    INSERT INTO public.profiles (user_id, email, display_name)
    VALUES (
        new.id,
        COALESCE(new.email, ''),
        COALESCE(
            new.raw_user_meta_data->>'full_name',
            split_part(new.email, '@', 1),
            'User'
        )
    )
    ON CONFLICT (user_id) DO NOTHING;

    RETURN new;
END;
$$;


ALTER FUNCTION "public"."handle_new_user"() OWNER TO "postgres";


COMMENT ON FUNCTION "public"."handle_new_user"() IS 'Auto-creates a profile record when a new user signs up via any auth method (Email, Google, GitHub, etc.)';



CREATE OR REPLACE FUNCTION "public"."move_node_atomic"("p_node_id" "text", "p_project_id" "text", "p_new_id_path" "text") RETURNS "void"
    LANGUAGE "plpgsql"
    AS $$
DECLARE
    v_old_id_path TEXT;
BEGIN
    SELECT id_path INTO v_old_id_path
    FROM content_nodes
    WHERE id = p_node_id
    FOR UPDATE;

    IF v_old_id_path IS NULL THEN
        RAISE EXCEPTION 'Node not found: %', p_node_id;
    END IF;

    UPDATE content_nodes
    SET id_path = p_new_id_path
    WHERE id = p_node_id;

    UPDATE content_nodes
    SET id_path = p_new_id_path || substring(id_path from length(v_old_id_path) + 1)
    WHERE project_id = p_project_id
      AND id_path LIKE v_old_id_path || '/%';
END;
$$;


ALTER FUNCTION "public"."move_node_atomic"("p_node_id" "text", "p_project_id" "text", "p_new_id_path" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."move_node_atomic"("p_node_id" "text", "p_project_id" "text", "p_new_parent_id" "text", "p_new_id_path" "text") RETURNS "void"
    LANGUAGE "plpgsql"
    AS $$
DECLARE
    v_old_id_path TEXT;
BEGIN
    SELECT id_path INTO v_old_id_path
    FROM content_nodes
    WHERE id = p_node_id
    FOR UPDATE;

    IF v_old_id_path IS NULL THEN
        RAISE EXCEPTION 'Node not found: %', p_node_id;
    END IF;

    -- Update the moved node (trigger auto-sets parent_id from new id_path)
    UPDATE content_nodes
    SET id_path = p_new_id_path
    WHERE id = p_node_id;

    -- Update all descendants (trigger auto-sets each node's parent_id)
    UPDATE content_nodes
    SET id_path = p_new_id_path || substring(id_path from length(v_old_id_path) + 1)
    WHERE project_id = p_project_id
      AND id_path LIKE v_old_id_path || '/%';
END;
$$;


ALTER FUNCTION "public"."move_node_atomic"("p_node_id" "text", "p_project_id" "text", "p_new_parent_id" "text", "p_new_id_path" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."next_version"("p_node_id" "text") RETURNS integer
    LANGUAGE "plpgsql"
    AS $$
DECLARE
    v INT;
BEGIN
    UPDATE content_nodes 
    SET current_version = current_version + 1,
        updated_at = NOW()
    WHERE id = p_node_id
    RETURNING current_version INTO v;
    
    IF v IS NULL THEN
        RAISE EXCEPTION 'Node % not found', p_node_id;
    END IF;
    
    RETURN v;
END;
$$;


ALTER FUNCTION "public"."next_version"("p_node_id" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."parent_path"("p_id_path" "text") RETURNS "text"
    LANGUAGE "sql" IMMUTABLE
    AS $_$
    SELECT CASE
        WHEN p_id_path ~ '^/[^/]+$' THEN '__root__'
        ELSE regexp_replace(p_id_path, '/[^/]+$', '')
    END;
$_$;


ALTER FUNCTION "public"."parent_path"("p_id_path" "text") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."sp_consume_credits"("p_user_id" "uuid", "p_units" integer, "p_request_id" "text", "p_meta" "jsonb" DEFAULT '{}'::"jsonb") RETURNS integer
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'public'
    AS $$
declare
  v_balance integer;
begin
  if p_units <= 0 then
    raise exception 'p_units must be positive';
  end if;

  -- 对用户档案行加锁，串行化同一用户扣费
  perform 1 from public.profiles where user_id = p_user_id for update;

  select coalesce(sum(delta),0) into v_balance
  from public.credit_ledger
  where user_id = p_user_id;

  if v_balance < p_units then
    raise exception 'INSUFFICIENT_CREDITS';
  end if;

  insert into public.credit_ledger (user_id, delta, request_id, meta)
  values (p_user_id, -p_units, p_request_id, coalesce(p_meta, '{}'::jsonb));

  select coalesce(sum(delta),0) into v_balance
  from public.credit_ledger
  where user_id = p_user_id;

  return v_balance;

exception when unique_violation then
  -- 幂等：同一 (user_id, request_id) 重试不重复扣
  select coalesce(sum(delta),0) into v_balance
  from public.credit_ledger
  where user_id = p_user_id;
  return v_balance;
end;
$$;


ALTER FUNCTION "public"."sp_consume_credits"("p_user_id" "uuid", "p_units" integer, "p_request_id" "text", "p_meta" "jsonb") OWNER TO "postgres";


CREATE OR REPLACE FUNCTION "public"."sp_grant_credits"("p_user_id" "uuid", "p_units" integer, "p_request_id" "text", "p_meta" "jsonb" DEFAULT '{}'::"jsonb") RETURNS integer
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'public'
    AS $$
declare
  v_balance integer;
begin
  if p_units <= 0 then
    raise exception 'p_units must be positive';
  end if;

  begin
    insert into public.credit_ledger (user_id, delta, request_id, meta)
    values (p_user_id, p_units, p_request_id, coalesce(p_meta, '{}'::jsonb));
  exception when unique_violation then
    -- 幂等：同一 (user_id, request_id) 重试忽略
    null;
  end;

  select coalesce(sum(delta),0) into v_balance
  from public.credit_ledger
  where user_id = p_user_id;

  return v_balance;
end;
$$;


ALTER FUNCTION "public"."sp_grant_credits"("p_user_id" "uuid", "p_units" integer, "p_request_id" "text", "p_meta" "jsonb") OWNER TO "postgres";

SET default_tablespace = '';

SET default_table_access_method = "heap";


CREATE TABLE IF NOT EXISTS "public"."access_logs" (
    "id" bigint NOT NULL,
    "node_id" "text",
    "node_type" "text",
    "node_name" "text",
    "user_id" "uuid",
    "agent_id" "text",
    "session_id" "text",
    "project_id" "text",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."access_logs" OWNER TO "postgres";


ALTER TABLE "public"."access_logs" ALTER COLUMN "id" ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME "public"."access_logs_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);



CREATE TABLE IF NOT EXISTS "public"."agent_execution_logs" (
    "id" bigint NOT NULL,
    "agent_id" "text" NOT NULL,
    "trigger_type" "text",
    "trigger_source" "text",
    "status" "text" DEFAULT 'running'::"text" NOT NULL,
    "started_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "finished_at" timestamp with time zone,
    "duration_ms" bigint,
    "input_snapshot" "jsonb",
    "output_summary" "text",
    "output_snapshot" "jsonb",
    "error_message" "text"
);


ALTER TABLE "public"."agent_execution_logs" OWNER TO "postgres";


ALTER TABLE "public"."agent_execution_logs" ALTER COLUMN "id" ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME "public"."agent_execution_log_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);



CREATE TABLE IF NOT EXISTS "public"."agent_logs" (
    "id" bigint NOT NULL,
    "call_type" "text" NOT NULL,
    "user_id" "uuid",
    "agent_id" "text",
    "session_id" "text",
    "success" boolean DEFAULT true NOT NULL,
    "latency_ms" bigint,
    "error_message" "text",
    "details" "jsonb",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."agent_logs" OWNER TO "postgres";


ALTER TABLE "public"."agent_logs" ALTER COLUMN "id" ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME "public"."agent_logs_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);



CREATE TABLE IF NOT EXISTS "public"."api_keys" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "user_id" "uuid" NOT NULL,
    "name" "text" DEFAULT 'default'::"text" NOT NULL,
    "prefix" "text" NOT NULL,
    "salt" "text" NOT NULL,
    "secret_hash" "text" NOT NULL,
    "scopes" "text"[] DEFAULT ARRAY['research:invoke'::"text"] NOT NULL,
    "last_used_at" timestamp with time zone,
    "expires_at" timestamp with time zone,
    "revoked_at" timestamp with time zone,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "secret_plain" "text"
);


ALTER TABLE "public"."api_keys" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."audit_logs" (
    "id" bigint NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "action" "text" NOT NULL,
    "node_id" "uuid" NOT NULL,
    "old_version" integer,
    "new_version" integer,
    "operator_type" "text" DEFAULT 'user'::"text" NOT NULL,
    "operator_id" "text",
    "status" "text",
    "strategy" "text",
    "conflict_details" "text",
    "metadata" "jsonb"
);


ALTER TABLE "public"."audit_logs" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."audit_logs_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."audit_logs_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."audit_logs_id_seq" OWNED BY "public"."audit_logs"."id";



CREATE TABLE IF NOT EXISTS "public"."chat_messages" (
    "id" "text" DEFAULT ("extensions"."uuid_generate_v4"())::"text" NOT NULL,
    "session_id" "text" NOT NULL,
    "role" "text" NOT NULL,
    "content" "text",
    "parts" "jsonb",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."chat_messages" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."chat_sessions" (
    "id" "text" DEFAULT ("extensions"."uuid_generate_v4"())::"text" NOT NULL,
    "user_id" "uuid" NOT NULL,
    "agent_id" "text",
    "title" "text",
    "mode" "text" DEFAULT 'agent'::"text",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."chat_sessions" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."chunks" (
    "id" bigint NOT NULL,
    "node_id" "text" NOT NULL,
    "json_pointer" "text" DEFAULT ''::"text" NOT NULL,
    "chunk_index" integer DEFAULT 0 NOT NULL,
    "total_chunks" integer DEFAULT 1 NOT NULL,
    "chunk_text" "text" NOT NULL,
    "char_start" integer DEFAULT 0 NOT NULL,
    "char_end" integer DEFAULT 0 NOT NULL,
    "content_hash" "text" DEFAULT ''::"text" NOT NULL,
    "turbopuffer_namespace" "text",
    "turbopuffer_doc_id" "text",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."chunks" OWNER TO "postgres";


ALTER TABLE "public"."chunks" ALTER COLUMN "id" ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME "public"."chunks_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);



CREATE TABLE IF NOT EXISTS "public"."connection_accesses" (
    "id" "text" DEFAULT ("extensions"."uuid_generate_v4"())::"text" NOT NULL,
    "connection_id" "text" NOT NULL,
    "node_id" "text" NOT NULL,
    "json_path" "text" DEFAULT ''::"text" NOT NULL,
    "readonly" boolean DEFAULT true NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "permission" "text" DEFAULT 'r'::"text" NOT NULL,
    CONSTRAINT "chk_agent_bash_permission" CHECK (("permission" = ANY (ARRAY['r'::"text", 'ra'::"text", 'rw-'::"text", 'rw'::"text"])))
);


ALTER TABLE "public"."connection_accesses" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."connection_tools" (
    "id" "text" DEFAULT ("extensions"."uuid_generate_v4"())::"text" NOT NULL,
    "connection_id" "text" NOT NULL,
    "tool_id" "text" NOT NULL,
    "enabled" boolean DEFAULT true NOT NULL,
    "mcp_exposed" boolean DEFAULT false NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."connection_tools" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."connections" (
    "id" "text" DEFAULT ("gen_random_uuid"())::"text" NOT NULL,
    "project_id" "text" NOT NULL,
    "node_id" "text",
    "direction" "text" NOT NULL,
    "provider" "text" NOT NULL,
    "authority" "text" DEFAULT 'mirror'::"text" NOT NULL,
    "config" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "credentials_ref" "text",
    "access_key" "text",
    "trigger" "jsonb" DEFAULT '{"type": "manual"}'::"jsonb" NOT NULL,
    "conflict_strategy" "text" DEFAULT 'three_way_merge'::"text" NOT NULL,
    "status" "text" DEFAULT 'active'::"text" NOT NULL,
    "cursor" bigint DEFAULT 0,
    "last_synced_at" timestamp with time zone,
    "error_message" "text",
    "remote_hash" "text",
    "last_sync_version" integer DEFAULT 0 NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "user_id" "uuid",
    CONSTRAINT "chk_syncs_authority" CHECK (("authority" = ANY (ARRAY['authoritative'::"text", 'mirror'::"text"]))),
    CONSTRAINT "chk_syncs_conflict_strategy" CHECK (("conflict_strategy" = ANY (ARRAY['source_wins'::"text", 'three_way_merge'::"text", 'lww'::"text"]))),
    CONSTRAINT "chk_syncs_direction" CHECK (("direction" = ANY (ARRAY['inbound'::"text", 'outbound'::"text", 'bidirectional'::"text"]))),
    CONSTRAINT "chk_syncs_status" CHECK (("status" = ANY (ARRAY['active'::"text", 'paused'::"text", 'error'::"text", 'syncing'::"text"])))
);


ALTER TABLE "public"."connections" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."content_nodes" (
    "id" "text" DEFAULT ("extensions"."uuid_generate_v4"())::"text" NOT NULL,
    "project_id" "text" NOT NULL,
    "created_by" "uuid",
    "name" "text" NOT NULL,
    "type" "text" NOT NULL,
    "id_path" "text" DEFAULT '/'::"text" NOT NULL,
    "preview_json" "jsonb",
    "preview_md" "text",
    "s3_key" "text",
    "mime_type" "text",
    "size_bytes" bigint DEFAULT 0 NOT NULL,
    "permissions" "jsonb" DEFAULT '{"inherit": true}'::"jsonb" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "current_version" integer DEFAULT 0 NOT NULL,
    "content_hash" "text",
    "depth" integer GENERATED ALWAYS AS ("array_length"("string_to_array"(TRIM(BOTH '/'::"text" FROM "id_path"), '/'::"text"), 1)) STORED,
    CONSTRAINT "chk_content_nodes_type" CHECK (("type" = ANY (ARRAY['folder'::"text", 'json'::"text", 'markdown'::"text", 'file'::"text"])))
);


ALTER TABLE "public"."content_nodes" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."context_publish" (
    "id" bigint NOT NULL,
    "created_by" "uuid",
    "table_id" "text" NOT NULL,
    "json_path" "text" DEFAULT ''::"text" NOT NULL,
    "publish_key" "text" NOT NULL,
    "status" boolean DEFAULT true NOT NULL,
    "expires_at" timestamp with time zone,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."context_publish" OWNER TO "postgres";


ALTER TABLE "public"."context_publish" ALTER COLUMN "id" ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME "public"."context_publish_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);



CREATE TABLE IF NOT EXISTS "public"."credit_ledger" (
    "id" bigint NOT NULL,
    "user_id" "uuid" NOT NULL,
    "delta" integer NOT NULL,
    "request_id" "text" NOT NULL,
    "meta" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "api_key_prefix" "text" GENERATED ALWAYS AS (("meta" ->> 'api_key_prefix'::"text")) STORED
);


ALTER TABLE "public"."credit_ledger" OWNER TO "postgres";


CREATE OR REPLACE VIEW "public"."credit_balance" AS
 SELECT "user_id",
    COALESCE("sum"("delta"), (0)::bigint) AS "balance"
   FROM "public"."credit_ledger"
  GROUP BY "user_id";


ALTER VIEW "public"."credit_balance" OWNER TO "postgres";


CREATE SEQUENCE IF NOT EXISTS "public"."credit_ledger_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."credit_ledger_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."credit_ledger_id_seq" OWNED BY "public"."credit_ledger"."id";



CREATE OR REPLACE VIEW "public"."credit_usage_by_prefix" AS
 SELECT "user_id",
    "api_key_prefix",
    (- "sum"("delta")) AS "used"
   FROM "public"."credit_ledger"
  WHERE (("delta" < 0) AND ("api_key_prefix" IS NOT NULL))
  GROUP BY "user_id", "api_key_prefix";


ALTER VIEW "public"."credit_usage_by_prefix" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."db_connections" (
    "id" "text" DEFAULT ("extensions"."uuid_generate_v4"())::"text" NOT NULL,
    "created_by" "uuid",
    "project_id" "text" NOT NULL,
    "name" "text" NOT NULL,
    "provider" "text" DEFAULT 'supabase'::"text" NOT NULL,
    "config" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "is_active" boolean DEFAULT true NOT NULL,
    "last_used_at" timestamp with time zone,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."db_connections" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."etl_rules" (
    "id" bigint NOT NULL,
    "created_by" "uuid",
    "name" "text" NOT NULL,
    "description" "text",
    "json_schema" "jsonb",
    "system_prompt" "text",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "org_id" "text" NOT NULL
);


ALTER TABLE "public"."etl_rules" OWNER TO "postgres";


ALTER TABLE "public"."etl_rules" ALTER COLUMN "id" ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME "public"."etl_rule_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);



CREATE TABLE IF NOT EXISTS "public"."file_versions" (
    "id" bigint NOT NULL,
    "node_id" "text" NOT NULL,
    "version" integer NOT NULL,
    "content_json" "jsonb",
    "content_text" "text",
    "s3_key" "text",
    "content_hash" "text" NOT NULL,
    "size_bytes" bigint DEFAULT 0 NOT NULL,
    "snapshot_id" bigint,
    "operator_type" "text" NOT NULL,
    "operator_id" "text",
    "session_id" "text",
    "operation" "text" NOT NULL,
    "merge_strategy" "text",
    "summary" "text",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "chk_file_versions_operation" CHECK (("operation" = ANY (ARRAY['create'::"text", 'update'::"text", 'delete'::"text", 'rollback'::"text", 'merge'::"text"]))),
    CONSTRAINT "chk_file_versions_operator_type" CHECK (("operator_type" = ANY (ARRAY['user'::"text", 'agent'::"text", 'system'::"text", 'sync'::"text"])))
);


ALTER TABLE "public"."file_versions" OWNER TO "postgres";


ALTER TABLE "public"."file_versions" ALTER COLUMN "id" ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME "public"."file_versions_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);



CREATE TABLE IF NOT EXISTS "public"."folder_snapshots" (
    "id" bigint NOT NULL,
    "folder_node_id" "text" NOT NULL,
    "file_versions_map" "jsonb" NOT NULL,
    "changed_files" "jsonb",
    "files_count" integer DEFAULT 0 NOT NULL,
    "changed_count" integer DEFAULT 0 NOT NULL,
    "operator_type" "text" NOT NULL,
    "operator_id" "text",
    "session_id" "text",
    "operation" "text" NOT NULL,
    "summary" "text",
    "base_snapshot_id" bigint,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."folder_snapshots" OWNER TO "postgres";


ALTER TABLE "public"."folder_snapshots" ALTER COLUMN "id" ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME "public"."folder_snapshots_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);



CREATE TABLE IF NOT EXISTS "public"."mcp" (
    "id" bigint NOT NULL,
    "api_key" "text" NOT NULL,
    "created_by" "uuid",
    "project_id" "text" NOT NULL,
    "table_id" "text",
    "name" "text",
    "json_path" "text" DEFAULT ''::"text" NOT NULL,
    "status" integer DEFAULT 0 NOT NULL,
    "port" integer,
    "docker_info" "jsonb",
    "tools_definition" "jsonb",
    "register_tools" "jsonb",
    "preview_keys" "jsonb",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."mcp" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."mcp_binding" (
    "id" bigint NOT NULL,
    "mcp_id" bigint NOT NULL,
    "tool_id" "text" NOT NULL,
    "status" boolean DEFAULT true NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."mcp_binding" OWNER TO "postgres";


ALTER TABLE "public"."mcp_binding" ALTER COLUMN "id" ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME "public"."mcp_binding_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);



CREATE TABLE IF NOT EXISTS "public"."mcp_bindings" (
    "id" bigint NOT NULL,
    "mcp_id" bigint NOT NULL,
    "tool_id" "text" NOT NULL,
    "status" boolean DEFAULT true NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."mcp_bindings" OWNER TO "postgres";


ALTER TABLE "public"."mcp_bindings" ALTER COLUMN "id" ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME "public"."mcp_bindings_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);



CREATE TABLE IF NOT EXISTS "public"."mcp_endpoints" (
    "id" "text" DEFAULT ("gen_random_uuid"())::"text" NOT NULL,
    "project_id" "text" NOT NULL,
    "node_id" "text",
    "name" "text" DEFAULT 'MCP Endpoint'::"text" NOT NULL,
    "description" "text",
    "api_key" "text" NOT NULL,
    "tools_config" "jsonb" DEFAULT '[]'::"jsonb" NOT NULL,
    "accesses" "jsonb" DEFAULT '[]'::"jsonb" NOT NULL,
    "config" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "status" "text" DEFAULT 'active'::"text" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "mcp_endpoints_status_check" CHECK (("status" = ANY (ARRAY['active'::"text", 'paused'::"text", 'error'::"text"])))
);


ALTER TABLE "public"."mcp_endpoints" OWNER TO "postgres";


ALTER TABLE "public"."mcp" ALTER COLUMN "id" ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME "public"."mcp_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);



CREATE TABLE IF NOT EXISTS "public"."mcps" (
    "id" bigint NOT NULL,
    "api_key" "text" NOT NULL,
    "user_id" "uuid" NOT NULL,
    "project_id" "text" NOT NULL,
    "table_id" "text",
    "name" "text",
    "json_path" "text" DEFAULT ''::"text" NOT NULL,
    "status" integer DEFAULT 0 NOT NULL,
    "port" integer,
    "docker_info" "jsonb",
    "tools_definition" "jsonb",
    "register_tools" "jsonb",
    "preview_keys" "jsonb",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."mcps" OWNER TO "postgres";


ALTER TABLE "public"."mcps" ALTER COLUMN "id" ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME "public"."mcps_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);



CREATE TABLE IF NOT EXISTS "public"."messages" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "thread_id" "uuid" NOT NULL,
    "user_id" "uuid" NOT NULL,
    "role" "text" NOT NULL,
    "content" "text" NOT NULL,
    "metadata" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "messages_role_check" CHECK (("role" = ANY (ARRAY['user'::"text", 'assistant'::"text", 'system'::"text"])))
);


ALTER TABLE "public"."messages" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."oauth_connections" (
    "id" bigint NOT NULL,
    "user_id" "uuid" NOT NULL,
    "provider" "text" NOT NULL,
    "access_token" "text" NOT NULL,
    "refresh_token" "text",
    "token_type" "text",
    "expires_at" timestamp with time zone,
    "workspace_id" "text",
    "workspace_name" "text",
    "bot_id" "text",
    "metadata" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."oauth_connections" OWNER TO "postgres";


ALTER TABLE "public"."oauth_connections" ALTER COLUMN "id" ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME "public"."oauth_connection_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);



CREATE TABLE IF NOT EXISTS "public"."org_invitations" (
    "id" "text" DEFAULT ("extensions"."uuid_generate_v4"())::"text" NOT NULL,
    "org_id" "text" NOT NULL,
    "email" "text" NOT NULL,
    "role" "text" DEFAULT 'member'::"text" NOT NULL,
    "token" "text" NOT NULL,
    "status" "text" DEFAULT 'pending'::"text" NOT NULL,
    "invited_by" "uuid" NOT NULL,
    "expires_at" timestamp with time zone NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "org_invitations_role_check" CHECK (("role" = ANY (ARRAY['member'::"text", 'viewer'::"text"]))),
    CONSTRAINT "org_invitations_status_check" CHECK (("status" = ANY (ARRAY['pending'::"text", 'accepted'::"text", 'expired'::"text", 'revoked'::"text"])))
);


ALTER TABLE "public"."org_invitations" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."org_members" (
    "id" "text" DEFAULT ("extensions"."uuid_generate_v4"())::"text" NOT NULL,
    "org_id" "text" NOT NULL,
    "user_id" "uuid" NOT NULL,
    "role" "text" DEFAULT 'member'::"text" NOT NULL,
    "joined_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "org_members_role_check" CHECK (("role" = ANY (ARRAY['owner'::"text", 'member'::"text", 'viewer'::"text"])))
);


ALTER TABLE "public"."org_members" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."organizations" (
    "id" "text" DEFAULT ("extensions"."uuid_generate_v4"())::"text" NOT NULL,
    "name" "text" NOT NULL,
    "slug" "text" NOT NULL,
    "avatar_url" "text",
    "type" "text" DEFAULT 'personal'::"text" NOT NULL,
    "plan" "text" DEFAULT 'free'::"text" NOT NULL,
    "seat_limit" integer DEFAULT 1 NOT NULL,
    "stripe_customer_id" "text",
    "stripe_subscription_id" "text",
    "created_by" "uuid" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "organizations_plan_check" CHECK (("plan" = ANY (ARRAY['free'::"text", 'plus'::"text", 'pro'::"text", 'team'::"text", 'enterprise'::"text"]))),
    CONSTRAINT "organizations_type_check" CHECK (("type" = ANY (ARRAY['personal'::"text", 'team'::"text"])))
);


ALTER TABLE "public"."organizations" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."profiles" (
    "user_id" "uuid" NOT NULL,
    "email" "text" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "has_onboarded" boolean DEFAULT false NOT NULL,
    "onboarded_at" timestamp with time zone,
    "demo_project_id" "text",
    "display_name" "text",
    "avatar_url" "text",
    "default_org_id" "text"
);


ALTER TABLE "public"."profiles" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."project_members" (
    "id" "text" DEFAULT ("extensions"."uuid_generate_v4"())::"text" NOT NULL,
    "project_id" "text" NOT NULL,
    "user_id" "uuid" NOT NULL,
    "role" "text" DEFAULT 'editor'::"text" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "project_members_role_check" CHECK (("role" = ANY (ARRAY['admin'::"text", 'editor'::"text", 'viewer'::"text"])))
);


ALTER TABLE "public"."project_members" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."projects" (
    "id" "text" DEFAULT ("extensions"."uuid_generate_v4"())::"text" NOT NULL,
    "name" "text" NOT NULL,
    "description" "text",
    "created_by" "uuid",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "org_id" "text" NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "visibility" "text" DEFAULT 'org'::"text" NOT NULL,
    CONSTRAINT "projects_visibility_check" CHECK (("visibility" = ANY (ARRAY['org'::"text", 'private'::"text"])))
);


ALTER TABLE "public"."projects" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."sandbox_endpoints" (
    "id" "text" DEFAULT ("gen_random_uuid"())::"text" NOT NULL,
    "project_id" "text" NOT NULL,
    "node_id" "text",
    "name" "text" DEFAULT 'Sandbox'::"text" NOT NULL,
    "description" "text",
    "access_key" "text" NOT NULL,
    "mounts" "jsonb" DEFAULT '[]'::"jsonb" NOT NULL,
    "runtime" "text" DEFAULT 'alpine'::"text" NOT NULL,
    "provider" "text" DEFAULT 'docker'::"text" NOT NULL,
    "timeout_seconds" integer DEFAULT 30 NOT NULL,
    "resource_limits" "jsonb" DEFAULT '{"memory_mb": 128, "cpu_shares": 0.5}'::"jsonb" NOT NULL,
    "status" "text" DEFAULT 'active'::"text" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "sandbox_endpoints_provider_check" CHECK (("provider" = ANY (ARRAY['docker'::"text", 'e2b'::"text"]))),
    CONSTRAINT "sandbox_endpoints_runtime_check" CHECK (("runtime" = ANY (ARRAY['alpine'::"text", 'python'::"text", 'node'::"text"]))),
    CONSTRAINT "sandbox_endpoints_status_check" CHECK (("status" = ANY (ARRAY['active'::"text", 'paused'::"text", 'error'::"text"])))
);


ALTER TABLE "public"."sandbox_endpoints" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."subscriptions" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "user_id" "uuid" NOT NULL,
    "provider" "text" DEFAULT 'stripe'::"text" NOT NULL,
    "status" "text" NOT NULL,
    "seat_type" "text" DEFAULT 'individual'::"text" NOT NULL,
    "current_period_end" timestamp with time zone,
    "metadata" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."subscriptions" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."sync_changelog" (
    "id" bigint NOT NULL,
    "project_id" "text" NOT NULL,
    "node_id" "text" NOT NULL,
    "action" "text" DEFAULT 'update'::"text" NOT NULL,
    "node_type" "text",
    "version" integer DEFAULT 0 NOT NULL,
    "hash" "text",
    "size_bytes" bigint DEFAULT 0,
    "created_at" timestamp with time zone DEFAULT "now"(),
    "folder_id" "text",
    "filename" "text",
    CONSTRAINT "chk_sync_changelog_action" CHECK (("action" = ANY (ARRAY['create'::"text", 'update'::"text", 'delete'::"text"])))
);


ALTER TABLE "public"."sync_changelog" OWNER TO "postgres";


COMMENT ON TABLE "public"."sync_changelog" IS 'Append-only change log for cursor-based incremental sync. Each row represents a content_node mutation. Clients store the last-seen id as their cursor and pull only newer entries. Rows older than 30 days are periodically cleaned up; expired cursors trigger a full-sync reset.';



CREATE SEQUENCE IF NOT EXISTS "public"."sync_changelog_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."sync_changelog_id_seq" OWNER TO "postgres";


ALTER SEQUENCE "public"."sync_changelog_id_seq" OWNED BY "public"."sync_changelog"."id";



CREATE TABLE IF NOT EXISTS "public"."sync_runs" (
    "id" "text" DEFAULT ("gen_random_uuid"())::"text" NOT NULL,
    "sync_id" "text" NOT NULL,
    "status" "text" DEFAULT 'running'::"text" NOT NULL,
    "started_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "finished_at" timestamp with time zone,
    "duration_ms" integer,
    "exit_code" integer,
    "stdout" "text",
    "error" "text",
    "trigger_type" "text" DEFAULT 'manual'::"text",
    "result_summary" "text",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."sync_runs" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."syncs" (
    "id" "text" DEFAULT ("gen_random_uuid"())::"text" NOT NULL,
    "project_id" "text" NOT NULL,
    "node_id" "text" NOT NULL,
    "direction" "text" DEFAULT 'bidirectional'::"text" NOT NULL,
    "provider" "text" NOT NULL,
    "authority" "text" DEFAULT 'mirror'::"text" NOT NULL,
    "config" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "credentials_ref" "text",
    "access_key" "text",
    "trigger" "jsonb" DEFAULT '{"type": "manual"}'::"jsonb" NOT NULL,
    "conflict_strategy" "text" DEFAULT 'three_way_merge'::"text" NOT NULL,
    "status" "text" DEFAULT 'active'::"text" NOT NULL,
    "cursor" bigint DEFAULT 0,
    "last_synced_at" timestamp with time zone,
    "error_message" "text",
    "remote_hash" "text",
    "last_sync_version" integer DEFAULT 0 NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."syncs" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."threads" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "user_id" "uuid" NOT NULL,
    "title" "text",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."threads" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."tools" (
    "id" "text" DEFAULT ("extensions"."uuid_generate_v4"())::"text" NOT NULL,
    "created_by" "uuid",
    "project_id" "text",
    "node_id" "text",
    "json_path" "text" DEFAULT ''::"text" NOT NULL,
    "type" "text" NOT NULL,
    "name" "text" NOT NULL,
    "alias" "text",
    "description" "text",
    "input_schema" "jsonb",
    "output_schema" "jsonb",
    "metadata" "jsonb",
    "category" "text" DEFAULT 'builtin'::"text" NOT NULL,
    "script_type" "text",
    "script_content" "text",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "org_id" "text" NOT NULL
);


ALTER TABLE "public"."tools" OWNER TO "postgres";


CREATE TABLE IF NOT EXISTS "public"."uploads" (
    "id" "text" DEFAULT ("gen_random_uuid"())::"text" NOT NULL,
    "created_by" "uuid",
    "project_id" "text" NOT NULL,
    "node_id" "text",
    "type" "text" NOT NULL,
    "config" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "status" "text" DEFAULT 'pending'::"text" NOT NULL,
    "progress" integer DEFAULT 0 NOT NULL,
    "message" "text",
    "error" "text",
    "result_node_id" "text",
    "result" "jsonb",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "started_at" timestamp with time zone,
    "completed_at" timestamp with time zone,
    CONSTRAINT "chk_uploads_status" CHECK (("status" = ANY (ARRAY['pending'::"text", 'running'::"text", 'completed'::"text", 'failed'::"text", 'cancelled'::"text"]))),
    CONSTRAINT "chk_uploads_type" CHECK (("type" = ANY (ARRAY['file_ocr'::"text", 'file_postprocess'::"text", 'import'::"text", 'search_index'::"text"]))),
    CONSTRAINT "uploads_progress_check" CHECK ((("progress" >= 0) AND ("progress" <= 100)))
);


ALTER TABLE "public"."uploads" OWNER TO "postgres";


ALTER TABLE ONLY "public"."audit_logs" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."audit_logs_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."credit_ledger" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."credit_ledger_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."sync_changelog" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."sync_changelog_id_seq"'::"regclass");



ALTER TABLE ONLY "public"."access_logs"
    ADD CONSTRAINT "access_logs_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."connection_accesses"
    ADD CONSTRAINT "agent_bash_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."agent_execution_logs"
    ADD CONSTRAINT "agent_execution_log_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."agent_logs"
    ADD CONSTRAINT "agent_logs_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."connection_tools"
    ADD CONSTRAINT "agent_tool_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."api_keys"
    ADD CONSTRAINT "api_keys_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."api_keys"
    ADD CONSTRAINT "api_keys_prefix_unique" UNIQUE ("prefix");



ALTER TABLE ONLY "public"."audit_logs"
    ADD CONSTRAINT "audit_logs_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."chat_messages"
    ADD CONSTRAINT "chat_messages_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."chat_sessions"
    ADD CONSTRAINT "chat_sessions_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."chunks"
    ADD CONSTRAINT "chunks_node_id_json_pointer_content_hash_chunk_index_key" UNIQUE ("node_id", "json_pointer", "content_hash", "chunk_index");



ALTER TABLE ONLY "public"."chunks"
    ADD CONSTRAINT "chunks_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."connection_accesses"
    ADD CONSTRAINT "connection_access_connection_id_node_id_json_path_key" UNIQUE ("connection_id", "node_id", "json_path");



ALTER TABLE ONLY "public"."connection_tools"
    ADD CONSTRAINT "connection_tool_connection_id_tool_id_key" UNIQUE ("connection_id", "tool_id");



ALTER TABLE ONLY "public"."content_nodes"
    ADD CONSTRAINT "content_nodes_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."context_publish"
    ADD CONSTRAINT "context_publish_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."context_publish"
    ADD CONSTRAINT "context_publish_publish_key_key" UNIQUE ("publish_key");



ALTER TABLE ONLY "public"."credit_ledger"
    ADD CONSTRAINT "credit_ledger_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."credit_ledger"
    ADD CONSTRAINT "credit_ledger_request_id_unique" UNIQUE ("user_id", "request_id");



ALTER TABLE ONLY "public"."db_connections"
    ADD CONSTRAINT "db_connections_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."etl_rules"
    ADD CONSTRAINT "etl_rule_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."file_versions"
    ADD CONSTRAINT "file_versions_node_id_version_key" UNIQUE ("node_id", "version");



ALTER TABLE ONLY "public"."file_versions"
    ADD CONSTRAINT "file_versions_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."folder_snapshots"
    ADD CONSTRAINT "folder_snapshots_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."mcp_binding"
    ADD CONSTRAINT "mcp_binding_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."mcp_bindings"
    ADD CONSTRAINT "mcp_bindings_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."mcp_endpoints"
    ADD CONSTRAINT "mcp_endpoints_api_key_key" UNIQUE ("api_key");



ALTER TABLE ONLY "public"."mcp_endpoints"
    ADD CONSTRAINT "mcp_endpoints_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."mcp"
    ADD CONSTRAINT "mcp_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."mcps"
    ADD CONSTRAINT "mcps_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."messages"
    ADD CONSTRAINT "messages_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."oauth_connections"
    ADD CONSTRAINT "oauth_connection_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."org_invitations"
    ADD CONSTRAINT "org_invitations_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."org_invitations"
    ADD CONSTRAINT "org_invitations_token_key" UNIQUE ("token");



ALTER TABLE ONLY "public"."org_members"
    ADD CONSTRAINT "org_members_org_id_user_id_key" UNIQUE ("org_id", "user_id");



ALTER TABLE ONLY "public"."org_members"
    ADD CONSTRAINT "org_members_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."organizations"
    ADD CONSTRAINT "organizations_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."organizations"
    ADD CONSTRAINT "organizations_slug_key" UNIQUE ("slug");



ALTER TABLE ONLY "public"."profiles"
    ADD CONSTRAINT "profiles_pkey" PRIMARY KEY ("user_id");



ALTER TABLE ONLY "public"."project_members"
    ADD CONSTRAINT "project_members_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."project_members"
    ADD CONSTRAINT "project_members_project_id_user_id_key" UNIQUE ("project_id", "user_id");



ALTER TABLE ONLY "public"."projects"
    ADD CONSTRAINT "project_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."sandbox_endpoints"
    ADD CONSTRAINT "sandbox_endpoints_access_key_key" UNIQUE ("access_key");



ALTER TABLE ONLY "public"."sandbox_endpoints"
    ADD CONSTRAINT "sandbox_endpoints_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."subscriptions"
    ADD CONSTRAINT "subscriptions_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."sync_changelog"
    ADD CONSTRAINT "sync_changelog_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."sync_runs"
    ADD CONSTRAINT "sync_runs_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."connections"
    ADD CONSTRAINT "syncs_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."syncs"
    ADD CONSTRAINT "syncs_pkey1" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."threads"
    ADD CONSTRAINT "threads_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."tools"
    ADD CONSTRAINT "tool_pkey" PRIMARY KEY ("id");



ALTER TABLE ONLY "public"."uploads"
    ADD CONSTRAINT "uploads_pkey" PRIMARY KEY ("id");



CREATE INDEX "idx_access_logs_created_at" ON "public"."access_logs" USING "btree" ("created_at");



CREATE INDEX "idx_access_logs_project_id" ON "public"."access_logs" USING "btree" ("project_id");



CREATE INDEX "idx_agent_execution_log_agent_id" ON "public"."agent_execution_logs" USING "btree" ("agent_id");



CREATE INDEX "idx_agent_logs_agent_id" ON "public"."agent_logs" USING "btree" ("agent_id");



CREATE INDEX "idx_agent_logs_created_at" ON "public"."agent_logs" USING "btree" ("created_at");



CREATE INDEX "idx_api_keys_user" ON "public"."api_keys" USING "btree" ("user_id", "created_at" DESC);



CREATE INDEX "idx_audit_logs_action" ON "public"."audit_logs" USING "btree" ("action", "created_at" DESC);



CREATE INDEX "idx_audit_logs_created_at" ON "public"."audit_logs" USING "btree" ("created_at" DESC);



CREATE INDEX "idx_audit_logs_node_id" ON "public"."audit_logs" USING "btree" ("node_id", "created_at" DESC);



CREATE INDEX "idx_audit_logs_operator" ON "public"."audit_logs" USING "btree" ("operator_type", "operator_id", "created_at" DESC);



CREATE INDEX "idx_chat_messages_session_id" ON "public"."chat_messages" USING "btree" ("session_id");



CREATE INDEX "idx_chat_sessions_agent_id" ON "public"."chat_sessions" USING "btree" ("agent_id");



CREATE INDEX "idx_chat_sessions_user_id" ON "public"."chat_sessions" USING "btree" ("user_id");



CREATE INDEX "idx_chunks_node_id" ON "public"."chunks" USING "btree" ("node_id");



CREATE INDEX "idx_content_nodes_children_lookup" ON "public"."content_nodes" USING "btree" ("project_id", "depth", "id_path" "text_pattern_ops");



CREATE INDEX "idx_content_nodes_id_path" ON "public"."content_nodes" USING "btree" ("id_path");



CREATE INDEX "idx_content_nodes_project_depth" ON "public"."content_nodes" USING "btree" ("project_id", "depth");



CREATE INDEX "idx_content_nodes_project_id" ON "public"."content_nodes" USING "btree" ("project_id");



CREATE INDEX "idx_content_nodes_type" ON "public"."content_nodes" USING "btree" ("type");



CREATE UNIQUE INDEX "idx_content_nodes_unique_name_v2" ON "public"."content_nodes" USING "btree" ("project_id", "public"."parent_path"("id_path"), "name");



CREATE INDEX "idx_credit_ledger_user_time" ON "public"."credit_ledger" USING "btree" ("user_id", "created_at" DESC);



CREATE INDEX "idx_db_connections_project_id" ON "public"."db_connections" USING "btree" ("project_id");



CREATE INDEX "idx_etl_rule_org" ON "public"."etl_rules" USING "btree" ("org_id");



CREATE INDEX "idx_file_versions_content_hash" ON "public"."file_versions" USING "btree" ("node_id", "content_hash");



CREATE INDEX "idx_file_versions_created_at" ON "public"."file_versions" USING "btree" ("created_at");



CREATE INDEX "idx_file_versions_node_id" ON "public"."file_versions" USING "btree" ("node_id");



CREATE INDEX "idx_file_versions_operator" ON "public"."file_versions" USING "btree" ("operator_type", "operator_id");



CREATE INDEX "idx_file_versions_snapshot_id" ON "public"."file_versions" USING "btree" ("snapshot_id");



CREATE INDEX "idx_folder_snapshots_created_at" ON "public"."folder_snapshots" USING "btree" ("created_at");



CREATE INDEX "idx_folder_snapshots_folder" ON "public"."folder_snapshots" USING "btree" ("folder_node_id");



CREATE INDEX "idx_folder_snapshots_operator" ON "public"."folder_snapshots" USING "btree" ("operator_type", "operator_id");



CREATE INDEX "idx_ledger_user_prefix_neg" ON "public"."credit_ledger" USING "btree" ("user_id", "api_key_prefix") WHERE ("delta" < 0);



CREATE INDEX "idx_mcp_binding_mcp_id" ON "public"."mcp_binding" USING "btree" ("mcp_id");



CREATE INDEX "idx_mcp_bindings_mcp_id" ON "public"."mcp_bindings" USING "btree" ("mcp_id");



CREATE INDEX "idx_mcp_endpoints_api_key" ON "public"."mcp_endpoints" USING "btree" ("api_key");



CREATE INDEX "idx_mcp_endpoints_node" ON "public"."mcp_endpoints" USING "btree" ("node_id");



CREATE INDEX "idx_mcp_endpoints_project" ON "public"."mcp_endpoints" USING "btree" ("project_id");



CREATE INDEX "idx_messages_thread_time" ON "public"."messages" USING "btree" ("thread_id", "created_at");



CREATE INDEX "idx_oauth_connection_provider" ON "public"."oauth_connections" USING "btree" ("provider");



CREATE INDEX "idx_oauth_connection_user_id" ON "public"."oauth_connections" USING "btree" ("user_id");



CREATE INDEX "idx_org_members_org" ON "public"."org_members" USING "btree" ("org_id");



CREATE INDEX "idx_org_members_user" ON "public"."org_members" USING "btree" ("user_id");



CREATE INDEX "idx_project_members_project" ON "public"."project_members" USING "btree" ("project_id");



CREATE INDEX "idx_project_members_user" ON "public"."project_members" USING "btree" ("user_id");



CREATE INDEX "idx_project_org" ON "public"."projects" USING "btree" ("org_id");



CREATE INDEX "idx_sandbox_endpoints_access_key" ON "public"."sandbox_endpoints" USING "btree" ("access_key");



CREATE INDEX "idx_sandbox_endpoints_node" ON "public"."sandbox_endpoints" USING "btree" ("node_id");



CREATE INDEX "idx_sandbox_endpoints_project" ON "public"."sandbox_endpoints" USING "btree" ("project_id");



CREATE INDEX "idx_subscriptions_user_id" ON "public"."subscriptions" USING "btree" ("user_id");



CREATE INDEX "idx_sync_changelog_cleanup" ON "public"."sync_changelog" USING "btree" ("created_at");



CREATE INDEX "idx_sync_changelog_folder_seq" ON "public"."sync_changelog" USING "btree" ("folder_id", "id") WHERE ("folder_id" IS NOT NULL);



CREATE INDEX "idx_sync_changelog_project_seq" ON "public"."sync_changelog" USING "btree" ("project_id", "id");



CREATE INDEX "idx_sync_runs_started_at" ON "public"."sync_runs" USING "btree" ("started_at" DESC);



CREATE INDEX "idx_sync_runs_sync_id" ON "public"."sync_runs" USING "btree" ("sync_id");



CREATE UNIQUE INDEX "idx_syncs_access_key" ON "public"."connections" USING "btree" ("access_key") WHERE ("access_key" IS NOT NULL);



CREATE INDEX "idx_syncs_node" ON "public"."connections" USING "btree" ("node_id");



CREATE UNIQUE INDEX "idx_syncs_one_authority_per_node" ON "public"."connections" USING "btree" ("node_id") WHERE ("authority" = 'authoritative'::"text");



CREATE INDEX "idx_syncs_project" ON "public"."connections" USING "btree" ("project_id");



CREATE INDEX "idx_syncs_provider" ON "public"."connections" USING "btree" ("provider");



CREATE INDEX "idx_syncs_provider_agent" ON "public"."connections" USING "btree" ("project_id") WHERE ("provider" = 'agent'::"text");



CREATE INDEX "idx_syncs_status" ON "public"."connections" USING "btree" ("status") WHERE ("status" = 'active'::"text");



CREATE INDEX "idx_syncs_user_id" ON "public"."connections" USING "btree" ("user_id") WHERE ("user_id" IS NOT NULL);



CREATE INDEX "idx_threads_user" ON "public"."threads" USING "btree" ("user_id", "created_at" DESC);



CREATE INDEX "idx_tool_node_id" ON "public"."tools" USING "btree" ("node_id");



CREATE INDEX "idx_tool_org" ON "public"."tools" USING "btree" ("org_id");



CREATE INDEX "idx_tool_project_id" ON "public"."tools" USING "btree" ("project_id");



CREATE INDEX "idx_uploads_created" ON "public"."uploads" USING "btree" ("created_at" DESC);



CREATE INDEX "idx_uploads_node" ON "public"."uploads" USING "btree" ("node_id") WHERE ("node_id" IS NOT NULL);



CREATE INDEX "idx_uploads_project" ON "public"."uploads" USING "btree" ("project_id");



CREATE INDEX "idx_uploads_status" ON "public"."uploads" USING "btree" ("status");



CREATE INDEX "idx_uploads_type" ON "public"."uploads" USING "btree" ("type");



CREATE INDEX "idx_usage_by_user_prefix" ON "public"."credit_ledger" USING "btree" ("user_id", "api_key_prefix") WHERE ("delta" < 0);



CREATE OR REPLACE TRIGGER "trg_check_no_cycle" BEFORE INSERT OR UPDATE OF "id_path" ON "public"."content_nodes" FOR EACH ROW EXECUTE FUNCTION "public"."check_no_cycle"();



ALTER TABLE ONLY "public"."access_logs"
    ADD CONSTRAINT "access_logs_agent_id_fkey" FOREIGN KEY ("agent_id") REFERENCES "public"."connections"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."access_logs"
    ADD CONSTRAINT "access_logs_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."access_logs"
    ADD CONSTRAINT "access_logs_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."connection_accesses"
    ADD CONSTRAINT "agent_bash_agent_id_fkey" FOREIGN KEY ("connection_id") REFERENCES "public"."connections"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."connection_accesses"
    ADD CONSTRAINT "agent_bash_node_id_fkey" FOREIGN KEY ("node_id") REFERENCES "public"."content_nodes"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."agent_execution_logs"
    ADD CONSTRAINT "agent_execution_log_agent_id_fkey" FOREIGN KEY ("agent_id") REFERENCES "public"."connections"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."agent_logs"
    ADD CONSTRAINT "agent_logs_agent_id_fkey" FOREIGN KEY ("agent_id") REFERENCES "public"."connections"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."agent_logs"
    ADD CONSTRAINT "agent_logs_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."connection_tools"
    ADD CONSTRAINT "agent_tool_agent_id_fkey" FOREIGN KEY ("connection_id") REFERENCES "public"."connections"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."connection_tools"
    ADD CONSTRAINT "agent_tool_tool_id_fkey" FOREIGN KEY ("tool_id") REFERENCES "public"."tools"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."api_keys"
    ADD CONSTRAINT "api_keys_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."chat_messages"
    ADD CONSTRAINT "chat_messages_session_id_fkey" FOREIGN KEY ("session_id") REFERENCES "public"."chat_sessions"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."chat_sessions"
    ADD CONSTRAINT "chat_sessions_agent_id_fkey" FOREIGN KEY ("agent_id") REFERENCES "public"."connections"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."chat_sessions"
    ADD CONSTRAINT "chat_sessions_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."chunks"
    ADD CONSTRAINT "chunks_node_id_fkey" FOREIGN KEY ("node_id") REFERENCES "public"."content_nodes"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."content_nodes"
    ADD CONSTRAINT "content_nodes_created_by_fkey" FOREIGN KEY ("created_by") REFERENCES "auth"."users"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."content_nodes"
    ADD CONSTRAINT "content_nodes_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."context_publish"
    ADD CONSTRAINT "context_publish_table_id_fkey" FOREIGN KEY ("table_id") REFERENCES "public"."content_nodes"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."context_publish"
    ADD CONSTRAINT "context_publish_user_id_fkey" FOREIGN KEY ("created_by") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."credit_ledger"
    ADD CONSTRAINT "credit_ledger_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."db_connections"
    ADD CONSTRAINT "db_connections_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."db_connections"
    ADD CONSTRAINT "db_connections_user_id_fkey" FOREIGN KEY ("created_by") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."etl_rules"
    ADD CONSTRAINT "etl_rule_org_id_fkey" FOREIGN KEY ("org_id") REFERENCES "public"."organizations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."etl_rules"
    ADD CONSTRAINT "etl_rule_user_id_fkey" FOREIGN KEY ("created_by") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."file_versions"
    ADD CONSTRAINT "file_versions_node_id_fkey" FOREIGN KEY ("node_id") REFERENCES "public"."content_nodes"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."file_versions"
    ADD CONSTRAINT "fk_file_versions_snapshot" FOREIGN KEY ("snapshot_id") REFERENCES "public"."folder_snapshots"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."folder_snapshots"
    ADD CONSTRAINT "folder_snapshots_base_snapshot_id_fkey" FOREIGN KEY ("base_snapshot_id") REFERENCES "public"."folder_snapshots"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."folder_snapshots"
    ADD CONSTRAINT "folder_snapshots_folder_node_id_fkey" FOREIGN KEY ("folder_node_id") REFERENCES "public"."content_nodes"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."mcp_binding"
    ADD CONSTRAINT "mcp_binding_mcp_id_fkey" FOREIGN KEY ("mcp_id") REFERENCES "public"."mcp"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."mcp_binding"
    ADD CONSTRAINT "mcp_binding_tool_id_fkey" FOREIGN KEY ("tool_id") REFERENCES "public"."tools"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."mcp_bindings"
    ADD CONSTRAINT "mcp_bindings_mcp_id_fkey" FOREIGN KEY ("mcp_id") REFERENCES "public"."mcps"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."mcp_bindings"
    ADD CONSTRAINT "mcp_bindings_tool_id_fkey" FOREIGN KEY ("tool_id") REFERENCES "public"."tools"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."mcp_endpoints"
    ADD CONSTRAINT "mcp_endpoints_node_id_fkey" FOREIGN KEY ("node_id") REFERENCES "public"."content_nodes"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."mcp_endpoints"
    ADD CONSTRAINT "mcp_endpoints_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."mcp"
    ADD CONSTRAINT "mcp_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."mcp"
    ADD CONSTRAINT "mcp_table_id_fkey" FOREIGN KEY ("table_id") REFERENCES "public"."content_nodes"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."mcp"
    ADD CONSTRAINT "mcp_user_id_fkey" FOREIGN KEY ("created_by") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."mcps"
    ADD CONSTRAINT "mcps_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."mcps"
    ADD CONSTRAINT "mcps_table_id_fkey" FOREIGN KEY ("table_id") REFERENCES "public"."content_nodes"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."mcps"
    ADD CONSTRAINT "mcps_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."messages"
    ADD CONSTRAINT "messages_thread_id_fkey" FOREIGN KEY ("thread_id") REFERENCES "public"."threads"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."messages"
    ADD CONSTRAINT "messages_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."oauth_connections"
    ADD CONSTRAINT "oauth_connection_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."org_invitations"
    ADD CONSTRAINT "org_invitations_invited_by_fkey" FOREIGN KEY ("invited_by") REFERENCES "auth"."users"("id");



ALTER TABLE ONLY "public"."org_invitations"
    ADD CONSTRAINT "org_invitations_org_id_fkey" FOREIGN KEY ("org_id") REFERENCES "public"."organizations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."org_members"
    ADD CONSTRAINT "org_members_org_id_fkey" FOREIGN KEY ("org_id") REFERENCES "public"."organizations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."org_members"
    ADD CONSTRAINT "org_members_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "public"."profiles"("user_id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."organizations"
    ADD CONSTRAINT "organizations_created_by_fkey" FOREIGN KEY ("created_by") REFERENCES "auth"."users"("id");



ALTER TABLE ONLY "public"."profiles"
    ADD CONSTRAINT "profiles_default_org_id_fkey" FOREIGN KEY ("default_org_id") REFERENCES "public"."organizations"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."profiles"
    ADD CONSTRAINT "profiles_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."project_members"
    ADD CONSTRAINT "project_members_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."project_members"
    ADD CONSTRAINT "project_members_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."projects"
    ADD CONSTRAINT "project_org_id_fkey" FOREIGN KEY ("org_id") REFERENCES "public"."organizations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."projects"
    ADD CONSTRAINT "project_user_id_fkey" FOREIGN KEY ("created_by") REFERENCES "auth"."users"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."sandbox_endpoints"
    ADD CONSTRAINT "sandbox_endpoints_node_id_fkey" FOREIGN KEY ("node_id") REFERENCES "public"."content_nodes"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."sandbox_endpoints"
    ADD CONSTRAINT "sandbox_endpoints_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."subscriptions"
    ADD CONSTRAINT "subscriptions_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."sync_runs"
    ADD CONSTRAINT "sync_runs_sync_id_fkey" FOREIGN KEY ("sync_id") REFERENCES "public"."connections"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."connections"
    ADD CONSTRAINT "syncs_node_id_fkey" FOREIGN KEY ("node_id") REFERENCES "public"."content_nodes"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."connections"
    ADD CONSTRAINT "syncs_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."connections"
    ADD CONSTRAINT "syncs_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."threads"
    ADD CONSTRAINT "threads_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."tools"
    ADD CONSTRAINT "tool_node_id_fkey" FOREIGN KEY ("node_id") REFERENCES "public"."content_nodes"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."tools"
    ADD CONSTRAINT "tool_org_id_fkey" FOREIGN KEY ("org_id") REFERENCES "public"."organizations"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."tools"
    ADD CONSTRAINT "tool_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."tools"
    ADD CONSTRAINT "tool_user_id_fkey" FOREIGN KEY ("created_by") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."uploads"
    ADD CONSTRAINT "uploads_node_id_fkey" FOREIGN KEY ("node_id") REFERENCES "public"."content_nodes"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."uploads"
    ADD CONSTRAINT "uploads_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE CASCADE;



ALTER TABLE ONLY "public"."uploads"
    ADD CONSTRAINT "uploads_result_node_id_fkey" FOREIGN KEY ("result_node_id") REFERENCES "public"."content_nodes"("id") ON DELETE SET NULL;



ALTER TABLE ONLY "public"."uploads"
    ADD CONSTRAINT "uploads_user_id_fkey" FOREIGN KEY ("created_by") REFERENCES "auth"."users"("id") ON DELETE CASCADE;



ALTER TABLE "public"."access_logs" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."agent_execution_logs" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."agent_logs" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."api_keys" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "api_keys_select" ON "public"."api_keys" FOR SELECT USING (("user_id" = "auth"."uid"()));



CREATE POLICY "authenticated_select_own_project_nodes" ON "public"."content_nodes" FOR SELECT TO "authenticated" USING (("project_id" IN ( SELECT "projects"."id"
   FROM "public"."projects"
  WHERE ("projects"."created_by" = "auth"."uid"()))));



ALTER TABLE "public"."chat_messages" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "chat_messages_service_role" ON "public"."chat_messages" TO "service_role" USING (true) WITH CHECK (true);



ALTER TABLE "public"."chat_sessions" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "chat_sessions_service_role" ON "public"."chat_sessions" TO "service_role" USING (true) WITH CHECK (true);



ALTER TABLE "public"."chunks" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."connection_accesses" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."connection_tools" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."connections" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."content_nodes" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."context_publish" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."credit_ledger" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "credit_ledger_select" ON "public"."credit_ledger" FOR SELECT USING (("user_id" = "auth"."uid"()));



ALTER TABLE "public"."db_connections" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."etl_rules" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."file_versions" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."folder_snapshots" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."mcp" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."mcp_binding" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."mcp_endpoints" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."messages" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "messages_delete" ON "public"."messages" FOR DELETE USING ((EXISTS ( SELECT 1
   FROM "public"."threads" "t"
  WHERE (("t"."id" = "messages"."thread_id") AND ("t"."user_id" = "auth"."uid"())))));



CREATE POLICY "messages_insert" ON "public"."messages" FOR INSERT WITH CHECK ((("user_id" = "auth"."uid"()) AND (EXISTS ( SELECT 1
   FROM "public"."threads" "t"
  WHERE (("t"."id" = "messages"."thread_id") AND ("t"."user_id" = "auth"."uid"()))))));



CREATE POLICY "messages_select" ON "public"."messages" FOR SELECT USING ((EXISTS ( SELECT 1
   FROM "public"."threads" "t"
  WHERE (("t"."id" = "messages"."thread_id") AND ("t"."user_id" = "auth"."uid"())))));



CREATE POLICY "messages_update" ON "public"."messages" FOR UPDATE USING ((EXISTS ( SELECT 1
   FROM "public"."threads" "t"
  WHERE (("t"."id" = "messages"."thread_id") AND ("t"."user_id" = "auth"."uid"()))))) WITH CHECK ((("user_id" = "auth"."uid"()) AND (EXISTS ( SELECT 1
   FROM "public"."threads" "t"
  WHERE (("t"."id" = "messages"."thread_id") AND ("t"."user_id" = "auth"."uid"()))))));



ALTER TABLE "public"."oauth_connections" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."org_invitations" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."org_members" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."organizations" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."profiles" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "profiles_select" ON "public"."profiles" FOR SELECT USING (("user_id" = "auth"."uid"()));



ALTER TABLE "public"."project_members" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."projects" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."sandbox_endpoints" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "service_role_all_access_logs" ON "public"."access_logs" TO "service_role" USING (true) WITH CHECK (true);



CREATE POLICY "service_role_all_agent_bash" ON "public"."connection_accesses" TO "service_role" USING (true) WITH CHECK (true);



CREATE POLICY "service_role_all_agent_execution_log" ON "public"."agent_execution_logs" TO "service_role" USING (true) WITH CHECK (true);



CREATE POLICY "service_role_all_agent_logs" ON "public"."agent_logs" TO "service_role" USING (true) WITH CHECK (true);



CREATE POLICY "service_role_all_agent_tool" ON "public"."connection_tools" TO "service_role" USING (true) WITH CHECK (true);



CREATE POLICY "service_role_all_chat_messages" ON "public"."chat_messages" TO "service_role" USING (true) WITH CHECK (true);



CREATE POLICY "service_role_all_chat_sessions" ON "public"."chat_sessions" TO "service_role" USING (true) WITH CHECK (true);



CREATE POLICY "service_role_all_chunks" ON "public"."chunks" TO "service_role" USING (true) WITH CHECK (true);



CREATE POLICY "service_role_all_content_nodes" ON "public"."content_nodes" TO "service_role" USING (true) WITH CHECK (true);



CREATE POLICY "service_role_all_context_publish" ON "public"."context_publish" TO "service_role" USING (true) WITH CHECK (true);



CREATE POLICY "service_role_all_db_connections" ON "public"."db_connections" TO "service_role" USING (true) WITH CHECK (true);



CREATE POLICY "service_role_all_etl_rule" ON "public"."etl_rules" TO "service_role" USING (true) WITH CHECK (true);



CREATE POLICY "service_role_all_file_versions" ON "public"."file_versions" TO "service_role" USING (true) WITH CHECK (true);



CREATE POLICY "service_role_all_folder_snapshots" ON "public"."folder_snapshots" TO "service_role" USING (true) WITH CHECK (true);



CREATE POLICY "service_role_all_mcp" ON "public"."mcp" TO "service_role" USING (true) WITH CHECK (true);



CREATE POLICY "service_role_all_mcp_binding" ON "public"."mcp_binding" TO "service_role" USING (true) WITH CHECK (true);



CREATE POLICY "service_role_all_mcp_endpoints" ON "public"."mcp_endpoints" TO "service_role" USING (true) WITH CHECK (true);



CREATE POLICY "service_role_all_oauth_connection" ON "public"."oauth_connections" TO "service_role" USING (true) WITH CHECK (true);



CREATE POLICY "service_role_all_org_invitations" ON "public"."org_invitations" TO "service_role" USING (true) WITH CHECK (true);



CREATE POLICY "service_role_all_org_members" ON "public"."org_members" TO "service_role" USING (true) WITH CHECK (true);



CREATE POLICY "service_role_all_organizations" ON "public"."organizations" TO "service_role" USING (true) WITH CHECK (true);



CREATE POLICY "service_role_all_profiles" ON "public"."profiles" TO "service_role" USING (true) WITH CHECK (true);



CREATE POLICY "service_role_all_project" ON "public"."projects" TO "service_role" USING (true) WITH CHECK (true);



CREATE POLICY "service_role_all_project_members" ON "public"."project_members" TO "service_role" USING (true) WITH CHECK (true);



CREATE POLICY "service_role_all_sandbox_endpoints" ON "public"."sandbox_endpoints" TO "service_role" USING (true) WITH CHECK (true);



CREATE POLICY "service_role_all_sync_changelog" ON "public"."sync_changelog" TO "service_role" USING (true) WITH CHECK (true);



CREATE POLICY "service_role_all_syncs" ON "public"."connections" TO "service_role" USING (true) WITH CHECK (true);



CREATE POLICY "service_role_all_tool" ON "public"."tools" TO "service_role" USING (true) WITH CHECK (true);



CREATE POLICY "service_role_all_uploads" ON "public"."uploads" TO "service_role" USING (true) WITH CHECK (true);



ALTER TABLE "public"."subscriptions" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "subscriptions_select" ON "public"."subscriptions" FOR SELECT USING (("user_id" = "auth"."uid"()));



ALTER TABLE "public"."sync_changelog" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."threads" ENABLE ROW LEVEL SECURITY;


CREATE POLICY "threads_delete" ON "public"."threads" FOR DELETE USING (("user_id" = "auth"."uid"()));



CREATE POLICY "threads_insert" ON "public"."threads" FOR INSERT WITH CHECK (("user_id" = "auth"."uid"()));



CREATE POLICY "threads_select" ON "public"."threads" FOR SELECT USING (("user_id" = "auth"."uid"()));



CREATE POLICY "threads_update" ON "public"."threads" FOR UPDATE USING (("user_id" = "auth"."uid"())) WITH CHECK (("user_id" = "auth"."uid"()));



ALTER TABLE "public"."tools" ENABLE ROW LEVEL SECURITY;


ALTER TABLE "public"."uploads" ENABLE ROW LEVEL SECURITY;




ALTER PUBLICATION "supabase_realtime" OWNER TO "postgres";






ALTER PUBLICATION "supabase_realtime" ADD TABLE ONLY "public"."content_nodes";






GRANT USAGE ON SCHEMA "public" TO "postgres";
GRANT USAGE ON SCHEMA "public" TO "anon";
GRANT USAGE ON SCHEMA "public" TO "authenticated";
GRANT USAGE ON SCHEMA "public" TO "service_role";

























































































































































GRANT ALL ON FUNCTION "public"."check_no_cycle"() TO "anon";
GRANT ALL ON FUNCTION "public"."check_no_cycle"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."check_no_cycle"() TO "service_role";



GRANT ALL ON FUNCTION "public"."count_children_batch"("p_parent_ids" "text"[]) TO "anon";
GRANT ALL ON FUNCTION "public"."count_children_batch"("p_parent_ids" "text"[]) TO "authenticated";
GRANT ALL ON FUNCTION "public"."count_children_batch"("p_parent_ids" "text"[]) TO "service_role";



GRANT ALL ON FUNCTION "public"."handle_new_user"() TO "anon";
GRANT ALL ON FUNCTION "public"."handle_new_user"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."handle_new_user"() TO "service_role";



GRANT ALL ON FUNCTION "public"."move_node_atomic"("p_node_id" "text", "p_project_id" "text", "p_new_id_path" "text") TO "anon";
GRANT ALL ON FUNCTION "public"."move_node_atomic"("p_node_id" "text", "p_project_id" "text", "p_new_id_path" "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."move_node_atomic"("p_node_id" "text", "p_project_id" "text", "p_new_id_path" "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."move_node_atomic"("p_node_id" "text", "p_project_id" "text", "p_new_parent_id" "text", "p_new_id_path" "text") TO "anon";
GRANT ALL ON FUNCTION "public"."move_node_atomic"("p_node_id" "text", "p_project_id" "text", "p_new_parent_id" "text", "p_new_id_path" "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."move_node_atomic"("p_node_id" "text", "p_project_id" "text", "p_new_parent_id" "text", "p_new_id_path" "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."next_version"("p_node_id" "text") TO "anon";
GRANT ALL ON FUNCTION "public"."next_version"("p_node_id" "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."next_version"("p_node_id" "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."parent_path"("p_id_path" "text") TO "anon";
GRANT ALL ON FUNCTION "public"."parent_path"("p_id_path" "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."parent_path"("p_id_path" "text") TO "service_role";



GRANT ALL ON FUNCTION "public"."sp_consume_credits"("p_user_id" "uuid", "p_units" integer, "p_request_id" "text", "p_meta" "jsonb") TO "anon";
GRANT ALL ON FUNCTION "public"."sp_consume_credits"("p_user_id" "uuid", "p_units" integer, "p_request_id" "text", "p_meta" "jsonb") TO "authenticated";
GRANT ALL ON FUNCTION "public"."sp_consume_credits"("p_user_id" "uuid", "p_units" integer, "p_request_id" "text", "p_meta" "jsonb") TO "service_role";



GRANT ALL ON FUNCTION "public"."sp_grant_credits"("p_user_id" "uuid", "p_units" integer, "p_request_id" "text", "p_meta" "jsonb") TO "anon";
GRANT ALL ON FUNCTION "public"."sp_grant_credits"("p_user_id" "uuid", "p_units" integer, "p_request_id" "text", "p_meta" "jsonb") TO "authenticated";
GRANT ALL ON FUNCTION "public"."sp_grant_credits"("p_user_id" "uuid", "p_units" integer, "p_request_id" "text", "p_meta" "jsonb") TO "service_role";


















GRANT ALL ON TABLE "public"."access_logs" TO "anon";
GRANT ALL ON TABLE "public"."access_logs" TO "authenticated";
GRANT ALL ON TABLE "public"."access_logs" TO "service_role";



GRANT ALL ON SEQUENCE "public"."access_logs_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."access_logs_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."access_logs_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."agent_execution_logs" TO "anon";
GRANT ALL ON TABLE "public"."agent_execution_logs" TO "authenticated";
GRANT ALL ON TABLE "public"."agent_execution_logs" TO "service_role";



GRANT ALL ON SEQUENCE "public"."agent_execution_log_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."agent_execution_log_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."agent_execution_log_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."agent_logs" TO "anon";
GRANT ALL ON TABLE "public"."agent_logs" TO "authenticated";
GRANT ALL ON TABLE "public"."agent_logs" TO "service_role";



GRANT ALL ON SEQUENCE "public"."agent_logs_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."agent_logs_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."agent_logs_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."api_keys" TO "anon";
GRANT ALL ON TABLE "public"."api_keys" TO "authenticated";
GRANT ALL ON TABLE "public"."api_keys" TO "service_role";



GRANT ALL ON TABLE "public"."audit_logs" TO "anon";
GRANT ALL ON TABLE "public"."audit_logs" TO "authenticated";
GRANT ALL ON TABLE "public"."audit_logs" TO "service_role";



GRANT ALL ON SEQUENCE "public"."audit_logs_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."audit_logs_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."audit_logs_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."chat_messages" TO "anon";
GRANT ALL ON TABLE "public"."chat_messages" TO "authenticated";
GRANT ALL ON TABLE "public"."chat_messages" TO "service_role";



GRANT ALL ON TABLE "public"."chat_sessions" TO "anon";
GRANT ALL ON TABLE "public"."chat_sessions" TO "authenticated";
GRANT ALL ON TABLE "public"."chat_sessions" TO "service_role";



GRANT ALL ON TABLE "public"."chunks" TO "anon";
GRANT ALL ON TABLE "public"."chunks" TO "authenticated";
GRANT ALL ON TABLE "public"."chunks" TO "service_role";



GRANT ALL ON SEQUENCE "public"."chunks_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."chunks_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."chunks_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."connection_accesses" TO "anon";
GRANT ALL ON TABLE "public"."connection_accesses" TO "authenticated";
GRANT ALL ON TABLE "public"."connection_accesses" TO "service_role";



GRANT ALL ON TABLE "public"."connection_tools" TO "anon";
GRANT ALL ON TABLE "public"."connection_tools" TO "authenticated";
GRANT ALL ON TABLE "public"."connection_tools" TO "service_role";



GRANT ALL ON TABLE "public"."connections" TO "anon";
GRANT ALL ON TABLE "public"."connections" TO "authenticated";
GRANT ALL ON TABLE "public"."connections" TO "service_role";



GRANT ALL ON TABLE "public"."content_nodes" TO "anon";
GRANT ALL ON TABLE "public"."content_nodes" TO "authenticated";
GRANT ALL ON TABLE "public"."content_nodes" TO "service_role";



GRANT ALL ON TABLE "public"."context_publish" TO "anon";
GRANT ALL ON TABLE "public"."context_publish" TO "authenticated";
GRANT ALL ON TABLE "public"."context_publish" TO "service_role";



GRANT ALL ON SEQUENCE "public"."context_publish_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."context_publish_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."context_publish_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."credit_ledger" TO "anon";
GRANT ALL ON TABLE "public"."credit_ledger" TO "authenticated";
GRANT ALL ON TABLE "public"."credit_ledger" TO "service_role";



GRANT ALL ON TABLE "public"."credit_balance" TO "anon";
GRANT ALL ON TABLE "public"."credit_balance" TO "authenticated";
GRANT ALL ON TABLE "public"."credit_balance" TO "service_role";



GRANT ALL ON SEQUENCE "public"."credit_ledger_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."credit_ledger_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."credit_ledger_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."credit_usage_by_prefix" TO "anon";
GRANT ALL ON TABLE "public"."credit_usage_by_prefix" TO "authenticated";
GRANT ALL ON TABLE "public"."credit_usage_by_prefix" TO "service_role";



GRANT ALL ON TABLE "public"."db_connections" TO "anon";
GRANT ALL ON TABLE "public"."db_connections" TO "authenticated";
GRANT ALL ON TABLE "public"."db_connections" TO "service_role";



GRANT ALL ON TABLE "public"."etl_rules" TO "anon";
GRANT ALL ON TABLE "public"."etl_rules" TO "authenticated";
GRANT ALL ON TABLE "public"."etl_rules" TO "service_role";



GRANT ALL ON SEQUENCE "public"."etl_rule_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."etl_rule_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."etl_rule_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."file_versions" TO "anon";
GRANT ALL ON TABLE "public"."file_versions" TO "authenticated";
GRANT ALL ON TABLE "public"."file_versions" TO "service_role";



GRANT ALL ON SEQUENCE "public"."file_versions_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."file_versions_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."file_versions_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."folder_snapshots" TO "anon";
GRANT ALL ON TABLE "public"."folder_snapshots" TO "authenticated";
GRANT ALL ON TABLE "public"."folder_snapshots" TO "service_role";



GRANT ALL ON SEQUENCE "public"."folder_snapshots_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."folder_snapshots_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."folder_snapshots_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."mcp" TO "anon";
GRANT ALL ON TABLE "public"."mcp" TO "authenticated";
GRANT ALL ON TABLE "public"."mcp" TO "service_role";



GRANT ALL ON TABLE "public"."mcp_binding" TO "anon";
GRANT ALL ON TABLE "public"."mcp_binding" TO "authenticated";
GRANT ALL ON TABLE "public"."mcp_binding" TO "service_role";



GRANT ALL ON SEQUENCE "public"."mcp_binding_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."mcp_binding_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."mcp_binding_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."mcp_bindings" TO "anon";
GRANT ALL ON TABLE "public"."mcp_bindings" TO "authenticated";
GRANT ALL ON TABLE "public"."mcp_bindings" TO "service_role";



GRANT ALL ON SEQUENCE "public"."mcp_bindings_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."mcp_bindings_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."mcp_bindings_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."mcp_endpoints" TO "anon";
GRANT ALL ON TABLE "public"."mcp_endpoints" TO "authenticated";
GRANT ALL ON TABLE "public"."mcp_endpoints" TO "service_role";



GRANT ALL ON SEQUENCE "public"."mcp_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."mcp_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."mcp_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."mcps" TO "anon";
GRANT ALL ON TABLE "public"."mcps" TO "authenticated";
GRANT ALL ON TABLE "public"."mcps" TO "service_role";



GRANT ALL ON SEQUENCE "public"."mcps_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."mcps_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."mcps_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."messages" TO "anon";
GRANT ALL ON TABLE "public"."messages" TO "authenticated";
GRANT ALL ON TABLE "public"."messages" TO "service_role";



GRANT ALL ON TABLE "public"."oauth_connections" TO "anon";
GRANT ALL ON TABLE "public"."oauth_connections" TO "authenticated";
GRANT ALL ON TABLE "public"."oauth_connections" TO "service_role";



GRANT ALL ON SEQUENCE "public"."oauth_connection_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."oauth_connection_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."oauth_connection_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."org_invitations" TO "anon";
GRANT ALL ON TABLE "public"."org_invitations" TO "authenticated";
GRANT ALL ON TABLE "public"."org_invitations" TO "service_role";



GRANT ALL ON TABLE "public"."org_members" TO "anon";
GRANT ALL ON TABLE "public"."org_members" TO "authenticated";
GRANT ALL ON TABLE "public"."org_members" TO "service_role";



GRANT ALL ON TABLE "public"."organizations" TO "anon";
GRANT ALL ON TABLE "public"."organizations" TO "authenticated";
GRANT ALL ON TABLE "public"."organizations" TO "service_role";



GRANT ALL ON TABLE "public"."profiles" TO "anon";
GRANT ALL ON TABLE "public"."profiles" TO "authenticated";
GRANT ALL ON TABLE "public"."profiles" TO "service_role";



GRANT ALL ON TABLE "public"."project_members" TO "anon";
GRANT ALL ON TABLE "public"."project_members" TO "authenticated";
GRANT ALL ON TABLE "public"."project_members" TO "service_role";



GRANT ALL ON TABLE "public"."projects" TO "anon";
GRANT ALL ON TABLE "public"."projects" TO "authenticated";
GRANT ALL ON TABLE "public"."projects" TO "service_role";



GRANT ALL ON TABLE "public"."sandbox_endpoints" TO "anon";
GRANT ALL ON TABLE "public"."sandbox_endpoints" TO "authenticated";
GRANT ALL ON TABLE "public"."sandbox_endpoints" TO "service_role";



GRANT ALL ON TABLE "public"."subscriptions" TO "anon";
GRANT ALL ON TABLE "public"."subscriptions" TO "authenticated";
GRANT ALL ON TABLE "public"."subscriptions" TO "service_role";



GRANT ALL ON TABLE "public"."sync_changelog" TO "anon";
GRANT ALL ON TABLE "public"."sync_changelog" TO "authenticated";
GRANT ALL ON TABLE "public"."sync_changelog" TO "service_role";



GRANT ALL ON SEQUENCE "public"."sync_changelog_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."sync_changelog_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."sync_changelog_id_seq" TO "service_role";



GRANT ALL ON TABLE "public"."sync_runs" TO "anon";
GRANT ALL ON TABLE "public"."sync_runs" TO "authenticated";
GRANT ALL ON TABLE "public"."sync_runs" TO "service_role";



GRANT ALL ON TABLE "public"."syncs" TO "anon";
GRANT ALL ON TABLE "public"."syncs" TO "authenticated";
GRANT ALL ON TABLE "public"."syncs" TO "service_role";



GRANT ALL ON TABLE "public"."threads" TO "anon";
GRANT ALL ON TABLE "public"."threads" TO "authenticated";
GRANT ALL ON TABLE "public"."threads" TO "service_role";



GRANT ALL ON TABLE "public"."tools" TO "anon";
GRANT ALL ON TABLE "public"."tools" TO "authenticated";
GRANT ALL ON TABLE "public"."tools" TO "service_role";



GRANT ALL ON TABLE "public"."uploads" TO "anon";
GRANT ALL ON TABLE "public"."uploads" TO "authenticated";
GRANT ALL ON TABLE "public"."uploads" TO "service_role";









ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON SEQUENCES TO "postgres";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON SEQUENCES TO "anon";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON SEQUENCES TO "authenticated";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON SEQUENCES TO "service_role";






ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON FUNCTIONS TO "postgres";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON FUNCTIONS TO "anon";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON FUNCTIONS TO "authenticated";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON FUNCTIONS TO "service_role";






ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON TABLES TO "postgres";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON TABLES TO "anon";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON TABLES TO "authenticated";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON TABLES TO "service_role";































CREATE TRIGGER on_auth_user_created AFTER INSERT ON auth.users FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();


