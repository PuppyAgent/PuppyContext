-- GENERATED BASELINE: fresh Supabase databases only.
-- Covers 102 migrations through 20260923000000.
-- Do not add alongside the covered migration files.

CREATE EXTENSION IF NOT EXISTS pg_graphql WITH SCHEMA graphql;
CREATE EXTENSION IF NOT EXISTS pg_net WITH SCHEMA extensions;
CREATE EXTENSION IF NOT EXISTS pg_stat_statements WITH SCHEMA extensions;
CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;
CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA extensions;
CREATE EXTENSION IF NOT EXISTS supabase_vault WITH SCHEMA vault;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA extensions;

-- Avoid reintroducing platform defaults on restored application objects.
-- The dump restores each object's ACL and the final creation defaults.
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" REVOKE ALL ON TABLES FROM "anon", "authenticated", "service_role";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" REVOKE ALL ON SEQUENCES FROM "anon", "authenticated", "service_role";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" REVOKE ALL ON FUNCTIONS FROM "anon", "authenticated", "service_role";

--
-- PostgreSQL database dump
--



SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: public; Type: SCHEMA; Schema: -; Owner: pg_database_owner
--

CREATE SCHEMA IF NOT EXISTS "public";


ALTER SCHEMA "public" OWNER TO "pg_database_owner";

--
-- Name: SCHEMA "public"; Type: COMMENT; Schema: -; Owner: pg_database_owner
--

COMMENT ON SCHEMA "public" IS 'standard public schema';


--
-- Name: _assert_project_creator_admin_at_commit(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."_assert_project_creator_admin_at_commit"() RETURNS "trigger"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
DECLARE
    project_ids text[];
BEGIN
    IF TG_OP = 'INSERT' THEN
        project_ids := ARRAY[NEW.id];
    ELSE
        project_ids := ARRAY[OLD.id, NEW.id];
    END IF;

    IF EXISTS (
        SELECT 1
        FROM public.projects p
        LEFT JOIN public.org_members om
          ON om.org_id = p.org_id AND om.user_id = p.created_by
        LEFT JOIN public.project_members pm
          ON pm.project_id = p.id
         AND pm.org_id = p.org_id
         AND pm.user_id = p.created_by
        WHERE p.id = ANY(project_ids)
          AND p.created_by IS NOT NULL
          AND (om.id IS NULL OR pm.role IS DISTINCT FROM 'admin')
    ) THEN
        RAISE EXCEPTION 'project creator must be an organization member and explicit Project Admin'
            USING ERRCODE = '23514';
    END IF;

    RETURN NEW;
END;
$$;


ALTER FUNCTION "public"."_assert_project_creator_admin_at_commit"() OWNER TO "postgres";

--
-- Name: _billing_row_bump_updated_at(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."_billing_row_bump_updated_at"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;


ALTER FUNCTION "public"."_billing_row_bump_updated_at"() OWNER TO "postgres";

--
-- Name: _confirm_correlated_billing_operations(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."_confirm_correlated_billing_operations"() RETURNS "trigger"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
BEGIN
    IF NEW.source_quote_id IS NULL THEN
        RETURN NEW;
    END IF;

    UPDATE public.organization_billing_operations AS operation
    SET status = 'confirmed',
        confirmed_revision = NEW.source_revision,
        completed_at = COALESCE(operation.completed_at, now()),
        last_error = NULL
    WHERE operation.org_id = NEW.org_id
      AND operation.quote_id = NEW.source_quote_id
      AND operation.kind IN (
          'checkout', 'plan_change', 'seat_increase', 'seat_decrease',
          'member_activation', 'member_deactivation'
      )
      AND operation.status IN (
          'pending', 'quoted', 'awaiting_confirmation', 'submitted'
      )
      -- Correlation is fail-closed: an incomplete legacy/partial intent may
      -- remain recoverable, but it can never be declared financially complete.
      AND operation.target_plan_id = NEW.plan_id
      AND operation.target_seat_quantity = NEW.seat_quantity
      AND operation.baseline_source_revision IS NOT NULL
      AND operation.baseline_source_revision < NEW.source_revision;

    RETURN NEW;
END;
$$;


ALTER FUNCTION "public"."_confirm_correlated_billing_operations"() OWNER TO "postgres";

--
-- Name: _connectors_bump_updated_at(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."_connectors_bump_updated_at"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;


ALTER FUNCTION "public"."_connectors_bump_updated_at"() OWNER TO "postgres";

--
-- Name: _context_entrypoint_bump_updated_at(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."_context_entrypoint_bump_updated_at"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;


ALTER FUNCTION "public"."_context_entrypoint_bump_updated_at"() OWNER TO "postgres";

--
-- Name: _enforce_project_creator_admin_member(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."_enforce_project_creator_admin_member"() RETURNS "trigger"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF EXISTS (
            SELECT 1
            FROM public.projects p
            WHERE p.id = OLD.project_id AND p.created_by = OLD.user_id
        ) THEN
            RAISE EXCEPTION 'project creator must retain explicit Project Admin membership'
                USING ERRCODE = '23514';
        END IF;
        RETURN OLD;
    END IF;

    IF TG_OP = 'UPDATE' AND EXISTS (
        SELECT 1
        FROM public.projects p
        WHERE p.id = OLD.project_id
          AND p.created_by = OLD.user_id
          AND (
              NEW.project_id IS DISTINCT FROM OLD.project_id
              OR NEW.user_id IS DISTINCT FROM OLD.user_id
              OR NEW.org_id IS DISTINCT FROM OLD.org_id
              OR NEW.role IS DISTINCT FROM 'admin'
          )
    ) THEN
        RAISE EXCEPTION 'project creator must retain explicit Project Admin membership'
            USING ERRCODE = '23514';
    END IF;

    IF TG_OP IN ('INSERT', 'UPDATE')
       AND NEW.role IS DISTINCT FROM 'admin'
       AND EXISTS (
           SELECT 1
           FROM public.projects p
           WHERE p.id = NEW.project_id AND p.created_by = NEW.user_id
       ) THEN
        RAISE EXCEPTION 'project creator must retain explicit Project Admin membership'
            USING ERRCODE = '23514';
    END IF;

    RETURN NEW;
END;
$$;


ALTER FUNCTION "public"."_enforce_project_creator_admin_member"() OWNER TO "postgres";

--
-- Name: _github_integrations_bump_updated_at(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."_github_integrations_bump_updated_at"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;


ALTER FUNCTION "public"."_github_integrations_bump_updated_at"() OWNER TO "postgres";

--
-- Name: _import_jobs_bump_updated_at(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."_import_jobs_bump_updated_at"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;


ALTER FUNCTION "public"."_import_jobs_bump_updated_at"() OWNER TO "postgres";

--
-- Name: _organization_entitlements_bump_updated_at(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."_organization_entitlements_bump_updated_at"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;


ALTER FUNCTION "public"."_organization_entitlements_bump_updated_at"() OWNER TO "postgres";

--
-- Name: _prepare_project_deletion_job_storage(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."_prepare_project_deletion_job_storage"() RETURNS "trigger"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM public.project_storage_inventory_state inventory
        WHERE inventory.singleton AND inventory.inventory_complete
    ) THEN
        RAISE EXCEPTION 'Project storage inventory is incomplete'
            USING ERRCODE = '55000',
                  HINT = 'Run the resumable Project storage inventory before deletion.';
    END IF;

    UPDATE public.projects
    SET lifecycle_status = 'deleting', updated_at = now()
    WHERE id = NEW.project_id
      AND lifecycle_status IN ('initializing', 'ready');

    -- Capture the authoritative ownership manifest before relational cleanup
    -- can remove any of its source rows.
    NEW.storage_principals := public._project_deletion_storage_principals(
        NEW.project_id, NEW.requested_by
    );
    NEW.object_prefixes := public._project_deletion_object_prefixes(
        NEW.project_id, NEW.storage_principals
    );
    NEW.search_namespace_prefixes := public._project_deletion_search_prefixes(
        NEW.project_id
    );
    NEW.sandbox_resources := public._project_deletion_sandbox_resources(
        NEW.project_id
    );
    RETURN NEW;
END;
$$;


ALTER FUNCTION "public"."_prepare_project_deletion_job_storage"() OWNER TO "postgres";

--
-- Name: _project_deletion_external_ingest_snapshot_valid("text", "jsonb"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."_project_deletion_external_ingest_snapshot_valid"("p_project_id" "text", "p_external_ingest_resources" "jsonb") RETURNS boolean
    LANGUAGE "sql" IMMUTABLE
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
    SELECT
        jsonb_typeof(p_external_ingest_resources) = 'object'
        AND p_external_ingest_resources ->> 'project_id' = p_project_id
        AND jsonb_typeof(p_external_ingest_resources -> 'provider_handles') = 'array'
        AND jsonb_typeof(p_external_ingest_resources -> 'redis_keys') = 'array'
        AND jsonb_typeof(p_external_ingest_resources -> 'cache_task_ids') = 'array'
        AND jsonb_typeof(p_external_ingest_resources -> 'etl_task_ids') = 'array'
        AND jsonb_typeof(p_external_ingest_resources -> 'arq_job_ids') = 'array'
        AND p_external_ingest_resources -> 'errors' = '[]'::jsonb;
$$;


ALTER FUNCTION "public"."_project_deletion_external_ingest_snapshot_valid"("p_project_id" "text", "p_external_ingest_resources" "jsonb") OWNER TO "postgres";

--
-- Name: _project_deletion_object_prefixes("text", "jsonb"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."_project_deletion_object_prefixes"("p_project_id" "text", "p_storage_principals" "jsonb") RETURNS "jsonb"
    LANGUAGE "sql" IMMUTABLE
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
    WITH principals AS (
        SELECT DISTINCT value AS principal
        FROM jsonb_array_elements_text(
            CASE
                WHEN jsonb_typeof(p_storage_principals) = 'array'
                    THEN p_storage_principals
                ELSE '[]'::jsonb
            END
        )
    ), prefixes AS (
        SELECT fixed.ordinal, ''::text AS principal, fixed.prefix
        FROM (VALUES
            (1, 'version/' || p_project_id || '/'),
            (2, 'mut/' || p_project_id || '/'),
            (3, 'projects/' || p_project_id || '/'),
            (4, 'shadow-snapshots/' || p_project_id || '/')
        ) AS fixed(ordinal, prefix)
        UNION ALL
        SELECT 10 + namespace.ordinal, principal.principal,
               'users/' || principal.principal || '/' || namespace.name ||
               '/' || p_project_id || '/'
        FROM principals principal
        CROSS JOIN (VALUES
            (1, 'etl_artifacts'),
            (2, 'processed'),
            (3, 'raw')
        ) AS namespace(ordinal, name)
    )
    SELECT jsonb_agg(prefix ORDER BY ordinal, principal)
    FROM prefixes;
$$;


ALTER FUNCTION "public"."_project_deletion_object_prefixes"("p_project_id" "text", "p_storage_principals" "jsonb") OWNER TO "postgres";

--
-- Name: _project_deletion_principals_valid("text", "uuid", "jsonb"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."_project_deletion_principals_valid"("p_project_id" "text", "p_requested_by" "uuid", "p_storage_principals" "jsonb") RETURNS boolean
    LANGUAGE "plpgsql" IMMUTABLE
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $_$
BEGIN
    IF p_project_id !~ '^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$'
       OR jsonb_typeof(p_storage_principals) IS DISTINCT FROM 'array'
       OR jsonb_array_length(p_storage_principals) = 0
    THEN
        RETURN false;
    END IF;
    RETURN
        jsonb_array_length(p_storage_principals) = (
            SELECT count(DISTINCT value)
            FROM jsonb_array_elements_text(p_storage_principals)
        )
        AND NOT EXISTS (
            SELECT 1
            FROM jsonb_array_elements_text(p_storage_principals) AS principal(value)
            WHERE principal.value !~ '^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$'
        )
        AND p_storage_principals ? p_requested_by::text;
END;
$_$;


ALTER FUNCTION "public"."_project_deletion_principals_valid"("p_project_id" "text", "p_requested_by" "uuid", "p_storage_principals" "jsonb") OWNER TO "postgres";

--
-- Name: _project_deletion_sandbox_resources("text"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."_project_deletion_sandbox_resources"("p_project_id" "text") RETURNS "jsonb"
    LANGUAGE "sql" STABLE
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
    SELECT COALESCE(
        jsonb_agg(resource.payload ORDER BY resource.kind, resource.provider,
                  resource.resource_id),
        '[]'::jsonb
    )
    FROM (
        SELECT DISTINCT
            'scope'::text AS kind,
            session.provider,
            session.sandbox_id AS resource_id,
            jsonb_build_object(
                'kind', 'scope',
                'provider', session.provider,
                'resource_id', session.sandbox_id
            ) AS payload
        FROM public.scope_sandbox_sessions session
        WHERE session.project_id = p_project_id
          AND session.sandbox_id <> ''
        UNION
        SELECT DISTINCT
            'execution'::text AS kind,
            session.provider,
            session.resource_id,
            jsonb_build_object(
                'kind', 'execution',
                'provider', session.provider,
                'resource_id', session.resource_id
            ) AS payload
        FROM public.sandbox_execution_sessions session
        WHERE session.project_id = p_project_id
          AND session.resource_id <> ''
    ) resource;
$$;


ALTER FUNCTION "public"."_project_deletion_sandbox_resources"("p_project_id" "text") OWNER TO "postgres";

--
-- Name: _project_deletion_sandbox_resources_valid("jsonb"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."_project_deletion_sandbox_resources_valid"("p_resources" "jsonb") RETURNS boolean
    LANGUAGE "plpgsql" IMMUTABLE
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $_$
BEGIN
    RETURN jsonb_typeof(p_resources) = 'array'
       AND NOT EXISTS (
           SELECT 1
           FROM jsonb_array_elements(p_resources) item(value)
           WHERE item.value ->> 'kind' NOT IN ('scope', 'execution')
              OR COALESCE(item.value ->> 'provider', '')
                   !~ '^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$'
              OR COALESCE(item.value ->> 'resource_id', '') = ''
              OR length(item.value ->> 'resource_id') > 512
       )
       AND jsonb_array_length(p_resources) = (
           SELECT count(DISTINCT jsonb_build_array(
               item.value ->> 'kind',
               item.value ->> 'provider',
               item.value ->> 'resource_id'
           ))
           FROM jsonb_array_elements(p_resources) item(value)
       );
END;
$_$;


ALTER FUNCTION "public"."_project_deletion_sandbox_resources_valid"("p_resources" "jsonb") OWNER TO "postgres";

--
-- Name: _project_deletion_search_prefixes("text"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."_project_deletion_search_prefixes"("p_project_id" "text") RETURNS "jsonb"
    LANGUAGE "sql" IMMUTABLE
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
    SELECT jsonb_build_array(
        'project_' || p_project_id || '_path_',
        'project_' || p_project_id || '_folder_'
    );
$$;


ALTER FUNCTION "public"."_project_deletion_search_prefixes"("p_project_id" "text") OWNER TO "postgres";

--
-- Name: _project_deletion_storage_principals("text", "uuid"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."_project_deletion_storage_principals"("p_project_id" "text", "p_requested_by" "uuid") RETURNS "jsonb"
    LANGUAGE "sql" STABLE
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $_$
    SELECT COALESCE(jsonb_agg(source.principal ORDER BY source.principal), '[]'::jsonb)
    FROM (
        SELECT DISTINCT candidate.principal
        FROM (
            SELECT p_requested_by::text AS principal
            UNION ALL
            SELECT project.created_by::text
            FROM public.projects project
            WHERE project.id = p_project_id
            UNION ALL
            SELECT stored.principal
            FROM public.project_storage_principals stored
            WHERE stored.project_id = p_project_id
            UNION ALL
            SELECT COALESCE(upload.created_by::text, p_project_id)
            FROM public.uploads upload
            WHERE upload.project_id = p_project_id
        ) candidate
        WHERE candidate.principal IS NOT NULL
          AND candidate.principal ~ '^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$'
    ) source;
$_$;


ALTER FUNCTION "public"."_project_deletion_storage_principals"("p_project_id" "text", "p_requested_by" "uuid") OWNER TO "postgres";

--
-- Name: _project_initialization_has_cascade_dependents("text"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."_project_initialization_has_cascade_dependents"("p_project_id" "text") RETURNS boolean
    LANGUAGE "plpgsql" STABLE SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $_$
DECLARE
    dependency record;
    has_rows boolean;
BEGIN
    FOR dependency IN
        SELECT
            child_namespace.nspname AS schema_name,
            child.relname AS table_name,
            string_agg(
                format(
                    'child.%I IS NOT DISTINCT FROM parent.%I',
                    child_attribute.attname,
                    parent_attribute.attname
                ),
                ' AND ' ORDER BY key_position.position
            ) AS join_predicate
        FROM pg_catalog.pg_constraint constraint_row
        JOIN pg_catalog.pg_class child
          ON child.oid = constraint_row.conrelid
        JOIN pg_catalog.pg_namespace child_namespace
          ON child_namespace.oid = child.relnamespace
        CROSS JOIN LATERAL pg_catalog.generate_subscripts(
            constraint_row.conkey,
            1
        ) AS key_position(position)
        JOIN pg_catalog.pg_attribute child_attribute
          ON child_attribute.attrelid = constraint_row.conrelid
         AND child_attribute.attnum = constraint_row.conkey[key_position.position]
        JOIN pg_catalog.pg_attribute parent_attribute
          ON parent_attribute.attrelid = constraint_row.confrelid
         AND parent_attribute.attnum = constraint_row.confkey[key_position.position]
        WHERE constraint_row.contype = 'f'
          AND constraint_row.confrelid = 'public.projects'::regclass
          AND constraint_row.confdeltype IN ('c', 'n', 'd')
          AND constraint_row.conrelid NOT IN (
              'public.project_members'::regclass,
              'public.access_surfaces'::regclass,
              'public.access_surface_credentials'::regclass,
              'public.version_scope_state'::regclass,
              'public.version_transactions'::regclass
          )
        GROUP BY constraint_row.oid, child_namespace.nspname, child.relname
    LOOP
        EXECUTE format(
            'SELECT EXISTS ('
            'SELECT 1 FROM %I.%I AS child '
            'JOIN public.projects AS parent ON %s '
            'WHERE parent.id = $1'
            ')',
            dependency.schema_name,
            dependency.table_name,
            dependency.join_predicate
        ) INTO has_rows USING p_project_id;

        IF has_rows THEN
            RETURN true;
        END IF;
    END LOOP;

    RETURN false;
END;
$_$;


ALTER FUNCTION "public"."_project_initialization_has_cascade_dependents"("p_project_id" "text") OWNER TO "postgres";

--
-- Name: _project_members_bump_updated_at(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."_project_members_bump_updated_at"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;


ALTER FUNCTION "public"."_project_members_bump_updated_at"() OWNER TO "postgres";

--
-- Name: _remember_upload_storage_principal(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."_remember_upload_storage_principal"() RETURNS "trigger"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
BEGIN
    INSERT INTO public.project_storage_principals (project_id, principal)
    VALUES (NEW.project_id, COALESCE(NEW.created_by::text, NEW.project_id))
    ON CONFLICT (project_id, principal) DO NOTHING;
    RETURN NEW;
END;
$$;


ALTER FUNCTION "public"."_remember_upload_storage_principal"() OWNER TO "postgres";

--
-- Name: _repository_scopes_bump_updated_at(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."_repository_scopes_bump_updated_at"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;


ALTER FUNCTION "public"."_repository_scopes_bump_updated_at"() OWNER TO "postgres";

--
-- Name: _reset_entitlement_quote_on_revision_advance(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."_reset_entitlement_quote_on_revision_advance"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
BEGIN
    IF NEW.source_revision IS DISTINCT FROM OLD.source_revision THEN
        NEW.source_quote_id := NULL;
    END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION "public"."_reset_entitlement_quote_on_revision_advance"() OWNER TO "postgres";

--
-- Name: _scope_sandbox_sessions_bump_updated_at(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."_scope_sandbox_sessions_bump_updated_at"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;


ALTER FUNCTION "public"."_scope_sandbox_sessions_bump_updated_at"() OWNER TO "postgres";

--
-- Name: _untitled_project_slot("text"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."_untitled_project_slot"("p_name" "text") RETURNS bigint
    LANGUAGE "plpgsql" IMMUTABLE
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $_$
DECLARE
    matched text[];
    numeric_slot numeric;
BEGIN
    matched := regexp_match(
        btrim(p_name),
        '^Untitled Project(?: (?:(\d+)|\((\d+)\)))?$',
        'i'
    );
    IF matched IS NULL THEN
        RETURN NULL;
    END IF;
    IF matched[1] IS NULL AND matched[2] IS NULL THEN
        RETURN 1;
    END IF;
    numeric_slot := COALESCE(matched[1], matched[2])::numeric;
    IF numeric_slot <= 1 OR numeric_slot > 9223372036854775807 THEN
        -- Preserve historical semantics: explicit slot 1 and values outside
        -- the allocator range are custom names, not default-name requests.
        RETURN NULL;
    END IF;
    RETURN numeric_slot::bigint;
END;
$_$;


ALTER FUNCTION "public"."_untitled_project_slot"("p_name" "text") OWNER TO "postgres";

--
-- Name: _validate_access_surface_credential(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."_validate_access_surface_credential"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
DECLARE
    selected_kind text;
    selected_target_max_mode text;
    selected_surface_mode text;
    selected_surface_status text;
    selected_user_org text;
    selected_user_role text;
BEGIN
    SELECT
        s.kind,
        CASE WHEN s.scope_id IS NULL THEN 'rw' ELSE rs.max_mode END,
        COALESCE(s.config ->> 'mode', 'rw'),
        s.status
      INTO
        selected_kind,
        selected_target_max_mode,
        selected_surface_mode,
        selected_surface_status
    FROM public.access_surfaces s
    LEFT JOIN public.repository_scopes rs
      ON rs.id = s.scope_id AND rs.project_id = s.project_id
    WHERE s.id = NEW.access_surface_id
      AND s.project_id = NEW.project_id
      AND s.org_id = NEW.org_id
      AND (s.scope_id IS NULL OR rs.id IS NOT NULL);

    IF NOT FOUND THEN
        RAISE EXCEPTION 'credential Surface/Project/Organization mismatch';
    END IF;
    IF selected_surface_mode NOT IN ('r', 'rw') THEN
        RAISE EXCEPTION 'credential Surface has an invalid mode';
    END IF;
    IF NEW.status = 'active' AND selected_surface_status <> 'active' THEN
        RAISE EXCEPTION 'active credential requires an active Surface';
    END IF;
    IF NEW.credential_lifecycle IS NULL THEN
        NEW.credential_lifecycle := CASE
            WHEN NEW.user_id IS NOT NULL THEN 'user'
            WHEN NEW.expires_at IS NOT NULL THEN 'session'
            ELSE 'shared'
        END;
    END IF;
    IF (NEW.user_id IS NOT NULL)
       IS DISTINCT FROM (NEW.credential_lifecycle = 'user') THEN
        RAISE EXCEPTION 'credential lifecycle/user owner mismatch';
    END IF;
    IF NEW.credential_lifecycle = 'session' AND NEW.expires_at IS NULL THEN
        RAISE EXCEPTION 'session credential requires an expiry';
    END IF;
    IF NEW.credential_lifecycle = 'user'
       AND NEW.credential_type <> 'git_http_token' THEN
        RAISE EXCEPTION 'user lifecycle is only valid for Git credentials';
    END IF;
    IF NEW.credential_type IN ('git_http_token', 'ssh_public_key')
       AND selected_kind <> 'git_remote' THEN
        RAISE EXCEPTION 'Git credential requires a git_remote Surface';
    END IF;
    IF NEW.credential_type = 'bearer_token'
       AND selected_kind NOT IN ('cli', 'agent', 'mcp', 'sandbox') THEN
        RAISE EXCEPTION 'bearer credential is invalid for this Surface kind';
    END IF;

    IF NEW.grant_mode IS NULL THEN
        NEW.grant_mode := COALESCE(selected_target_max_mode, 'r');
    END IF;
    IF NEW.grant_mode = 'rw' AND selected_target_max_mode <> 'rw' THEN
        RAISE EXCEPTION 'credential mode cannot exceed target mode';
    END IF;

    IF NEW.status = 'active' AND NEW.credential_lifecycle = 'user' THEN
        SELECT role.org_id, role.effective_role
          INTO selected_user_org, selected_user_role
        FROM public.resolve_project_role(NEW.project_id, NEW.user_id) role;
        IF selected_user_org IS DISTINCT FROM NEW.org_id
           OR selected_user_role IS NULL THEN
            RAISE EXCEPTION 'user Git credential Project authorization denied'
                USING ERRCODE = '42501';
        END IF;
        IF NEW.grant_mode = 'rw' AND selected_user_role = 'viewer' THEN
            RAISE EXCEPTION 'user Git credential write authorization denied'
                USING ERRCODE = '42501';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION "public"."_validate_access_surface_credential"() OWNER TO "postgres";

--
-- Name: _validate_access_tool_project_boundary(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."_validate_access_tool_project_boundary"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
DECLARE
    surface_project_id text;
    surface_org_id text;
    tool_project_id text;
    tool_org_id text;
BEGIN
    SELECT project_id, org_id INTO surface_project_id, surface_org_id
    FROM public.access_surfaces WHERE id = NEW.access_point_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'access tool surface not found';
    END IF;

    SELECT project_id, org_id INTO tool_project_id, tool_org_id
    FROM public.tools WHERE id = NEW.tool_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'access tool not found';
    END IF;

    IF tool_org_id IS DISTINCT FROM surface_org_id
       OR (tool_project_id IS NOT NULL
           AND tool_project_id IS DISTINCT FROM surface_project_id) THEN
        RAISE EXCEPTION 'access tool crosses Project or Organization boundary';
    END IF;
    RETURN NEW;
END;
$$;


ALTER FUNCTION "public"."_validate_access_tool_project_boundary"() OWNER TO "postgres";

--
-- Name: _version_refs_bump_updated_at(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."_version_refs_bump_updated_at"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;


ALTER FUNCTION "public"."_version_refs_bump_updated_at"() OWNER TO "postgres";

--
-- Name: abandon_project_initialization("text", "text", "uuid", integer, "text"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."abandon_project_initialization"("p_project_id" "text", "p_operation_key" "text", "p_actor_user_id" "uuid", "p_quiescence_seconds" integer DEFAULT 3600, "p_worker_id" "text" DEFAULT NULL::"text") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
DECLARE
    create_operation public.project_create_operations%ROWTYPE;
    deletion_job public.project_deletion_jobs%ROWTYPE;
    project_row public.projects%ROWTYPE;
    empty_tree constant text := '4b825dc642cb6eb9a060e54bf8d69288fbee4904';
BEGIN
    PERFORM pg_advisory_xact_lock(
        hashtextextended('project-create:' || p_actor_user_id::text || ':' || p_operation_key, 0)
    );
    SELECT * INTO create_operation
    FROM public.project_create_operations
    WHERE actor_user_id = p_actor_user_id AND operation_key = p_operation_key
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('outcome', 'not_found');
    END IF;
    IF create_operation.project_id IS DISTINCT FROM p_project_id THEN
        RETURN jsonb_build_object('outcome', 'conflict');
    END IF;
    IF create_operation.publication_mode <> 'empty' THEN
        RETURN jsonb_build_object('outcome', 'not_abandonable');
    END IF;

    SELECT * INTO deletion_job
    FROM public.project_deletion_jobs
    WHERE project_id = p_project_id;
    IF create_operation.status = 'deleted' THEN
        IF deletion_job.id IS NULL THEN
            RETURN jsonb_build_object('outcome', 'gone');
        END IF;
        RETURN jsonb_build_object(
            'outcome', 'replayed',
            'job', to_jsonb(deletion_job)
        );
    END IF;
    IF p_worker_id IS NOT NULL
       AND (
           create_operation.status <> 'initializing'
           OR create_operation.initialization_claimed_by IS DISTINCT FROM p_worker_id
       ) THEN
        RETURN jsonb_build_object('outcome', 'claim_lost');
    END IF;

    SELECT * INTO project_row
    FROM public.projects
    WHERE id = p_project_id
    FOR UPDATE;

    IF project_row.id IS NOT NULL THEN
        IF project_row.created_by IS DISTINCT FROM p_actor_user_id
           OR (
               p_worker_id IS NULL
               AND NOT EXISTS (
                   SELECT 1 FROM public.org_members om
                   WHERE om.org_id = project_row.org_id
                     AND om.user_id = p_actor_user_id
               )
           ) THEN
            RETURN jsonb_build_object('outcome', 'forbidden');
        END IF;
        IF (
               to_jsonb(project_row)
                   - ARRAY[
                       'version_root_hash', 'mut_root_hash',
                       'updated_at', 'lifecycle_status'
                   ]::text[]
             ) IS DISTINCT FROM (
               create_operation.project_snapshot
                   - ARRAY[
                       'version_root_hash', 'mut_root_hash',
                       'updated_at', 'lifecycle_status'
                   ]::text[]
             )
           OR NOT (
               (
                   COALESCE(project_row.version_root_hash, '') = ''
                   AND COALESCE(project_row.mut_root_hash, '') = ''
               )
               OR (
                   project_row.version_root_hash = empty_tree
                   AND project_row.mut_root_hash = empty_tree
               )
           )
           OR EXISTS (
               SELECT 1 FROM public.version_scope_state state
               WHERE state.project_id = p_project_id
                 AND (
                     state.scope_path <> ''
                     OR COALESCE(state.scope_hash, '') <> ''
                     OR COALESCE(state.head_commit_id, '') <> ''
                 )
           )
           OR EXISTS (
               SELECT 1 FROM public.version_transactions tx
               WHERE tx.project_id = p_project_id
                 AND tx.status = 'committed'
           )
           OR (SELECT count(*) FROM public.project_members member
               WHERE member.project_id = p_project_id) <> 1
           OR NOT EXISTS (
               SELECT 1 FROM public.project_members member
               WHERE member.project_id = p_project_id
                 AND member.org_id = project_row.org_id
                 AND member.user_id = p_actor_user_id
                 AND member.role = 'admin'
                 AND member.granted_by = p_actor_user_id
           )
           -- The only credential operation allowed during bootstrap is the
           -- user Git credential issued with the same publish operation key.
           OR EXISTS (
               SELECT 1
               FROM public.git_credential_issue_operations op
               WHERE op.project_id = p_project_id
                 AND (
                     op.actor_user_id IS DISTINCT FROM p_actor_user_id
                     OR op.operation_key IS DISTINCT FROM p_operation_key
                     OR op.org_id IS DISTINCT FROM project_row.org_id
                     OR op.status IS DISTINCT FROM 'active'
                     OR NOT EXISTS (
                         SELECT 1
                         FROM public.access_surface_credentials credential
                         JOIN public.access_surfaces surface
                           ON surface.id = credential.access_surface_id
                         WHERE credential.id = op.credential_id
                           AND credential.project_id = p_project_id
                           AND credential.org_id = project_row.org_id
                           AND credential.user_id = p_actor_user_id
                           AND credential.created_by = p_actor_user_id
                           AND credential.credential_type = 'git_http_token'
                           AND credential.credential_lifecycle = 'user'
                           AND credential.status = 'active'
                           AND credential.key_hash = op.credential_hash
                           AND surface.project_id = p_project_id
                           AND surface.org_id = project_row.org_id
                           AND surface.scope_id IS NULL
                           AND surface.kind = 'git_remote'
                     )
                 )
           )
           OR EXISTS (
               SELECT 1
               FROM public.access_surface_credentials credential
               WHERE credential.project_id = p_project_id
                 AND NOT EXISTS (
                     SELECT 1
                     FROM public.git_credential_issue_operations op
                     WHERE op.actor_user_id = p_actor_user_id
                       AND op.operation_key = p_operation_key
                       AND op.project_id = p_project_id
                       AND op.org_id = project_row.org_id
                       AND op.credential_id = credential.id
                       AND op.credential_hash = credential.key_hash
                       AND op.status = 'active'
                 )
           )
           -- Bootstrap may materialize only the standard root Git/CLI
           -- Surfaces.  Any Scope, Agent, policy, tool, or modified Surface
           -- means this Project has become a real user resource.
           OR (SELECT count(*) FROM public.access_surfaces surface
               WHERE surface.project_id = p_project_id) NOT IN (0, 2)
           OR EXISTS (
               SELECT 1 FROM public.access_surfaces surface
               WHERE surface.project_id = p_project_id
                 AND (
                     surface.org_id IS DISTINCT FROM project_row.org_id
                     OR surface.scope_id IS NOT NULL
                     OR surface.kind NOT IN ('git_remote', 'cli')
                     OR surface.name IS DISTINCT FROM CASE surface.kind
                         WHEN 'git_remote' THEN 'Git Remote'
                         WHEN 'cli' THEN 'FS CLI'
                     END
                     OR surface.status IS DISTINCT FROM 'active'
                     OR surface.principal_type IS DISTINCT FROM 'project'
                     OR surface.principal_id IS DISTINCT FROM p_project_id
                     OR surface.config IS DISTINCT FROM jsonb_build_object(
                         'mode', 'rw', 'direction', 'bidirectional'
                     )
                     OR surface.created_by IS DISTINCT FROM p_actor_user_id
                 )
           )
           OR EXISTS (
               SELECT 1
               FROM public.access_surface_policies policy
               JOIN public.access_surfaces surface
                 ON surface.id = policy.access_surface_id
               WHERE surface.project_id = p_project_id
           )
           OR EXISTS (
               SELECT 1
               FROM public.access_tools tool_binding
               JOIN public.access_surfaces surface
                 ON surface.id = tool_binding.access_point_id
               WHERE surface.project_id = p_project_id
           )
           OR public._project_initialization_has_cascade_dependents(p_project_id)
        THEN
            RETURN jsonb_build_object('outcome', 'not_abandonable');
        END IF;
    END IF;

    UPDATE public.access_surface_credentials c
    SET status = 'revoked', revoked_at = COALESCE(c.revoked_at, now())
    FROM public.git_credential_issue_operations op
    WHERE op.actor_user_id = p_actor_user_id
      AND op.operation_key = p_operation_key
      AND op.project_id = p_project_id
      AND c.id = op.credential_id
      AND c.status = 'active';
    UPDATE public.git_credential_issue_operations
    SET status = 'revoked', revoked_at = COALESCE(revoked_at, now())
    WHERE actor_user_id = p_actor_user_id
      AND operation_key = p_operation_key
      AND project_id = p_project_id
      AND status = 'active';

    INSERT INTO public.project_deletion_jobs (
        project_id, org_id, requested_by, source, source_operation_key,
        object_prefixes, quiescence_seconds, available_at
    ) VALUES (
        p_project_id, create_operation.org_id, p_actor_user_id,
        'initialization_abandon', p_operation_key,
        jsonb_build_array(
            'version/' || p_project_id || '/',
            'mut/' || p_project_id || '/',
            'projects/' || p_project_id || '/'
        ),
        GREATEST(COALESCE(p_quiescence_seconds, 3600), 1800),
        now()
    )
    ON CONFLICT (project_id) DO UPDATE
      SET updated_at = now()
    RETURNING * INTO deletion_job;

    UPDATE public.project_create_operations
    SET status = 'deleted', deleted_at = COALESCE(deleted_at, now())
    WHERE actor_user_id = p_actor_user_id AND operation_key = p_operation_key;
    UPDATE public.projects
    SET lifecycle_status = 'deleting', updated_at = now()
    WHERE id = p_project_id AND lifecycle_status = 'initializing';

    RETURN jsonb_build_object(
        'outcome', 'accepted',
        'job', to_jsonb(deletion_job)
    );
END;
$$;


ALTER FUNCTION "public"."abandon_project_initialization"("p_project_id" "text", "p_operation_key" "text", "p_actor_user_id" "uuid", "p_quiescence_seconds" integer, "p_worker_id" "text") OWNER TO "postgres";

--
-- Name: abort_deferred_project_publication("text", "text", "uuid", integer, "text"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."abort_deferred_project_publication"("p_project_id" "text", "p_operation_key" "text", "p_actor_user_id" "uuid", "p_quiescence_seconds" integer DEFAULT 3600, "p_worker_id" "text" DEFAULT NULL::"text") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
DECLARE
    operation public.project_create_operations%ROWTYPE;
    project_row public.projects%ROWTYPE;
    deletion_job public.project_deletion_jobs%ROWTYPE;
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

    IF operation.publication_mode <> 'deferred' THEN
        RETURN jsonb_build_object('outcome', 'not_abortable');
    END IF;

    SELECT * INTO deletion_job
    FROM public.project_deletion_jobs
    WHERE project_id = p_project_id;
    IF operation.status = 'deleted' THEN
        IF deletion_job.id IS NULL THEN
            RETURN jsonb_build_object('outcome', 'gone');
        END IF;
        RETURN jsonb_build_object('outcome', 'replayed', 'job', to_jsonb(deletion_job));
    END IF;
    IF operation.status <> 'initializing'
       OR (
           p_worker_id IS NOT NULL
           AND operation.initialization_claimed_by IS DISTINCT FROM p_worker_id
       ) THEN
        RETURN jsonb_build_object('outcome', 'not_abortable');
    END IF;

    SELECT * INTO project_row
    FROM public.projects
    WHERE id = p_project_id
    FOR UPDATE;
    IF FOUND AND (
        project_row.lifecycle_status <> 'initializing'
        OR project_row.created_by IS DISTINCT FROM p_actor_user_id
        OR project_row.org_id IS DISTINCT FROM operation.org_id
    ) THEN
        RETURN jsonb_build_object('outcome', 'not_abortable');
    END IF;

    -- Even if another control-plane action has already removed the database
    -- aggregate, object writes may have completed before that crash. Persist
    -- the exact-prefix cleanup tombstone before returning so an absent Project
    -- can never turn into leaked immutable storage.

    INSERT INTO public.project_deletion_jobs (
        project_id, org_id, requested_by, source, source_operation_key,
        object_prefixes, quiescence_seconds, available_at
    ) VALUES (
        p_project_id, operation.org_id, p_actor_user_id,
        'publication_abort', p_operation_key,
        jsonb_build_array(
            'version/' || p_project_id || '/',
            'mut/' || p_project_id || '/',
            'projects/' || p_project_id || '/'
        ),
        GREATEST(COALESCE(p_quiescence_seconds, 3600), 1800),
        now()
    )
    ON CONFLICT (project_id) DO UPDATE
      SET updated_at = now()
    RETURNING * INTO deletion_job;

    UPDATE public.git_credential_issue_operations
    SET status = 'deleted', revoked_at = COALESCE(revoked_at, now())
    WHERE project_id = p_project_id AND status = 'active';
    UPDATE public.project_create_operations
    SET status = 'deleted', deleted_at = COALESCE(deleted_at, now())
    WHERE actor_user_id = p_actor_user_id
      AND operation_key = p_operation_key;
    UPDATE public.projects
    SET lifecycle_status = 'deleting', updated_at = now()
    WHERE id = p_project_id AND lifecycle_status = 'initializing';

    RETURN jsonb_build_object(
        'outcome', 'accepted',
        'job', to_jsonb(deletion_job)
    );
END;
$$;


ALTER FUNCTION "public"."abort_deferred_project_publication"("p_project_id" "text", "p_operation_key" "text", "p_actor_user_id" "uuid", "p_quiescence_seconds" integer, "p_worker_id" "text") OWNER TO "postgres";

--
-- Name: acquire_project_write_lease("text", "uuid", "text", "text", integer, "text", "uuid", "text"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."acquire_project_write_lease"("p_project_id" "text", "p_lease_id" "uuid", "p_holder_id" "text", "p_operation" "text", "p_ttl_seconds" integer DEFAULT 120, "p_initialization_operation_key" "text" DEFAULT NULL::"text", "p_initialization_actor" "uuid" DEFAULT NULL::"uuid", "p_initialization_worker" "text" DEFAULT NULL::"text") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $_$
DECLARE
    project_status text;
    lease public.project_write_leases%ROWTYPE;
BEGIN
    IF p_lease_id IS NULL
       OR p_holder_id !~ '^[A-Za-z0-9][A-Za-z0-9:._-]{0,255}$'
       OR p_operation !~ '^[A-Za-z0-9][A-Za-z0-9:._/-]{0,255}$' THEN
        RETURN jsonb_build_object('outcome', 'invalid');
    END IF;

    SELECT project.lifecycle_status INTO project_status
    FROM public.projects project
    WHERE project.id = p_project_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('outcome', 'unavailable');
    END IF;
    IF project_status = 'initializing' THEN
        IF p_initialization_operation_key IS NULL
           OR p_initialization_actor IS NULL
           OR NOT EXISTS (
               SELECT 1
               FROM public.project_create_operations operation
               WHERE operation.project_id = p_project_id
                 AND operation.operation_key = p_initialization_operation_key
                 AND operation.actor_user_id = p_initialization_actor
                 AND operation.status = 'initializing'
                 AND (
                     p_initialization_worker IS NULL
                     OR operation.initialization_claimed_by = p_initialization_worker
                 )
           ) THEN
            RETURN jsonb_build_object('outcome', 'unavailable');
        END IF;
    ELSIF project_status <> 'ready' THEN
        RETURN jsonb_build_object('outcome', 'unavailable');
    END IF;

    DELETE FROM public.project_write_leases expired
    WHERE expired.project_id = p_project_id
      AND expired.expires_at <= now();

    SELECT * INTO lease
    FROM public.project_write_leases existing
    WHERE existing.id = p_lease_id
    FOR UPDATE;
    IF FOUND THEN
        IF lease.project_id IS DISTINCT FROM p_project_id
           OR lease.holder_id IS DISTINCT FROM p_holder_id
           OR lease.operation IS DISTINCT FROM p_operation THEN
            RETURN jsonb_build_object('outcome', 'conflict');
        END IF;
        UPDATE public.project_write_leases
        SET renewed_at = now(),
            expires_at = now() + make_interval(
                secs => GREATEST(30, LEAST(COALESCE(p_ttl_seconds, 120), 7200))
            )
        WHERE id = p_lease_id
        RETURNING * INTO lease;
        RETURN jsonb_build_object('outcome', 'replayed', 'lease', to_jsonb(lease));
    END IF;

    INSERT INTO public.project_write_leases (
        id, project_id, holder_id, operation, expires_at
    ) VALUES (
        p_lease_id, p_project_id, p_holder_id, p_operation,
        now() + make_interval(
            secs => GREATEST(30, LEAST(COALESCE(p_ttl_seconds, 120), 7200))
        )
    ) RETURNING * INTO lease;
    RETURN jsonb_build_object('outcome', 'acquired', 'lease', to_jsonb(lease));
END;
$_$;


ALTER FUNCTION "public"."acquire_project_write_lease"("p_project_id" "text", "p_lease_id" "uuid", "p_holder_id" "text", "p_operation" "text", "p_ttl_seconds" integer, "p_initialization_operation_key" "text", "p_initialization_actor" "uuid", "p_initialization_worker" "text") OWNER TO "postgres";

SET default_tablespace = '';

SET default_table_access_method = "heap";

--
-- Name: project_members; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."project_members" (
    "id" "text" DEFAULT ("extensions"."uuid_generate_v4"())::"text" NOT NULL,
    "project_id" "text" NOT NULL,
    "user_id" "uuid" NOT NULL,
    "role" "text" DEFAULT 'editor'::"text" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "org_id" "text" NOT NULL,
    "granted_by" "uuid",
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "project_members_role_check" CHECK (("role" = ANY (ARRAY['admin'::"text", 'editor'::"text", 'viewer'::"text"])))
);


ALTER TABLE "public"."project_members" OWNER TO "postgres";

--
-- Name: add_project_member_authorized("text", "uuid", "text", "uuid"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."add_project_member_authorized"("p_project_id" "text", "p_target_user_id" "uuid", "p_role" "text", "p_actor_user_id" "uuid") RETURNS SETOF "public"."project_members"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
DECLARE
    project_org_id text;
    actor_role text;
    created_member public.project_members%ROWTYPE;
BEGIN
    IF p_role NOT IN ('admin', 'editor', 'viewer') THEN
        RAISE EXCEPTION 'invalid project role' USING ERRCODE = '22023';
    END IF;

    SELECT r.org_id, r.effective_role
      INTO project_org_id, actor_role
    FROM public.resolve_project_role(p_project_id, p_actor_user_id) r;
    IF actor_role IS DISTINCT FROM 'admin' THEN
        RAISE EXCEPTION 'project member management denied'
            USING ERRCODE = '42501';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM public.org_members
        WHERE org_id = project_org_id AND user_id = p_target_user_id
    ) THEN
        RAISE EXCEPTION 'project member must belong to the organization'
            USING ERRCODE = '42501';
    END IF;

    INSERT INTO public.project_members (
        id, org_id, project_id, user_id, role, granted_by
    ) VALUES (
        gen_random_uuid()::text, project_org_id, p_project_id,
        p_target_user_id, p_role, p_actor_user_id
    ) RETURNING * INTO created_member;

    INSERT INTO public.audit_logs (
        action, path, project_id, operator_type, operator_id, status, metadata
    ) VALUES (
        'project_member.add', '', p_project_id, 'user',
        p_actor_user_id::text, 'success',
        jsonb_build_object('target_user_id', p_target_user_id, 'role', p_role)
    );
    RETURN NEXT created_member;
END;
$$;


ALTER FUNCTION "public"."add_project_member_authorized"("p_project_id" "text", "p_target_user_id" "uuid", "p_role" "text", "p_actor_user_id" "uuid") OWNER TO "postgres";

--
-- Name: adjust_organization_usage_counter("text", "text", bigint, "text", "text", "jsonb"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."adjust_organization_usage_counter"("p_org_id" "text", "p_metric" "text", "p_delta" bigint, "p_idempotency_key" "text", "p_source" "text", "p_metadata" "jsonb" DEFAULT '{}'::"jsonb") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
DECLARE
    existing_event public.organization_usage_events%ROWTYPE;
    counter_row public.organization_usage_counters%ROWTYPE;
    current_value bigint := 0;
    new_value bigint;
BEGIN
    IF p_metric <> 'storage.logical_bytes' THEN
        RAISE EXCEPTION 'unsupported usage metric %', p_metric
            USING ERRCODE = '22023';
    END IF;
    IF p_idempotency_key IS NULL OR length(p_idempotency_key) < 8 THEN
        RAISE EXCEPTION 'usage idempotency key is required'
            USING ERRCODE = '22023';
    END IF;

    PERFORM pg_advisory_xact_lock(hashtextextended(p_org_id || ':' || p_metric, 0));

    SELECT * INTO existing_event
    FROM public.organization_usage_events
    WHERE org_id = p_org_id
      AND metric = p_metric
      AND idempotency_key = p_idempotency_key;
    IF FOUND THEN
        IF existing_event.source IS DISTINCT FROM p_source
           OR existing_event.metadata -> 'requested_delta'
                IS DISTINCT FROM to_jsonb(p_delta) THEN
            RAISE EXCEPTION 'usage_idempotency_payload_mismatch'
                USING ERRCODE = '23505';
        END IF;
        RETURN jsonb_build_object(
            'outcome', 'idempotent',
            'value', existing_event.value_after
        );
    END IF;

    SELECT value INTO current_value
    FROM public.organization_usage_counters
    WHERE org_id = p_org_id AND metric = p_metric
    FOR UPDATE;
    current_value := COALESCE(current_value, 0);
    -- Use numeric for the intermediate sum so malformed or extreme deltas
    -- fail safely on the final bigint cast instead of wrapping arithmetic.
    new_value := GREATEST(
        0::numeric,
        current_value::numeric + p_delta::numeric
    )::bigint;

    INSERT INTO public.organization_usage_counters (org_id, metric, value, version)
    VALUES (p_org_id, p_metric, new_value, 1)
    ON CONFLICT (org_id, metric) DO UPDATE SET
        value = EXCLUDED.value,
        version = public.organization_usage_counters.version + 1
    RETURNING * INTO counter_row;

    INSERT INTO public.organization_usage_events (
        org_id, metric, idempotency_key, delta, value_after, source, metadata
    )
    VALUES (
        p_org_id, p_metric, p_idempotency_key,
        counter_row.value - current_value, counter_row.value,
        p_source, COALESCE(p_metadata, '{}'::jsonb) || jsonb_build_object(
            'requested_delta', p_delta
        )
    );

    RETURN jsonb_build_object(
        'outcome', 'applied',
        'value', counter_row.value,
        'version', counter_row.version
    );
END;
$$;


ALTER FUNCTION "public"."adjust_organization_usage_counter"("p_org_id" "text", "p_metric" "text", "p_delta" bigint, "p_idempotency_key" "text", "p_source" "text", "p_metadata" "jsonb") OWNER TO "postgres";

--
-- Name: analytics_access_summary("text", timestamp with time zone); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."analytics_access_summary"("p_project_id" "text", "p_start_time" timestamp with time zone) RETURNS TABLE("total_accesses" bigint, "unique_agents" bigint, "unique_nodes" bigint)
    LANGUAGE "sql" STABLE
    SET "search_path" TO 'public', 'pg_temp'
    AS $$
    SELECT COUNT(*)::BIGINT,
           COUNT(DISTINCT al.agent_id)::BIGINT,
           COUNT(DISTINCT al.node_name)::BIGINT
    FROM public.access_logs al
    WHERE al.project_id::TEXT = p_project_id
      AND al.created_at >= p_start_time;
$$;


ALTER FUNCTION "public"."analytics_access_summary"("p_project_id" "text", "p_start_time" timestamp with time zone) OWNER TO "postgres";

--
-- Name: analytics_access_timeseries("text", timestamp with time zone, "text", "text", "text"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."analytics_access_timeseries"("p_project_id" "text", "p_start_time" timestamp with time zone, "p_interval" "text", "p_agent_id" "text" DEFAULT NULL::"text", "p_node_name" "text" DEFAULT NULL::"text") RETURNS TABLE("bucket" timestamp with time zone, "event_count" bigint)
    LANGUAGE "sql" STABLE
    SET "search_path" TO 'public', 'pg_temp'
    AS $$
    SELECT
        CASE WHEN p_interval = 'day'
             THEN date_trunc('day', al.created_at)
             ELSE date_trunc('hour', al.created_at) END,
        COUNT(*)::BIGINT
    FROM public.access_logs al
    WHERE al.project_id::TEXT = p_project_id
      AND al.created_at >= p_start_time
      AND (p_agent_id IS NULL OR al.agent_id::TEXT = p_agent_id)
      AND (p_node_name IS NULL OR al.node_name = p_node_name)
    GROUP BY 1
    ORDER BY 1 ASC;
$$;


ALTER FUNCTION "public"."analytics_access_timeseries"("p_project_id" "text", "p_start_time" timestamp with time zone, "p_interval" "text", "p_agent_id" "text", "p_node_name" "text") OWNER TO "postgres";

--
-- Name: cas_update_root_hash("text", "text", "text"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."cas_update_root_hash"("p_project_id" "text", "p_old_hash" "text", "p_new_hash" "text") RETURNS boolean
    LANGUAGE "plpgsql"
    AS $$
DECLARE
    rows_affected INT;
BEGIN
    UPDATE projects
    SET mut_root_hash = p_new_hash
    WHERE id = p_project_id
      AND (mut_root_hash = p_old_hash
           OR (mut_root_hash IS NULL AND (p_old_hash = '' OR p_old_hash IS NULL)));

    GET DIAGNOSTICS rows_affected = ROW_COUNT;
    RETURN rows_affected > 0;
END;
$$;


ALTER FUNCTION "public"."cas_update_root_hash"("p_project_id" "text", "p_old_hash" "text", "p_new_hash" "text") OWNER TO "postgres";

--
-- Name: cas_update_scope_state("text", "text", "text", "text", "text"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."cas_update_scope_state"("p_project_id" "text", "p_scope_path" "text", "p_old_hash" "text", "p_new_hash" "text", "p_head_commit_id" "text" DEFAULT ''::"text") RETURNS boolean
    LANGUAGE "plpgsql"
    AS $$
DECLARE
    rows_affected INT;
BEGIN
    -- First-push fast path: insert a new scope-state row if none exists.
    IF p_old_hash = '' OR p_old_hash IS NULL THEN
        BEGIN
            INSERT INTO mut_scope_state
                (project_id, scope_path, scope_hash, head_commit_id)
            VALUES
                (p_project_id, p_scope_path, p_new_hash, p_head_commit_id)
            ON CONFLICT (project_id, scope_path) DO NOTHING;

            GET DIAGNOSTICS rows_affected = ROW_COUNT;
            IF rows_affected > 0 THEN
                RETURN TRUE;
            END IF;
        EXCEPTION WHEN OTHERS THEN
            NULL;  -- fall through to UPDATE branch
        END;
    END IF;

    -- CAS update: succeed only if current scope_hash matches p_old_hash.
    -- head_commit_id is updated in the same statement for atomicity.
    --
    -- Defensive: when the caller forgets to pass a head_commit_id
    -- (empty string / NULL) we keep whatever is already there instead
    -- of blanking it out. Push/rollback always derive a fresh
    -- head_commit_id before CAS, so in practice this branch is only
    -- a safety net for legacy callers and for the set_scope_hash
    -- fast path that only wants to bump the content fingerprint.
    UPDATE mut_scope_state
    SET scope_hash     = p_new_hash,
        head_commit_id = CASE
            WHEN p_head_commit_id IS NULL OR p_head_commit_id = ''
                THEN head_commit_id
            ELSE p_head_commit_id
        END,
        updated_at     = NOW()
    WHERE project_id = p_project_id
      AND scope_path = p_scope_path
      AND (scope_hash = p_old_hash OR (scope_hash IS NULL AND p_old_hash = ''));

    GET DIAGNOSTICS rows_affected = ROW_COUNT;
    RETURN rows_affected > 0;
END;
$$;


ALTER FUNCTION "public"."cas_update_scope_state"("p_project_id" "text", "p_scope_path" "text", "p_old_hash" "text", "p_new_hash" "text", "p_head_commit_id" "text") OWNER TO "postgres";

--
-- Name: organization_billing_operations; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."organization_billing_operations" (
    "id" "text" DEFAULT ("extensions"."uuid_generate_v4"())::"text" NOT NULL,
    "org_id" "text" NOT NULL,
    "kind" "text" NOT NULL,
    "status" "text" DEFAULT 'pending'::"text" NOT NULL,
    "idempotency_key" "text" NOT NULL,
    "actor_user_id" "uuid",
    "subject_user_id" "uuid",
    "invitation_id" "text",
    "target_plan_id" "text",
    "current_seat_quantity" integer,
    "target_seat_quantity" integer,
    "quote_id" "text",
    "confirmed_revision" bigint,
    "request_payload" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "response_payload" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "attempts" integer DEFAULT 0 NOT NULL,
    "next_attempt_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "last_error" "text",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "completed_at" timestamp with time zone,
    "baseline_source_revision" bigint,
    CONSTRAINT "organization_billing_operations_baseline_revision_check" CHECK ((("baseline_source_revision" IS NULL) OR ("baseline_source_revision" >= 0))),
    CONSTRAINT "organization_billing_operations_kind_check" CHECK (("kind" = ANY (ARRAY['checkout'::"text", 'seat_increase'::"text", 'seat_decrease'::"text", 'plan_change'::"text", 'member_activation'::"text", 'member_deactivation'::"text", 'entitlement_provision'::"text"]))),
    CONSTRAINT "organization_billing_operations_quote_shape" CHECK ((("quote_id" IS NULL) OR ((NULLIF("btrim"("quote_id"), ''::"text") IS NOT NULL) AND ("length"("quote_id") <= 255)))),
    CONSTRAINT "organization_billing_operations_request_object" CHECK (("jsonb_typeof"("request_payload") = 'object'::"text")),
    CONSTRAINT "organization_billing_operations_response_object" CHECK (("jsonb_typeof"("response_payload") = 'object'::"text")),
    CONSTRAINT "organization_billing_operations_seats_check" CHECK (((("current_seat_quantity" IS NULL) OR ("current_seat_quantity" >= 0)) AND (("target_seat_quantity" IS NULL) OR ("target_seat_quantity" >= 0)))),
    CONSTRAINT "organization_billing_operations_status_check" CHECK (("status" = ANY (ARRAY['pending'::"text", 'quoted'::"text", 'awaiting_confirmation'::"text", 'submitted'::"text", 'confirmed'::"text", 'failed'::"text", 'canceled'::"text"])))
);


ALTER TABLE "public"."organization_billing_operations" OWNER TO "postgres";

--
-- Name: claim_entitlement_provisioning_batch(integer, integer); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."claim_entitlement_provisioning_batch"("p_limit" integer DEFAULT 25, "p_lease_seconds" integer DEFAULT 60) RETURNS SETOF "public"."organization_billing_operations"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
BEGIN
    IF p_limit < 1 OR p_limit > 100 THEN
        RAISE EXCEPTION 'p_limit must be between 1 and 100';
    END IF;
    IF p_lease_seconds < 10 OR p_lease_seconds > 3600 THEN
        RAISE EXCEPTION 'p_lease_seconds must be between 10 and 3600';
    END IF;

    RETURN QUERY
    UPDATE public.organization_billing_operations AS operation
    SET
        status = 'submitted',
        attempts = operation.attempts + 1,
        next_attempt_at = now() + make_interval(secs => p_lease_seconds),
        last_error = NULL
    WHERE operation.id IN (
        SELECT candidate.id
        FROM public.organization_billing_operations AS candidate
        WHERE candidate.kind = 'entitlement_provision'
          AND candidate.status IN ('pending', 'failed', 'submitted')
          AND candidate.next_attempt_at <= now()
        ORDER BY candidate.next_attempt_at, candidate.created_at
        FOR UPDATE SKIP LOCKED
        LIMIT p_limit
    )
    RETURNING operation.*;
END;
$$;


ALTER FUNCTION "public"."claim_entitlement_provisioning_batch"("p_limit" integer, "p_lease_seconds" integer) OWNER TO "postgres";

--
-- Name: claim_mut_version_outbox_batch(integer); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."claim_mut_version_outbox_batch"("p_limit" integer DEFAULT 50) RETURNS TABLE("id" bigint, "project_id" "text", "commit_id" "text", "event_type" "text", "payload" "jsonb", "attempts" integer)
    LANGUAGE "sql"
    AS $$
 SELECT * FROM public.claim_version_outbox_batch(p_limit); $$;


ALTER FUNCTION "public"."claim_mut_version_outbox_batch"("p_limit" integer) OWNER TO "postgres";

--
-- Name: project_deletion_jobs; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."project_deletion_jobs" (
    "id" "text" DEFAULT ("gen_random_uuid"())::"text" NOT NULL,
    "project_id" "text" NOT NULL,
    "org_id" "text" NOT NULL,
    "requested_by" "uuid" NOT NULL,
    "source" "text" NOT NULL,
    "source_operation_key" "text",
    "object_prefixes" "jsonb" NOT NULL,
    "phase" "text" DEFAULT 'drain'::"text" NOT NULL,
    "status" "text" DEFAULT 'pending'::"text" NOT NULL,
    "attempts" integer DEFAULT 0 NOT NULL,
    "quiescence_seconds" integer NOT NULL,
    "available_at" timestamp with time zone NOT NULL,
    "claimed_at" timestamp with time zone,
    "claimed_by" "text",
    "purged_at" timestamp with time zone,
    "verification_cycles" integer DEFAULT 0 NOT NULL,
    "completed_at" timestamp with time zone,
    "last_error" "text",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "storage_principals" "jsonb" NOT NULL,
    "search_namespace_prefixes" "jsonb" NOT NULL,
    "sandbox_resources" "jsonb" NOT NULL,
    "external_ingest_resources" "jsonb",
    "external_ingest_snapshot_at" timestamp with time zone,
    CONSTRAINT "project_deletion_jobs_external_ingest_snapshot_check" CHECK (((("external_ingest_resources" IS NULL) AND ("external_ingest_snapshot_at" IS NULL)) OR (("external_ingest_resources" IS NOT NULL) AND ("external_ingest_snapshot_at" IS NOT NULL) AND "public"."_project_deletion_external_ingest_snapshot_valid"("project_id", "external_ingest_resources")))),
    CONSTRAINT "project_deletion_jobs_phase_check" CHECK (("phase" = ANY (ARRAY['drain'::"text", 'purge'::"text", 'verify'::"text"]))),
    CONSTRAINT "project_deletion_jobs_prefixes_check" CHECK (("object_prefixes" = "public"."_project_deletion_object_prefixes"("project_id", "storage_principals"))),
    CONSTRAINT "project_deletion_jobs_principals_check" CHECK ("public"."_project_deletion_principals_valid"("project_id", "requested_by", "storage_principals")),
    CONSTRAINT "project_deletion_jobs_quiescence_check" CHECK (("quiescence_seconds" >= 1800)),
    CONSTRAINT "project_deletion_jobs_sandbox_resources_check" CHECK ("public"."_project_deletion_sandbox_resources_valid"("sandbox_resources")),
    CONSTRAINT "project_deletion_jobs_search_prefixes_check" CHECK (("search_namespace_prefixes" = "public"."_project_deletion_search_prefixes"("project_id"))),
    CONSTRAINT "project_deletion_jobs_source_check" CHECK (("source" = ANY (ARRAY['project_delete'::"text", 'initialization_abandon'::"text", 'publication_abort'::"text"]))),
    CONSTRAINT "project_deletion_jobs_status_check" CHECK (("status" = ANY (ARRAY['pending'::"text", 'running'::"text", 'failed'::"text", 'completed'::"text"])))
);


ALTER TABLE "public"."project_deletion_jobs" OWNER TO "postgres";

--
-- Name: claim_project_deletion_jobs("text", integer, integer); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."claim_project_deletion_jobs"("p_worker_id" "text", "p_limit" integer DEFAULT 10, "p_lease_seconds" integer DEFAULT 300) RETURNS SETOF "public"."project_deletion_jobs"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
BEGIN
    RETURN QUERY
    WITH claimable AS (
        SELECT job.id
        FROM public.project_deletion_jobs job
        WHERE (
            job.status IN ('pending', 'failed') AND job.available_at <= now()
        ) OR (
            job.status = 'running'
            AND job.claimed_at < now() - make_interval(secs => GREATEST(p_lease_seconds, 30))
        )
        ORDER BY job.created_at
        FOR UPDATE SKIP LOCKED
        LIMIT GREATEST(1, LEAST(p_limit, 100))
    )
    UPDATE public.project_deletion_jobs job
    SET status = 'running',
        attempts = job.attempts + 1,
        claimed_at = now(),
        claimed_by = p_worker_id,
        updated_at = now(),
        last_error = NULL
    FROM claimable
    WHERE job.id = claimable.id
    RETURNING job.*;
END;
$$;


ALTER FUNCTION "public"."claim_project_deletion_jobs"("p_worker_id" "text", "p_limit" integer, "p_lease_seconds" integer) OWNER TO "postgres";

--
-- Name: project_create_operations; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."project_create_operations" (
    "actor_user_id" "uuid" NOT NULL,
    "operation_key" "text" NOT NULL,
    "payload_hash" "text" NOT NULL,
    "request_hash" "text" NOT NULL,
    "org_id" "text" NOT NULL,
    "project_id" "text" NOT NULL,
    "project_snapshot" "jsonb" NOT NULL,
    "result_metadata" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "publication_mode" "text" NOT NULL,
    "status" "text" DEFAULT 'initializing'::"text" NOT NULL,
    "initialization_attempts" integer DEFAULT 0 NOT NULL,
    "initialization_available_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "initialization_deadline_at" timestamp with time zone DEFAULT ("now"() + '24:00:00'::interval) NOT NULL,
    "initialization_claimed_at" timestamp with time zone,
    "initialization_claimed_by" "text",
    "initialization_last_error" "text",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "ready_at" timestamp with time zone,
    "replayed_at" timestamp with time zone,
    "deleted_at" timestamp with time zone,
    "dead_lettered_at" timestamp with time zone,
    CONSTRAINT "project_create_operations_hash_check" CHECK (("payload_hash" ~ '^[0-9a-f]{64}$'::"text")),
    CONSTRAINT "project_create_operations_key_check" CHECK (("operation_key" ~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'::"text")),
    CONSTRAINT "project_create_operations_publication_mode_check" CHECK (("publication_mode" = ANY (ARRAY['empty'::"text", 'deferred'::"text"]))),
    CONSTRAINT "project_create_operations_request_hash_check" CHECK (("request_hash" ~ '^[0-9a-f]{64}$'::"text")),
    CONSTRAINT "project_create_operations_result_metadata_check" CHECK (("jsonb_typeof"("result_metadata") = 'object'::"text")),
    CONSTRAINT "project_create_operations_status_check" CHECK (("status" = ANY (ARRAY['initializing'::"text", 'ready'::"text", 'deleted'::"text", 'dead_lettered'::"text"])))
);


ALTER TABLE "public"."project_create_operations" OWNER TO "postgres";

--
-- Name: claim_project_initialization_operations("text", integer, integer); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."claim_project_initialization_operations"("p_worker_id" "text", "p_limit" integer DEFAULT 1, "p_lease_seconds" integer DEFAULT 300) RETURNS SETOF "public"."project_create_operations"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
BEGIN
    RETURN QUERY
    WITH claimable AS (
        SELECT operation.actor_user_id, operation.operation_key
        FROM public.project_create_operations operation
        WHERE operation.status = 'initializing'
          AND operation.initialization_available_at <= now()
          AND (
              operation.initialization_claimed_at IS NULL
              OR operation.initialization_claimed_at
                 < now() - make_interval(secs => GREATEST(p_lease_seconds, 30))
          )
          AND EXISTS (
              SELECT 1 FROM public.projects project
              WHERE project.id = operation.project_id
          )
        ORDER BY operation.created_at
        FOR UPDATE SKIP LOCKED
        LIMIT GREATEST(1, LEAST(p_limit, 100))
    )
    UPDATE public.project_create_operations operation
    SET initialization_attempts = operation.initialization_attempts + 1,
        initialization_claimed_at = now(),
        initialization_claimed_by = p_worker_id,
        initialization_last_error = NULL
    FROM claimable
    WHERE operation.actor_user_id = claimable.actor_user_id
      AND operation.operation_key = claimable.operation_key
    RETURNING operation.*;
END;
$$;


ALTER FUNCTION "public"."claim_project_initialization_operations"("p_worker_id" "text", "p_limit" integer, "p_lease_seconds" integer) OWNER TO "postgres";

--
-- Name: claim_seat_proposal_batch(integer, integer); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."claim_seat_proposal_batch"("p_limit" integer DEFAULT 25, "p_lease_seconds" integer DEFAULT 60) RETURNS SETOF "public"."organization_billing_operations"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
BEGIN
    IF p_limit < 1 OR p_limit > 100 THEN
        RAISE EXCEPTION 'p_limit must be between 1 and 100';
    END IF;
    IF p_lease_seconds < 10 OR p_lease_seconds > 3600 THEN
        RAISE EXCEPTION 'p_lease_seconds must be between 10 and 3600';
    END IF;

    RETURN QUERY
    UPDATE public.organization_billing_operations AS operation
    SET attempts = operation.attempts + 1,
        next_attempt_at = now() + make_interval(secs => p_lease_seconds),
        last_error = NULL
    WHERE operation.id IN (
        SELECT candidate.id
        FROM public.organization_billing_operations AS candidate
        WHERE candidate.kind IN ('member_activation', 'member_deactivation')
          AND candidate.status IN ('pending', 'awaiting_confirmation')
          AND candidate.quote_id IS NULL
          AND candidate.next_attempt_at <= now()
        ORDER BY candidate.next_attempt_at, candidate.created_at
        FOR UPDATE SKIP LOCKED
        LIMIT p_limit
    )
    RETURNING operation.*;
END;
$$;


ALTER FUNCTION "public"."claim_seat_proposal_batch"("p_limit" integer, "p_lease_seconds" integer) OWNER TO "postgres";

--
-- Name: claim_storage_reconciliation_batch(integer, integer, integer); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."claim_storage_reconciliation_batch"("p_limit" integer DEFAULT 25, "p_min_age_seconds" integer DEFAULT 86400, "p_claim_lease_seconds" integer DEFAULT 900) RETURNS TABLE("org_id" "text")
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
BEGIN
    IF p_limit <= 0 OR p_limit > 200 OR p_min_age_seconds < 60
       OR p_claim_lease_seconds < 60 OR p_claim_lease_seconds > 86400 THEN
        RAISE EXCEPTION 'invalid storage reconciliation claim parameters'
            USING ERRCODE = '22023';
    END IF;

    RETURN QUERY
    WITH candidates AS (
        SELECT o.id
        FROM public.organizations o
        LEFT JOIN public.organization_usage_counters c
          ON c.org_id = o.id AND c.metric = 'storage.logical_bytes'
        WHERE (
            c.full_reconciled_at IS NULL
            OR c.full_reconciled_at <= now() - make_interval(secs => p_min_age_seconds)
        ) AND (
            c.reconciliation_claimed_at IS NULL
            OR c.reconciliation_claimed_at <= now() - make_interval(
                secs => p_claim_lease_seconds
            )
          )
        ORDER BY c.full_reconciled_at NULLS FIRST, o.id
        FOR UPDATE OF o SKIP LOCKED
        LIMIT p_limit
    ), claimed AS (
        INSERT INTO public.organization_usage_counters (
            org_id, metric, value, version, reconciliation_claimed_at
        )
        SELECT id, 'storage.logical_bytes', 0, 0, now()
        FROM candidates
        ON CONFLICT ON CONSTRAINT organization_usage_counters_pkey DO UPDATE SET
            reconciliation_claimed_at = now()
        RETURNING organization_usage_counters.org_id
    )
    SELECT claimed.org_id FROM claimed;
END;
$$;


ALTER FUNCTION "public"."claim_storage_reconciliation_batch"("p_limit" integer, "p_min_age_seconds" integer, "p_claim_lease_seconds" integer) OWNER TO "postgres";

--
-- Name: claim_version_outbox_batch(integer); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."claim_version_outbox_batch"("p_limit" integer DEFAULT 50) RETURNS TABLE("id" bigint, "project_id" "text", "commit_id" "text", "event_type" "text", "payload" "jsonb", "attempts" integer)
    LANGUAGE "plpgsql"
    AS $$
BEGIN
    RETURN QUERY
    WITH picked AS (
        SELECT o.id
          FROM public.mut_version_outbox o
         WHERE o.processed_at IS NULL
           AND (o.locked_at IS NULL OR o.locked_at < NOW() - INTERVAL '5 minutes')
           AND o.created_at < NOW() - INTERVAL '15 seconds'
           AND o.attempts < 25
         ORDER BY o.created_at ASC, o.id ASC
         LIMIT GREATEST(1, LEAST(COALESCE(p_limit, 50), 500))
         FOR UPDATE SKIP LOCKED
    )
    UPDATE public.mut_version_outbox o
       SET locked_at = NOW(),
           attempts = o.attempts + 1,
           last_error = NULL
      FROM picked
     WHERE o.id = picked.id
    RETURNING o.id, o.project_id, o.commit_id, o.event_type, o.payload, o.attempts;
END;
$$;


ALTER FUNCTION "public"."claim_version_outbox_batch"("p_limit" integer) OWNER TO "postgres";

--
-- Name: complete_mut_version_outbox(bigint); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."complete_mut_version_outbox"("p_id" bigint) RETURNS boolean
    LANGUAGE "sql"
    AS $$ SELECT public.complete_version_outbox(p_id); $$;


ALTER FUNCTION "public"."complete_mut_version_outbox"("p_id" bigint) OWNER TO "postgres";

--
-- Name: complete_project_deletion_job("text", "text"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."complete_project_deletion_job"("p_job_id" "text", "p_worker_id" "text") RETURNS boolean
    LANGUAGE "sql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
UPDATE public.project_deletion_jobs
SET status = 'completed', completed_at = now(), updated_at = now()
WHERE id = p_job_id
  AND phase = 'verify'
  AND status = 'running'
  AND claimed_by = p_worker_id
RETURNING true;
$$;


ALTER FUNCTION "public"."complete_project_deletion_job"("p_job_id" "text", "p_worker_id" "text") OWNER TO "postgres";

--
-- Name: complete_project_initialization("text", "text", "uuid"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."complete_project_initialization"("p_operation_key" "text", "p_project_id" "text", "p_actor_user_id" "uuid") RETURNS "jsonb"
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
    IF COALESCE(project_row.version_root_hash, '') = ''
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


ALTER FUNCTION "public"."complete_project_initialization"("p_operation_key" "text", "p_project_id" "text", "p_actor_user_id" "uuid") OWNER TO "postgres";

--
-- Name: complete_project_storage_inventory(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."complete_project_storage_inventory"() RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
DECLARE
    state public.project_storage_inventory_state%ROWTYPE;
BEGIN
    SELECT * INTO state
    FROM public.project_storage_inventory_state
    WHERE singleton
    FOR UPDATE;
    IF state.inventory_complete THEN
        RETURN jsonb_build_object('outcome', 'replayed');
    END IF;
    IF state.inventory_digest IS NULL
       OR state.verification_digest IS DISTINCT FROM state.inventory_digest
       OR state.verification_object_count IS DISTINCT FROM state.observed_object_count
       OR state.verification_multipart_count IS DISTINCT FROM state.observed_multipart_count THEN
        RETURN jsonb_build_object('outcome', 'verification_required');
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.project_storage_orphan_prefixes orphan
        WHERE orphan.status = 'pending'
    ) THEN
        RETURN jsonb_build_object('outcome', 'orphan_cleanup_required');
    END IF;
    UPDATE public.project_storage_inventory_state
    SET inventory_complete = true, completed_at = now(), updated_at = now()
    WHERE singleton;
    RETURN jsonb_build_object('outcome', 'completed');
END;
$$;


ALTER FUNCTION "public"."complete_project_storage_inventory"() OWNER TO "postgres";

--
-- Name: complete_version_outbox(bigint); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."complete_version_outbox"("p_id" bigint) RETURNS boolean
    LANGUAGE "plpgsql"
    AS $$
DECLARE
    rows_affected INT;
BEGIN
    UPDATE public.mut_version_outbox
       SET processed_at = NOW(),
           locked_at = NULL,
           last_error = NULL
     WHERE id = p_id
       AND processed_at IS NULL;
    GET DIAGNOSTICS rows_affected = ROW_COUNT;
    RETURN rows_affected > 0;
END;
$$;


ALTER FUNCTION "public"."complete_version_outbox"("p_id" bigint) OWNER TO "postgres";

--
-- Name: count_billable_organization_members("text"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."count_billable_organization_members"("p_org_id" "text") RETURNS bigint
    LANGUAGE "sql" STABLE SECURITY DEFINER
    SET "search_path" TO ''
    AS $$
    SELECT count(*)::bigint
    FROM public.org_members om
    WHERE om.org_id = p_org_id
      AND (
          om.role IN ('owner', 'member')
          OR EXISTS (
              SELECT 1
              FROM public.project_members pm
              WHERE pm.org_id = om.org_id
                AND pm.user_id = om.user_id
                AND pm.role IN ('admin', 'editor')
          )
      );
$$;


ALTER FUNCTION "public"."count_billable_organization_members"("p_org_id" "text") OWNER TO "postgres";

--
-- Name: count_children_batch("text"[]); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."count_children_batch"("p_parent_ids" "text"[]) RETURNS TABLE("parent_id" "text", "child_count" bigint)
    LANGUAGE "plpgsql" STABLE
    AS $$
BEGIN
    RETURN QUERY
    SELECT p.id AS parent_id, COALESCE(count(c.id), 0)::BIGINT AS child_count
    FROM content_nodes p
    LEFT JOIN content_nodes c
      ON c.project_id = p.project_id
      AND c.mut_path LIKE p.mut_path || '/%'
      AND c.depth = p.depth + 1
    WHERE p.id = ANY(p_parent_ids)
    GROUP BY p.id;
END;
$$;


ALTER FUNCTION "public"."count_children_batch"("p_parent_ids" "text"[]) OWNER TO "postgres";

--
-- Name: create_project_idempotent("text", "text", "text", "text", "text", "text", "uuid", "text", "text", integer, "text", "jsonb"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."create_project_idempotent"("p_operation_key" "text", "p_payload_hash" "text", "p_project_id" "text", "p_name" "text", "p_description" "text", "p_org_id" "text", "p_created_by" "uuid", "p_share_token" "text", "p_publication_mode" "text", "p_project_limit" integer DEFAULT NULL::integer, "p_request_hash" "text" DEFAULT NULL::"text", "p_result_metadata" "jsonb" DEFAULT '{}'::"jsonb") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $_$
DECLARE
    existing_operation public.project_create_operations%ROWTYPE;
    created_project public.projects%ROWTYPE;
    current_count bigint;
    requested_name_slot bigint;
    resolved_name_slot bigint;
    resolved_project_name text;
BEGIN
    IF p_operation_key !~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
       OR p_payload_hash !~ '^[0-9a-f]{64}$'
       OR COALESCE(p_request_hash, p_payload_hash) !~ '^[0-9a-f]{64}$'
       OR p_project_id !~ '^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$'
       OR p_publication_mode NOT IN ('empty', 'deferred')
       OR jsonb_typeof(COALESCE(p_result_metadata, '{}'::jsonb)) <> 'object' THEN
        RETURN jsonb_build_object('outcome', 'invalid');
    END IF;

    -- Same-key requests serialize independently of organization quota.  This
    -- makes a durable replay observable before any capacity admission.
    PERFORM pg_advisory_xact_lock(
        hashtextextended('project-create:' || p_created_by::text || ':' || p_operation_key, 0)
    );
    SELECT * INTO existing_operation
    FROM public.project_create_operations
    WHERE actor_user_id = p_created_by AND operation_key = p_operation_key
    FOR UPDATE;

    IF FOUND THEN
        -- request_hash is the durable identity of the caller's intent.  The
        -- payload hash may include a resolved mutable source (for example a
        -- Registry `latest` release), so a retry after source drift must
        -- replay the first admitted result instead of turning into a false
        -- idempotency conflict.
        IF existing_operation.request_hash IS DISTINCT FROM
              COALESCE(p_request_hash, p_payload_hash)
           OR existing_operation.publication_mode IS DISTINCT FROM p_publication_mode THEN
            RETURN jsonb_build_object('outcome', 'conflict');
        END IF;
        IF existing_operation.status = 'dead_lettered' THEN
            RETURN jsonb_build_object('outcome', 'dead_lettered');
        END IF;
        IF existing_operation.status = 'deleted'
           OR NOT EXISTS (
               SELECT 1 FROM public.projects p
               WHERE p.id = existing_operation.project_id
           )
           OR EXISTS (
               SELECT 1 FROM public.projects p
               WHERE p.id = existing_operation.project_id
                 AND p.lifecycle_status = 'deleting'
           ) THEN
            RETURN jsonb_build_object(
                'outcome', 'gone',
                'project_id', existing_operation.project_id
            );
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM public.org_members om
            WHERE om.org_id = existing_operation.org_id
              AND om.user_id = p_created_by
        ) THEN
            RETURN jsonb_build_object('outcome', 'forbidden');
        END IF;
        UPDATE public.project_create_operations
        SET replayed_at = now()
        WHERE actor_user_id = p_created_by AND operation_key = p_operation_key;
        RETURN jsonb_build_object(
            'outcome', CASE existing_operation.status
                WHEN 'ready' THEN 'replayed'
                ELSE 'initializing_replayed'
            END,
            'project', existing_operation.project_snapshot,
            'result_metadata', existing_operation.result_metadata
        );
    END IF;

    -- Lock the organization row so every new Project admission for one tenant
    -- observes a serialized Project count.  Same-key replay already returned.
    PERFORM 1 FROM public.organizations WHERE id = p_org_id FOR UPDATE;
    IF NOT FOUND OR NOT EXISTS (
        SELECT 1 FROM public.org_members om
        WHERE om.org_id = p_org_id AND om.user_id = p_created_by
    ) THEN
        RETURN jsonb_build_object('outcome', 'forbidden');
    END IF;

    SELECT count(*) INTO current_count
    FROM public.projects p
    WHERE p.org_id = p_org_id;
    IF p_project_limit IS NOT NULL AND current_count >= p_project_limit THEN
        RETURN jsonb_build_object(
            'outcome', 'capacity_exceeded',
            'current', current_count,
            'maximum', p_project_limit
        );
    END IF;

    resolved_project_name := p_name;
    requested_name_slot := public._untitled_project_slot(p_name);
    IF requested_name_slot IS NOT NULL THEN
        IF EXISTS (
            SELECT 1
            FROM public.projects project
            WHERE project.org_id = p_org_id
              AND public._untitled_project_slot(project.name) = requested_name_slot
        ) THEN
            SELECT candidate.slot INTO resolved_name_slot
            FROM generate_series(1::bigint, current_count + 1) AS candidate(slot)
            WHERE NOT EXISTS (
                SELECT 1
                FROM public.projects project
                WHERE project.org_id = p_org_id
                  AND public._untitled_project_slot(project.name) = candidate.slot
            )
            ORDER BY candidate.slot
            LIMIT 1;
        ELSE
            resolved_name_slot := requested_name_slot;
        END IF;
        resolved_project_name := CASE resolved_name_slot
            WHEN 1 THEN 'Untitled Project'
            ELSE 'Untitled Project ' || resolved_name_slot::text
        END;
    END IF;

    -- Root refs deliberately remain uninitialized here.  The existing L5
    -- VersionWriteEngine.initialize_project_tree entry point owns the
    -- canonical empty-root write; this transaction prepares only Project,
    -- creator Admin, and the durable initialization operation.
    INSERT INTO public.projects (
        id, name, description, org_id, created_by, share_token,
        lifecycle_status
    ) VALUES (
        p_project_id, resolved_project_name, p_description, p_org_id, p_created_by,
        p_share_token, 'initializing'
    ) RETURNING * INTO created_project;

    INSERT INTO public.project_members (
        id, org_id, project_id, user_id, role, granted_by
    ) VALUES (
        gen_random_uuid()::text, p_org_id, p_project_id,
        p_created_by, 'admin', p_created_by
    );

    INSERT INTO public.project_create_operations (
        actor_user_id, operation_key, payload_hash, request_hash, org_id, project_id,
        project_snapshot, result_metadata, publication_mode,
        initialization_available_at, initialization_deadline_at
    ) VALUES (
        p_created_by, p_operation_key, p_payload_hash,
        COALESCE(p_request_hash, p_payload_hash), p_org_id, p_project_id,
        to_jsonb(created_project), COALESCE(p_result_metadata, '{}'::jsonb),
        p_publication_mode,
        CASE p_publication_mode
            WHEN 'deferred' THEN now() + interval '6 hours'
            ELSE now()
        END,
        CASE p_publication_mode
            WHEN 'deferred' THEN now() + interval '6 hours'
            ELSE now() + interval '24 hours'
        END
    );

    RETURN jsonb_build_object(
        'outcome', 'initializing_created',
        'project', to_jsonb(created_project),
        'result_metadata', COALESCE(p_result_metadata, '{}'::jsonb)
    );
END;
$_$;


ALTER FUNCTION "public"."create_project_idempotent"("p_operation_key" "text", "p_payload_hash" "text", "p_project_id" "text", "p_name" "text", "p_description" "text", "p_org_id" "text", "p_created_by" "uuid", "p_share_token" "text", "p_publication_mode" "text", "p_project_limit" integer, "p_request_hash" "text", "p_result_metadata" "jsonb") OWNER TO "postgres";

--
-- Name: dead_letter_project_initialization_operation("text", "uuid", "text", "text"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."dead_letter_project_initialization_operation"("p_operation_key" "text", "p_actor_user_id" "uuid", "p_worker_id" "text", "p_error" "text") RETURNS boolean
    LANGUAGE "sql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
UPDATE public.project_create_operations
SET status = 'dead_lettered',
    dead_lettered_at = COALESCE(dead_lettered_at, now()),
    initialization_claimed_at = NULL,
    initialization_claimed_by = NULL,
    initialization_last_error = left(p_error, 2000)
WHERE actor_user_id = p_actor_user_id
  AND operation_key = p_operation_key
  AND status = 'initializing'
  AND initialization_claimed_by = p_worker_id
RETURNING true;
$$;


ALTER FUNCTION "public"."dead_letter_project_initialization_operation"("p_operation_key" "text", "p_actor_user_id" "uuid", "p_worker_id" "text", "p_error" "text") OWNER TO "postgres";

--
-- Name: delete_empty_organization_control_plane("text", "uuid"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."delete_empty_organization_control_plane"("p_org_id" "text", "p_actor_user_id" "uuid") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
DECLARE
    actor_role text;
    actor_org_count bigint;
BEGIN
    IF p_org_id IS NULL OR btrim(p_org_id) = '' OR p_actor_user_id IS NULL THEN
        RETURN jsonb_build_object('outcome', 'invalid_request');
    END IF;

    -- Serialize delete decisions across all Organizations owned by this actor,
    -- so two concurrent requests cannot both observe "one other org" and
    -- delete the actor's final two Organizations.
    PERFORM pg_advisory_xact_lock(
        hashtextextended('organization-delete:' || p_actor_user_id::text, 0)
    );

    -- Lock the Organization before authorization facts.  Project creation
    -- also locks this row, while FK inserts take a conflicting key-share lock;
    -- no new Project can appear between the emptiness proof and DELETE.
    PERFORM 1
    FROM public.organizations organization
    WHERE organization.id = p_org_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('outcome', 'not_found');
    END IF;

    -- Lock all of the actor's memberships in a deterministic order before the
    -- final count.  The advisory lock covers concurrent delete RPCs and these
    -- row locks linearize membership removal/transfer.
    PERFORM 1
    FROM public.org_members member
    WHERE member.user_id = p_actor_user_id
    ORDER BY member.org_id
    FOR UPDATE;
    SELECT member.role INTO actor_role
    FROM public.org_members member
    WHERE member.org_id = p_org_id
      AND member.user_id = p_actor_user_id;
    IF actor_role IS DISTINCT FROM 'owner' THEN
        RETURN jsonb_build_object('outcome', 'forbidden');
    END IF;

    SELECT count(*) INTO actor_org_count
    FROM public.org_members member
    WHERE member.user_id = p_actor_user_id;
    IF actor_org_count <= 1 THEN
        RETURN jsonb_build_object('outcome', 'only_organization');
    END IF;

    IF EXISTS (
        SELECT 1
        FROM public.projects project
        WHERE project.org_id = p_org_id
    ) THEN
        RETURN jsonb_build_object('outcome', 'organization_not_empty');
    END IF;

    -- A drained Project row is not the end of deletion: S3 objects, search
    -- namespaces and provider sandboxes remain billable until verify completes.
    -- Lock the durable jobs so completion and Organization deletion have one
    -- linear order.
    PERFORM job.id
    FROM public.project_deletion_jobs job
    WHERE job.org_id = p_org_id
    ORDER BY job.id
    FOR UPDATE;
    IF EXISTS (
        SELECT 1
        FROM public.project_deletion_jobs job
        WHERE job.org_id = p_org_id
          AND job.status <> 'completed'
    ) THEN
        RETURN jsonb_build_object(
            'outcome', 'organization_deletion_in_progress'
        );
    END IF;

    DELETE FROM public.organizations organization
    WHERE organization.id = p_org_id;
    RETURN jsonb_build_object('outcome', 'deleted');
END;
$$;


ALTER FUNCTION "public"."delete_empty_organization_control_plane"("p_org_id" "text", "p_actor_user_id" "uuid") OWNER TO "postgres";

--
-- Name: delete_project_control_plane("text", "uuid", integer); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."delete_project_control_plane"("p_project_id" "text", "p_actor_user_id" "uuid", "p_quiescence_seconds" integer DEFAULT 3600) RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
DECLARE
    resolved_org_id text;
    effective_role text;
    deletion_job public.project_deletion_jobs%ROWTYPE;
BEGIN
    -- Serialize the destructive decision with every mutable authorization
    -- fact it consumes.  The fixed Project -> org membership -> Project
    -- membership order makes the final role resolution linearizable with a
    -- concurrent downgrade/removal instead of authorizing from a stale fact.
    SELECT project.org_id INTO resolved_org_id
    FROM public.projects project
    WHERE project.id = p_project_id
      AND project.lifecycle_status = 'ready'
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('outcome', 'not_found');
    END IF;

    PERFORM 1
    FROM public.org_members member
    WHERE member.org_id = resolved_org_id
      AND member.user_id = p_actor_user_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('outcome', 'not_found');
    END IF;

    -- The row is optional for an Organization owner or org-visible Viewer,
    -- but when present it must stay locked through the destructive commit.
    PERFORM 1
    FROM public.project_members member
    WHERE member.project_id = p_project_id
      AND member.org_id = resolved_org_id
      AND member.user_id = p_actor_user_id
    FOR UPDATE;

    SELECT role.org_id, role.effective_role
      INTO resolved_org_id, effective_role
    FROM public.resolve_project_role(p_project_id, p_actor_user_id) role;
    IF effective_role IS NULL THEN
        RETURN jsonb_build_object('outcome', 'not_found');
    END IF;
    IF effective_role <> 'admin' THEN
        RETURN jsonb_build_object('outcome', 'forbidden');
    END IF;

    INSERT INTO public.project_deletion_jobs (
        project_id, org_id, requested_by, source, object_prefixes,
        quiescence_seconds, available_at
    ) VALUES (
        p_project_id, resolved_org_id, p_actor_user_id, 'project_delete',
        jsonb_build_array(
            'version/' || p_project_id || '/',
            'mut/' || p_project_id || '/',
            'projects/' || p_project_id || '/'
        ),
        GREATEST(COALESCE(p_quiescence_seconds, 3600), 1800),
        now()
    )
    ON CONFLICT (project_id) DO UPDATE
      SET updated_at = now()
    RETURNING * INTO deletion_job;

    UPDATE public.project_create_operations
    SET status = 'deleted', deleted_at = COALESCE(deleted_at, now())
    WHERE project_id = p_project_id AND status IN ('initializing', 'ready');
    UPDATE public.git_credential_issue_operations
    SET status = 'deleted', revoked_at = COALESCE(revoked_at, now())
    WHERE project_id = p_project_id AND status = 'active';
    UPDATE public.projects
    SET lifecycle_status = 'deleting', updated_at = now()
    WHERE id = p_project_id AND lifecycle_status = 'ready';

    RETURN jsonb_build_object(
        'outcome', 'deleted',
        'job', to_jsonb(deletion_job)
    );
END;
$$;


ALTER FUNCTION "public"."delete_project_control_plane"("p_project_id" "text", "p_actor_user_id" "uuid", "p_quiescence_seconds" integer) OWNER TO "postgres";

--
-- Name: drain_project_deletion_job("text", "text"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."drain_project_deletion_job"("p_job_id" "text", "p_worker_id" "text") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
DECLARE
    job public.project_deletion_jobs%ROWTYPE;
    active_count bigint;
    next_expiry timestamptz;
    resolved_principals jsonb;
    resolved_prefixes jsonb;
    resolved_sandboxes jsonb;
BEGIN
    SELECT * INTO job
    FROM public.project_deletion_jobs deletion_job
    WHERE deletion_job.id = p_job_id
    FOR UPDATE;
    IF NOT FOUND
       OR job.status <> 'running'
       OR job.phase <> 'drain'
       OR job.claimed_by IS DISTINCT FROM p_worker_id THEN
        RETURN jsonb_build_object('outcome', 'claim_lost');
    END IF;

    DELETE FROM public.project_write_leases lease
    WHERE lease.project_id = job.project_id AND lease.expires_at <= now();
    SELECT count(*), min(lease.expires_at)
      INTO active_count, next_expiry
    FROM public.project_write_leases lease
    WHERE lease.project_id = job.project_id AND lease.expires_at > now();

    IF active_count > 0 THEN
        UPDATE public.project_deletion_jobs
        SET status = 'pending',
            available_at = LEAST(next_expiry, now() + interval '60 seconds'),
            claimed_at = NULL,
            claimed_by = NULL,
            updated_at = now()
        WHERE id = p_job_id;
        RETURN jsonb_build_object(
            'outcome', 'waiting',
            'active_leases', active_count,
            'next_expiry', next_expiry
        );
    END IF;

    IF job.external_ingest_resources IS NULL
       OR job.external_ingest_snapshot_at IS NULL THEN
        -- Keep the claim: the worker now has a race-free window in which to
        -- snapshot provider ownership and immediately call drain again.
        RETURN jsonb_build_object('outcome', 'snapshot_required');
    END IF;
    IF NOT public._project_deletion_external_ingest_snapshot_valid(
        job.project_id, job.external_ingest_resources
    ) THEN
        RETURN jsonb_build_object('outcome', 'invalid_external_ingest_snapshot');
    END IF;

    resolved_principals := public._project_deletion_storage_principals(
        job.project_id, job.requested_by
    );
    resolved_prefixes := public._project_deletion_object_prefixes(
        job.project_id, resolved_principals
    );
    resolved_sandboxes := public._project_deletion_sandbox_resources(job.project_id);
    UPDATE public.project_deletion_jobs
    SET storage_principals = resolved_principals,
        object_prefixes = resolved_prefixes,
        search_namespace_prefixes = public._project_deletion_search_prefixes(
            job.project_id
        ),
        sandbox_resources = resolved_sandboxes,
        updated_at = now()
    WHERE id = p_job_id;

    UPDATE public.project_create_operations
    SET status = 'deleted', deleted_at = COALESCE(deleted_at, now())
    WHERE project_id = job.project_id
      AND status IN ('initializing', 'ready', 'dead_lettered');
    UPDATE public.git_credential_issue_operations
    SET status = 'deleted', revoked_at = COALESCE(revoked_at, now())
    WHERE project_id = job.project_id AND status = 'active';
    DELETE FROM public.projects WHERE id = job.project_id;

    UPDATE public.project_deletion_jobs
    SET phase = 'purge',
        status = 'pending',
        available_at = now(),
        claimed_at = NULL,
        claimed_by = NULL,
        updated_at = now()
    WHERE id = p_job_id;
    RETURN jsonb_build_object('outcome', 'drained');
END;
$$;


ALTER FUNCTION "public"."drain_project_deletion_job"("p_job_id" "text", "p_worker_id" "text") OWNER TO "postgres";

--
-- Name: enqueue_missing_entitlement_provisioning(integer); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."enqueue_missing_entitlement_provisioning"("p_limit" integer DEFAULT 100) RETURNS integer
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
DECLARE
    inserted_count integer;
BEGIN
    IF p_limit < 1 OR p_limit > 1000 THEN
        RAISE EXCEPTION 'p_limit must be between 1 and 1000';
    END IF;

    WITH candidates AS (
        SELECT organization.id
        FROM public.organizations AS organization
        LEFT JOIN public.organization_entitlements AS entitlement
          ON entitlement.org_id = organization.id
        WHERE entitlement.org_id IS NULL
        ORDER BY organization.id
        LIMIT p_limit
    )
    INSERT INTO public.organization_billing_operations (
        org_id,
        kind,
        status,
        idempotency_key,
        request_payload
    )
    SELECT
        candidate.id,
        'entitlement_provision',
        'pending',
        'entitlement-provision:v1',
        jsonb_build_object('schema_version', '1.0')
    FROM candidates AS candidate
    ON CONFLICT (org_id, idempotency_key) DO NOTHING;

    GET DIAGNOSTICS inserted_count = ROW_COUNT;
    RETURN inserted_count;
END;
$$;


ALTER FUNCTION "public"."enqueue_missing_entitlement_provisioning"("p_limit" integer) OWNER TO "postgres";

--
-- Name: access_surfaces; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."access_surfaces" (
    "id" "text" DEFAULT ("gen_random_uuid"())::"text" NOT NULL,
    "org_id" "text" NOT NULL,
    "project_id" "text" NOT NULL,
    "scope_id" "text",
    "kind" "text" NOT NULL,
    "name" "text" NOT NULL,
    "status" "text" DEFAULT 'active'::"text" NOT NULL,
    "principal_type" "text",
    "principal_id" "text",
    "config" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "created_by" "uuid",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "access_surfaces_config_no_runtime_secrets" CHECK (((NOT ("config" ? 'api_key'::"text")) AND (NOT ("config" ? 'mcp_api_key'::"text")) AND (NOT ("config" ? 'access_key'::"text")))),
    CONSTRAINT "access_surfaces_kind_check" CHECK (("kind" = ANY (ARRAY['git_remote'::"text", 'cli'::"text", 'agent'::"text", 'mcp'::"text", 'sandbox'::"text"]))),
    CONSTRAINT "access_surfaces_name_check" CHECK (("name" <> ''::"text")),
    CONSTRAINT "access_surfaces_status_check" CHECK (("status" = ANY (ARRAY['active'::"text", 'paused'::"text", 'error'::"text", 'disabled'::"text"])))
);


ALTER TABLE "public"."access_surfaces" OWNER TO "postgres";

--
-- Name: ensure_repository_target_access_surfaces("text", "text", "uuid", "text", "text"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."ensure_repository_target_access_surfaces"("p_project_id" "text", "p_scope_id" "text", "p_created_by" "uuid" DEFAULT NULL::"uuid", "p_git_surface_id" "text" DEFAULT NULL::"text", "p_cli_surface_id" "text" DEFAULT NULL::"text") RETURNS SETOF "public"."access_surfaces"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
DECLARE
    selected_org_id text;
    selected_path text := '';
    selected_max_mode text := 'rw';
BEGIN
    -- Serialize per target so concurrent enable/attach requests converge on
    -- the same Surfaces without treating a uniqueness race as an error.
    PERFORM pg_advisory_xact_lock(
        hashtextextended(p_project_id || E'\n' || COALESCE(p_scope_id, ''), 0)
    );

    SELECT org_id INTO selected_org_id
    FROM public.projects
    WHERE id = p_project_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Repository target Project is invalid';
    END IF;

    IF p_scope_id IS NOT NULL THEN
        SELECT path, max_mode INTO selected_path, selected_max_mode
        FROM public.repository_scopes
        WHERE id = p_scope_id AND project_id = p_project_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'Repository Scope target is invalid';
        END IF;
    END IF;

    INSERT INTO public.access_surfaces (
        id, org_id, project_id, scope_id, kind, name, status,
        principal_type, principal_id, config, created_by
    ) VALUES (
        COALESCE(p_git_surface_id, gen_random_uuid()::text),
        selected_org_id, p_project_id, p_scope_id, 'git_remote', 'Git Remote',
        'active', CASE WHEN p_scope_id IS NULL THEN 'project' ELSE 'scope' END,
        COALESCE(p_scope_id, p_project_id),
        jsonb_build_object(
            'mode', selected_max_mode,
            'direction', 'bidirectional'
        ) || CASE WHEN p_scope_id IS NULL THEN '{}'::jsonb
                  ELSE jsonb_build_object('path', selected_path) END,
        p_created_by
    ) ON CONFLICT DO NOTHING;

    INSERT INTO public.access_surfaces (
        id, org_id, project_id, scope_id, kind, name, status,
        principal_type, principal_id, config, created_by
    ) VALUES (
        COALESCE(p_cli_surface_id, gen_random_uuid()::text),
        selected_org_id, p_project_id, p_scope_id, 'cli', 'FS CLI',
        'active', CASE WHEN p_scope_id IS NULL THEN 'project' ELSE 'scope' END,
        COALESCE(p_scope_id, p_project_id),
        jsonb_build_object(
            'mode', selected_max_mode,
            'direction', 'bidirectional'
        ) || CASE WHEN p_scope_id IS NULL THEN '{}'::jsonb
                  ELSE jsonb_build_object('path', selected_path) END,
        p_created_by
    ) ON CONFLICT DO NOTHING;

    RETURN QUERY
    SELECT s.*
    FROM public.access_surfaces s
    WHERE s.project_id = p_project_id
      AND s.scope_id IS NOT DISTINCT FROM p_scope_id
      AND s.kind IN ('git_remote', 'cli')
    ORDER BY s.kind;
END;
$$;


ALTER FUNCTION "public"."ensure_repository_target_access_surfaces"("p_project_id" "text", "p_scope_id" "text", "p_created_by" "uuid", "p_git_surface_id" "text", "p_cli_surface_id" "text") OWNER TO "postgres";

--
-- Name: fail_mut_version_outbox(bigint, "text"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."fail_mut_version_outbox"("p_id" bigint, "p_error" "text") RETURNS boolean
    LANGUAGE "sql"
    AS $$ SELECT public.fail_version_outbox(p_id,p_error); $$;


ALTER FUNCTION "public"."fail_mut_version_outbox"("p_id" bigint, "p_error" "text") OWNER TO "postgres";

--
-- Name: fail_project_deletion_job("text", "text", "text", integer); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."fail_project_deletion_job"("p_job_id" "text", "p_worker_id" "text", "p_error" "text", "p_retry_after_seconds" integer DEFAULT 60) RETURNS boolean
    LANGUAGE "sql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
UPDATE public.project_deletion_jobs
SET status = 'failed',
    available_at = now() + make_interval(secs => GREATEST(p_retry_after_seconds, 1)),
    last_error = left(p_error, 2000),
    updated_at = now()
WHERE id = p_job_id AND status = 'running' AND claimed_by = p_worker_id
RETURNING true;
$$;


ALTER FUNCTION "public"."fail_project_deletion_job"("p_job_id" "text", "p_worker_id" "text", "p_error" "text", "p_retry_after_seconds" integer) OWNER TO "postgres";

--
-- Name: fail_project_initialization_operation("text", "uuid", "text", "text", integer); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."fail_project_initialization_operation"("p_operation_key" "text", "p_actor_user_id" "uuid", "p_worker_id" "text", "p_error" "text", "p_retry_after_seconds" integer DEFAULT 60) RETURNS boolean
    LANGUAGE "sql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
UPDATE public.project_create_operations
SET initialization_available_at = now() + make_interval(
        secs => GREATEST(p_retry_after_seconds, 1)
    ),
    initialization_claimed_at = NULL,
    initialization_claimed_by = NULL,
    initialization_last_error = left(p_error, 2000)
WHERE actor_user_id = p_actor_user_id
  AND operation_key = p_operation_key
  AND status = 'initializing'
  AND initialization_claimed_by = p_worker_id
RETURNING true;
$$;


ALTER FUNCTION "public"."fail_project_initialization_operation"("p_operation_key" "text", "p_actor_user_id" "uuid", "p_worker_id" "text", "p_error" "text", "p_retry_after_seconds" integer) OWNER TO "postgres";

--
-- Name: fail_version_outbox(bigint, "text"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."fail_version_outbox"("p_id" bigint, "p_error" "text") RETURNS boolean
    LANGUAGE "plpgsql"
    AS $$
DECLARE
    rows_affected INT;
BEGIN
    UPDATE public.mut_version_outbox
       SET locked_at = NULL,
           last_error = LEFT(COALESCE(p_error, ''), 2000)
     WHERE id = p_id
       AND processed_at IS NULL;
    GET DIAGNOSTICS rows_affected = ROW_COUNT;
    RETURN rows_affected > 0;
END;
$$;


ALTER FUNCTION "public"."fail_version_outbox"("p_id" bigint, "p_error" "text") OWNER TO "postgres";

--
-- Name: finalize_project_storage_inventory_scan(bigint, bigint, "text"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."finalize_project_storage_inventory_scan"("p_observed_object_count" bigint, "p_observed_multipart_count" bigint, "p_inventory_digest" "text") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $_$
BEGIN
    IF p_observed_object_count < 0
       OR p_observed_multipart_count < 0
       OR p_inventory_digest !~ '^[0-9a-f]{64}$' THEN
        RETURN jsonb_build_object('outcome', 'invalid');
    END IF;
    UPDATE public.project_storage_inventory_state
    SET checkpoint = jsonb_build_object('scan_complete', true),
        observed_object_count = p_observed_object_count,
        observed_multipart_count = p_observed_multipart_count,
        inventory_digest = p_inventory_digest,
        verification_object_count = NULL,
        verification_multipart_count = NULL,
        verification_digest = NULL,
        updated_at = now()
    WHERE singleton AND NOT inventory_complete;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('outcome', 'already_complete');
    END IF;
    RETURN jsonb_build_object('outcome', 'finalized');
END;
$_$;


ALTER FUNCTION "public"."finalize_project_storage_inventory_scan"("p_observed_object_count" bigint, "p_observed_multipart_count" bigint, "p_inventory_digest" "text") OWNER TO "postgres";

--
-- Name: get_mut_project_write_state("text", "text"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."get_mut_project_write_state"("p_project_id" "text", "p_user_id" "text") RETURNS TABLE("project_id" "text", "project_name" "text", "org_id" "text", "visibility" "text", "role" "text", "can_write" boolean, "root_hash" "text", "head_commit_id" "text")
    LANGUAGE "sql"
    AS $$
 SELECT * FROM public.get_version_project_write_state(p_project_id,p_user_id); $$;


ALTER FUNCTION "public"."get_mut_project_write_state"("p_project_id" "text", "p_user_id" "text") OWNER TO "postgres";

--
-- Name: get_project_create_operation_replay("text", "uuid", "text"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."get_project_create_operation_replay"("p_operation_key" "text", "p_actor_user_id" "uuid", "p_request_hash" "text") RETURNS "jsonb"
    LANGUAGE "plpgsql" STABLE SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $_$
DECLARE
    operation public.project_create_operations%ROWTYPE;
BEGIN
    IF p_operation_key !~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
       OR p_request_hash !~ '^[0-9a-f]{64}$' THEN
        RETURN jsonb_build_object('outcome', 'invalid');
    END IF;

    SELECT * INTO operation
    FROM public.project_create_operations
    WHERE actor_user_id = p_actor_user_id
      AND operation_key = p_operation_key;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('outcome', 'not_found');
    END IF;
    IF operation.request_hash IS DISTINCT FROM p_request_hash THEN
        RETURN jsonb_build_object('outcome', 'conflict');
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM public.org_members member
        WHERE member.org_id = operation.org_id
          AND member.user_id = p_actor_user_id
    ) THEN
        RETURN jsonb_build_object('outcome', 'forbidden');
    END IF;
    IF operation.status = 'deleted'
       OR NOT EXISTS (
           SELECT 1 FROM public.projects project
           WHERE project.id = operation.project_id
       )
       OR EXISTS (
           SELECT 1 FROM public.projects project
           WHERE project.id = operation.project_id
             AND project.lifecycle_status = 'deleting'
       ) THEN
        RETURN jsonb_build_object('outcome', 'gone');
    END IF;
    IF operation.status = 'dead_lettered' THEN
        RETURN jsonb_build_object('outcome', 'dead_lettered');
    END IF;
    IF operation.status <> 'ready' THEN
        RETURN jsonb_build_object(
            'outcome', 'initializing',
            'result_metadata', operation.result_metadata
        );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM public.projects project
        WHERE project.id = operation.project_id
          AND project.lifecycle_status = 'ready'
    ) THEN
        RETURN jsonb_build_object('outcome', 'lifecycle_conflict');
    END IF;

    RETURN jsonb_build_object(
        'outcome', 'replayed',
        'project', operation.project_snapshot,
        'result_metadata', operation.result_metadata
    );
END;
$_$;


ALTER FUNCTION "public"."get_project_create_operation_replay"("p_operation_key" "text", "p_actor_user_id" "uuid", "p_request_hash" "text") OWNER TO "postgres";

--
-- Name: get_version_project_history_refs("text"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."get_version_project_history_refs"("p_project_id" "text") RETURNS TABLE("ref_name" "text", "ref_type" "text", "commit_id" "text")
    LANGUAGE "sql" STABLE SECURITY DEFINER
    SET "search_path" TO ''
    AS $_$
    WITH project_root AS (
        SELECT COALESCE(p.version_root_hash, '') AS root_hash
          FROM public.projects AS p
         WHERE p.id = p_project_id
         LIMIT 1
    ),
    resolved_head AS (
        SELECT COALESCE(
            (
                SELECT s.head_commit_id
                  FROM public.version_scope_state AS s
                  CROSS JOIN project_root AS p
                 WHERE s.project_id = p_project_id
                   AND s.scope_path = ''
                   AND s.scope_hash = p.root_hash
                   AND s.head_commit_id ~ '^[0-9a-f]{40}$'
                 LIMIT 1
            ),
            (
                SELECT v.project_view_commit_id
                  FROM public.version_view_commits AS v
                  CROSS JOIN project_root AS p
                 WHERE v.project_id = p_project_id
                   AND v.project_root_hash = p.root_hash
                   AND v.project_view_commit_id ~ '^[0-9a-f]{40}$'
                 ORDER BY v.created_at DESC, v.id DESC
                 LIMIT 1
            ),
            (
                SELECT s.head_commit_id
                  FROM public.version_scope_state AS s
                 WHERE s.project_id = p_project_id
                   AND s.scope_path = ''
                   AND s.head_commit_id ~ '^[0-9a-f]{40}$'
                 LIMIT 1
            ),
            (
                SELECT c.commit_id
                  FROM public.version_commits AS c
                 WHERE c.project_id = p_project_id
                   AND c.commit_id ~ '^[0-9a-f]{40}$'
                 ORDER BY c.created_at DESC, c.commit_id DESC
                 LIMIT 1
            ),
            ''
        ) AS commit_id
    ),
    snapshot_refs AS (
        SELECT 'refs/heads/main'::TEXT AS ref_name,
               'branch'::TEXT AS ref_type,
               h.commit_id
          FROM resolved_head AS h
         WHERE h.commit_id ~ '^[0-9a-f]{40}$'
        UNION ALL
        SELECT r.ref_name::TEXT,
               r.ref_type::TEXT,
               r.commit_id::TEXT
          FROM public.version_refs AS r
         WHERE r.project_id = p_project_id
           AND r.scope_path = ''
           AND r.ref_name <> 'refs/heads/main'
           AND r.ref_type IN ('branch', 'tag')
           AND r.commit_id ~ '^[0-9a-f]{40}$'
    )
    SELECT s.ref_name, s.ref_type, s.commit_id
      FROM snapshot_refs AS s
     ORDER BY
        CASE WHEN s.ref_name = 'refs/heads/main' THEN 0 ELSE 1 END,
        CASE WHEN s.ref_type = 'branch' THEN 0 ELSE 1 END,
        s.ref_name;
$_$;


ALTER FUNCTION "public"."get_version_project_history_refs"("p_project_id" "text") OWNER TO "postgres";

--
-- Name: get_version_project_write_state("text", "text"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."get_version_project_write_state"("p_project_id" "text", "p_user_id" "text") RETURNS TABLE("project_id" "text", "project_name" "text", "org_id" "text", "visibility" "text", "role" "text", "can_write" boolean, "root_hash" "text", "head_commit_id" "text")
    LANGUAGE "sql" STABLE SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
SELECT
    p.id::text,
    p.name::text,
    p.org_id::text,
    COALESCE(p.visibility, 'private')::text,
    r.effective_role::text,
    (r.effective_role IN ('admin', 'editor')),
    COALESCE(p.version_root_hash, '')::text,
    COALESCE(s.head_commit_id, '')::text
FROM public.projects p
JOIN public.resolve_project_role(p.id, p_user_id::uuid) r ON true
LEFT JOIN public.version_scope_state s
  ON s.project_id = p.id AND s.scope_path = ''
WHERE p.id = p_project_id;
$$;


ALTER FUNCTION "public"."get_version_project_write_state"("p_project_id" "text", "p_user_id" "text") OWNER TO "postgres";

--
-- Name: handle_new_user(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."handle_new_user"() RETURNS "trigger"
    LANGUAGE "plpgsql" SECURITY DEFINER
    AS $$
BEGIN
    INSERT INTO public.profiles (user_id, email, display_name)
    VALUES (
        NEW.id,
        COALESCE(
            NEW.email,
            NEW.raw_user_meta_data ->> 'email',
            ''
        ),
        COALESCE(
            NEW.raw_user_meta_data ->> 'full_name',
            split_part(COALESCE(NEW.email, ''), '@', 1),
            'User'
        )
    )
    ON CONFLICT (user_id) DO NOTHING;

    RETURN NEW;
END;
$$;


ALTER FUNCTION "public"."handle_new_user"() OWNER TO "postgres";

--
-- Name: FUNCTION "handle_new_user"(); Type: COMMENT; Schema: public; Owner: postgres
--

COMMENT ON FUNCTION "public"."handle_new_user"() IS 'Auto-creates a profile record when a new user signs up via any auth method (Email, Google, GitHub, etc.)';


--
-- Name: is_billable_organization_member("text", "uuid"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."is_billable_organization_member"("p_org_id" "text", "p_user_id" "uuid") RETURNS boolean
    LANGUAGE "sql" STABLE SECURITY DEFINER
    SET "search_path" TO ''
    AS $$
    SELECT EXISTS (
        SELECT 1
        FROM public.org_members om
        WHERE om.org_id = p_org_id
          AND om.user_id = p_user_id
          AND (
              om.role IN ('owner', 'member')
              OR EXISTS (
                  SELECT 1
                  FROM public.project_members pm
                  WHERE pm.org_id = om.org_id
                    AND pm.user_id = om.user_id
                    AND pm.role IN ('admin', 'editor')
              )
          )
    );
$$;


ALTER FUNCTION "public"."is_billable_organization_member"("p_org_id" "text", "p_user_id" "uuid") OWNER TO "postgres";

--
-- Name: issue_user_git_http_credential("text", "text", "text", "text", "text", "uuid", "text", "text", "text", "text", "text"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."issue_user_git_http_credential"("p_credential_id" "text", "p_access_surface_id" "text", "p_org_id" "text", "p_project_id" "text", "p_scope_id" "text", "p_user_id" "uuid", "p_grant_mode" "text", "p_key_prefix" "text", "p_key_last4" "text", "p_key_hash" "text", "p_hash_alg" "text") RETURNS "text"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
DECLARE
    resolved_org_id text;
    effective_role text;
    target_max_mode text := 'rw';
    selected_surface_id text;
BEGIN
    IF p_grant_mode NOT IN ('r', 'rw') THEN
        RAISE EXCEPTION 'invalid Git credential mode';
    END IF;
    SELECT role.org_id, role.effective_role
      INTO resolved_org_id, effective_role
    FROM public.resolve_project_role(p_project_id, p_user_id) role;
    IF resolved_org_id IS DISTINCT FROM p_org_id OR effective_role IS NULL THEN
        RAISE EXCEPTION 'Project Git credential authorization denied'
            USING ERRCODE = '42501';
    END IF;
    IF p_grant_mode = 'rw' AND effective_role = 'viewer' THEN
        RAISE EXCEPTION 'Project Git write credential authorization denied'
            USING ERRCODE = '42501';
    END IF;
    IF p_scope_id IS NOT NULL THEN
        SELECT max_mode INTO target_max_mode
        FROM public.repository_scopes
        WHERE id = p_scope_id AND project_id = p_project_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'repository Scope target is invalid';
        END IF;
    END IF;
    IF p_grant_mode = 'rw' AND target_max_mode <> 'rw' THEN
        RAISE EXCEPTION 'Git credential mode exceeds target mode';
    END IF;

    PERFORM public.ensure_repository_target_access_surfaces(
        p_project_id,
        p_scope_id,
        p_user_id,
        p_access_surface_id,
        NULL
    );
    SELECT id INTO selected_surface_id
    FROM public.access_surfaces
    WHERE project_id = p_project_id
      AND org_id = p_org_id
      AND scope_id IS NOT DISTINCT FROM p_scope_id
      AND kind = 'git_remote'
      AND status = 'active'
    ORDER BY created_at ASC
    LIMIT 1;
    IF selected_surface_id IS NULL THEN
        RAISE EXCEPTION 'Git credential Surface is unavailable';
    END IF;

    INSERT INTO public.access_surface_credentials (
        id, org_id, project_id, access_surface_id, user_id,
        credential_type, grant_mode, credential_lifecycle,
        key_prefix, key_last4, key_hash, hash_alg, status, created_by
    ) VALUES (
        p_credential_id, p_org_id, p_project_id, selected_surface_id, p_user_id,
        'git_http_token', p_grant_mode, 'user', p_key_prefix, p_key_last4,
        p_key_hash, p_hash_alg, 'active', p_user_id
    );

    INSERT INTO public.audit_logs (
        action, path, project_id, operator_type, operator_id, status, metadata
    ) VALUES (
        'git_credential.issue', '', p_project_id, 'user', p_user_id::text,
        'success', jsonb_build_object(
            'credential_id', p_credential_id,
            'target', CASE WHEN p_scope_id IS NULL
                THEN jsonb_build_object('kind', 'project_root', 'project_id', p_project_id)
                ELSE jsonb_build_object(
                    'kind', 'scope', 'project_id', p_project_id, 'scope_id', p_scope_id
                )
            END,
            'mode', p_grant_mode,
            'credential_type', 'git_http_token'
        )
    );
    RETURN p_credential_id;
END;
$$;


ALTER FUNCTION "public"."issue_user_git_http_credential"("p_credential_id" "text", "p_access_surface_id" "text", "p_org_id" "text", "p_project_id" "text", "p_scope_id" "text", "p_user_id" "uuid", "p_grant_mode" "text", "p_key_prefix" "text", "p_key_last4" "text", "p_key_hash" "text", "p_hash_alg" "text") OWNER TO "postgres";

--
-- Name: issue_user_git_http_credential_idempotent("text", "text", "text", "text", "text", "text", "text", "uuid", "text", "text", "text", "text", "text"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."issue_user_git_http_credential_idempotent"("p_operation_key" "text", "p_payload_hash" "text", "p_credential_id" "text", "p_access_surface_id" "text", "p_org_id" "text", "p_project_id" "text", "p_scope_id" "text", "p_user_id" "uuid", "p_grant_mode" "text", "p_key_prefix" "text", "p_key_last4" "text", "p_key_hash" "text", "p_hash_alg" "text") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $_$
DECLARE
    existing_operation public.git_credential_issue_operations%ROWTYPE;
BEGIN
    IF p_operation_key !~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
       OR p_payload_hash !~ '^[0-9a-f]{64}$' THEN
        RETURN jsonb_build_object('outcome', 'invalid');
    END IF;

    PERFORM pg_advisory_xact_lock(
        hashtextextended('git-credential:' || p_user_id::text || ':' || p_operation_key, 0)
    );
    SELECT * INTO existing_operation
    FROM public.git_credential_issue_operations
    WHERE actor_user_id = p_user_id AND operation_key = p_operation_key
    FOR UPDATE;

    IF FOUND THEN
        IF existing_operation.payload_hash IS DISTINCT FROM p_payload_hash
           OR existing_operation.credential_hash IS DISTINCT FROM p_key_hash THEN
            RETURN jsonb_build_object('outcome', 'conflict');
        END IF;
        IF existing_operation.status <> 'active'
           OR NOT EXISTS (
               SELECT 1 FROM public.projects p
               WHERE p.id = existing_operation.project_id
                 AND p.lifecycle_status = 'ready'
           )
           OR NOT EXISTS (
               SELECT 1 FROM public.access_surface_credentials c
               WHERE c.id = existing_operation.credential_id
                 AND c.project_id = existing_operation.project_id
                 AND c.user_id = p_user_id
                 AND c.status = 'active'
                 AND c.key_hash = p_key_hash
           ) THEN
            RETURN jsonb_build_object(
                'outcome', 'gone',
                'credential_id', existing_operation.credential_id
            );
        END IF;
        UPDATE public.git_credential_issue_operations
        SET replayed_at = now()
        WHERE actor_user_id = p_user_id AND operation_key = p_operation_key;
        RETURN jsonb_build_object(
            'outcome', 'replayed',
            'credential_id', existing_operation.credential_id
        );
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM public.projects p
        WHERE p.id = p_project_id
          AND p.org_id = p_org_id
          AND p.lifecycle_status = 'ready'
    ) THEN
        RAISE EXCEPTION 'Project Git credential authorization denied'
            USING ERRCODE = '42501';
    END IF;

    PERFORM public.issue_user_git_http_credential(
        p_credential_id, p_access_surface_id, p_org_id, p_project_id,
        p_scope_id, p_user_id, p_grant_mode, p_key_prefix, p_key_last4,
        p_key_hash, p_hash_alg
    );

    INSERT INTO public.git_credential_issue_operations (
        actor_user_id, operation_key, payload_hash, org_id, project_id,
        credential_id, credential_hash
    ) VALUES (
        p_user_id, p_operation_key, p_payload_hash, p_org_id, p_project_id,
        p_credential_id, p_key_hash
    );

    RETURN jsonb_build_object(
        'outcome', 'created',
        'credential_id', p_credential_id
    );
END;
$_$;


ALTER FUNCTION "public"."issue_user_git_http_credential_idempotent"("p_operation_key" "text", "p_payload_hash" "text", "p_credential_id" "text", "p_access_surface_id" "text", "p_org_id" "text", "p_project_id" "text", "p_scope_id" "text", "p_user_id" "uuid", "p_grant_mode" "text", "p_key_prefix" "text", "p_key_last4" "text", "p_key_hash" "text", "p_hash_alg" "text") OWNER TO "postgres";

--
-- Name: join_project_via_share_token("text", "uuid"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."join_project_via_share_token"("p_share_token" "text", "p_user_id" "uuid") RETURNS TABLE("project_id" "text", "project_name" "text", "role" "text", "newly_joined" boolean)
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
DECLARE
    selected_project public.projects%ROWTYPE;
    existing_role text;
BEGIN
    SELECT * INTO selected_project
    FROM public.projects
    WHERE share_token = p_share_token
      AND lifecycle_status = 'ready'
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM public.org_members
        WHERE org_id = selected_project.org_id AND user_id = p_user_id
    ) THEN
        RAISE EXCEPTION 'share recipient must belong to the organization'
            USING ERRCODE = '42501';
    END IF;

    SELECT pm.role INTO existing_role
    FROM public.project_members pm
    WHERE pm.project_id = selected_project.id AND pm.user_id = p_user_id;
    IF existing_role IS NOT NULL THEN
        RETURN QUERY SELECT selected_project.id, selected_project.name,
            existing_role, false;
        RETURN;
    END IF;

    INSERT INTO public.project_members (
        id, org_id, project_id, user_id, role, granted_by
    ) VALUES (
        gen_random_uuid()::text, selected_project.org_id,
        selected_project.id, p_user_id, 'viewer', p_user_id
    );
    INSERT INTO public.audit_logs (
        action, path, project_id, operator_type, operator_id, status, metadata
    ) VALUES (
        'project_member.share_join', '', selected_project.id, 'user',
        p_user_id::text, 'success', jsonb_build_object('role', 'viewer')
    );
    RETURN QUERY SELECT selected_project.id, selected_project.name,
        'viewer'::text, true;
END;
$$;


ALTER FUNCTION "public"."join_project_via_share_token"("p_share_token" "text", "p_user_id" "uuid") OWNER TO "postgres";

--
-- Name: list_project_deletion_host_tombstones(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."list_project_deletion_host_tombstones"() RETURNS TABLE("project_id" "text")
    LANGUAGE "sql" STABLE SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
    SELECT DISTINCT job.project_id
    FROM public.project_deletion_jobs job
    ORDER BY job.project_id
$$;


ALTER FUNCTION "public"."list_project_deletion_host_tombstones"() OWNER TO "postgres";

--
-- Name: mark_project_storage_orphan_cleaned("text", "text"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."mark_project_storage_orphan_cleaned"("p_project_id" "text", "p_principal" "text") RETURNS boolean
    LANGUAGE "sql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
UPDATE public.project_storage_orphan_prefixes
SET status = 'cleaned', cleaned_at = now()
WHERE project_id = p_project_id
  AND principal = p_principal
  AND status = 'pending'
RETURNING true;
$$;


ALTER FUNCTION "public"."mark_project_storage_orphan_cleaned"("p_project_id" "text", "p_principal" "text") OWNER TO "postgres";

--
-- Name: normalize_scope_path(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."normalize_scope_path"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    AS $$
BEGIN
    NEW.scope_path := TRIM(BOTH '/' FROM COALESCE(NEW.scope_path, ''));
    RETURN NEW;
END;
$$;


ALTER FUNCTION "public"."normalize_scope_path"() OWNER TO "postgres";

--
-- Name: oauth_states_purge_expired(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."oauth_states_purge_expired"() RETURNS bigint
    LANGUAGE "sql"
    AS $$
    WITH deleted AS (
        DELETE FROM public.oauth_states
         WHERE expires_at < now()
         RETURNING 1
    )
    SELECT COUNT(*)::BIGINT FROM deleted;
$$;


ALTER FUNCTION "public"."oauth_states_purge_expired"() OWNER TO "postgres";

--
-- Name: persist_project_deletion_external_ingest_snapshot("text", "text", "jsonb"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."persist_project_deletion_external_ingest_snapshot"("p_job_id" "text", "p_worker_id" "text", "p_external_ingest_resources" "jsonb") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
DECLARE
    job public.project_deletion_jobs%ROWTYPE;
BEGIN
    SELECT * INTO job
    FROM public.project_deletion_jobs deletion_job
    WHERE deletion_job.id = p_job_id
    FOR UPDATE;
    IF NOT FOUND
       OR job.status <> 'running'
       OR job.phase <> 'drain'
       OR job.claimed_by IS DISTINCT FROM p_worker_id THEN
        RETURN jsonb_build_object('outcome', 'claim_lost');
    END IF;
    IF NOT public._project_deletion_external_ingest_snapshot_valid(
        job.project_id, p_external_ingest_resources
    ) THEN
        RETURN jsonb_build_object('outcome', 'invalid');
    END IF;
    IF job.external_ingest_resources IS NOT NULL THEN
        IF job.external_ingest_resources = p_external_ingest_resources THEN
            RETURN jsonb_build_object('outcome', 'replayed');
        END IF;
        RETURN jsonb_build_object('outcome', 'conflict');
    END IF;
    UPDATE public.project_deletion_jobs
    SET external_ingest_resources = p_external_ingest_resources,
        external_ingest_snapshot_at = now(),
        updated_at = now()
    WHERE id = p_job_id;
    RETURN jsonb_build_object('outcome', 'persisted');
END;
$$;


ALTER FUNCTION "public"."persist_project_deletion_external_ingest_snapshot"("p_job_id" "text", "p_worker_id" "text", "p_external_ingest_resources" "jsonb") OWNER TO "postgres";

--
-- Name: project_creator_authorization_preflight(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."project_creator_authorization_preflight"() RETURNS "jsonb"
    LANGUAGE "sql" STABLE
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
SELECT jsonb_build_object(
    'creator_missing_profile', (
        SELECT count(*)
        FROM public.projects p
        LEFT JOIN public.profiles pr ON pr.user_id = p.created_by
        WHERE p.created_by IS NOT NULL AND pr.user_id IS NULL
    ),
    'creator_missing_org_membership', (
        SELECT count(*)
        FROM public.projects p
        JOIN public.profiles pr ON pr.user_id = p.created_by
        LEFT JOIN public.org_members om
          ON om.org_id = p.org_id AND om.user_id = p.created_by
        WHERE p.created_by IS NOT NULL AND om.id IS NULL
    ),
    'creator_missing_project_membership', (
        SELECT count(*)
        FROM public.projects p
        JOIN public.org_members om
          ON om.org_id = p.org_id AND om.user_id = p.created_by
        LEFT JOIN public.project_members pm
          ON pm.project_id = p.id AND pm.user_id = p.created_by
        WHERE p.created_by IS NOT NULL AND pm.id IS NULL
    ),
    'creator_non_admin_project_membership', (
        SELECT count(*)
        FROM public.projects p
        JOIN public.org_members om
          ON om.org_id = p.org_id AND om.user_id = p.created_by
        JOIN public.project_members pm
          ON pm.project_id = p.id AND pm.user_id = p.created_by
        WHERE p.created_by IS NOT NULL AND pm.role IS DISTINCT FROM 'admin'
    )
);
$$;


ALTER FUNCTION "public"."project_creator_authorization_preflight"() OWNER TO "postgres";

--
-- Name: project_storage_inventory_status(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."project_storage_inventory_status"() RETURNS "jsonb"
    LANGUAGE "sql" STABLE SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
    SELECT jsonb_build_object(
        'inventory_complete', state.inventory_complete,
        'checkpoint', state.checkpoint,
        'pending_orphans', COALESCE(
            (
                SELECT jsonb_agg(
                    jsonb_build_object(
                        'project_id', orphan.project_id,
                        'principal', orphan.principal
                    )
                    ORDER BY orphan.project_id, orphan.principal
                )
                FROM public.project_storage_orphan_prefixes orphan
                WHERE orphan.status = 'pending'
            ),
            '[]'::jsonb
        )
    )
    FROM public.project_storage_inventory_state state
    WHERE state.singleton;
$$;


ALTER FUNCTION "public"."project_storage_inventory_status"() OWNER TO "postgres";

--
-- Name: publish_mut_project_update("text", "text", "text", "text", "text", "text", "text", "jsonb", "jsonb", "text", "text", "jsonb", "text", "text", "text", "text", "text", "text", "text", "text", "text", "text"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."publish_mut_project_update"("p_project_id" "text", "p_old_root_hash" "text", "p_new_root_hash" "text", "p_head_commit_id" "text", "p_who" "text", "p_message" "text", "p_event_type" "text", "p_changes" "jsonb", "p_conflicts" "jsonb", "p_created_at" "text", "p_audit_agent_id" "text", "p_audit_detail" "jsonb", "p_source_channel" "text" DEFAULT ''::"text", "p_policy" "text" DEFAULT ''::"text", "p_base_commit_id" "text" DEFAULT ''::"text", "p_client_commit_id" "text" DEFAULT ''::"text", "p_proposed_tree_id" "text" DEFAULT ''::"text", "p_intent_type" "text" DEFAULT 'operation'::"text", "p_scope_path" "text" DEFAULT ''::"text", "p_scope_hash" "text" DEFAULT ''::"text", "p_scope_head_commit_id" "text" DEFAULT ''::"text", "p_expected_scope_head_commit_id" "text" DEFAULT NULL::"text") RETURNS TABLE("published" boolean, "txn_id" bigint)
    LANGUAGE "sql"
    AS $$
 SELECT * FROM public.publish_version_project_update(
  p_project_id,p_old_root_hash,p_new_root_hash,p_head_commit_id,p_who,p_message,
  p_event_type,p_changes,p_conflicts,p_created_at,p_audit_agent_id,p_audit_detail,
  p_source_channel,p_policy,p_base_commit_id,p_client_commit_id,p_proposed_tree_id,
  p_intent_type,p_scope_path,p_scope_hash,p_scope_head_commit_id,
  p_expected_scope_head_commit_id);
$$;


ALTER FUNCTION "public"."publish_mut_project_update"("p_project_id" "text", "p_old_root_hash" "text", "p_new_root_hash" "text", "p_head_commit_id" "text", "p_who" "text", "p_message" "text", "p_event_type" "text", "p_changes" "jsonb", "p_conflicts" "jsonb", "p_created_at" "text", "p_audit_agent_id" "text", "p_audit_detail" "jsonb", "p_source_channel" "text", "p_policy" "text", "p_base_commit_id" "text", "p_client_commit_id" "text", "p_proposed_tree_id" "text", "p_intent_type" "text", "p_scope_path" "text", "p_scope_hash" "text", "p_scope_head_commit_id" "text", "p_expected_scope_head_commit_id" "text") OWNER TO "postgres";

--
-- Name: publish_organization_entitlement("text", "text", "text", "text", "text", "jsonb", integer, "text", bigint, timestamp with time zone, timestamp with time zone, timestamp with time zone, "text", "text", "text"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."publish_organization_entitlement"("p_org_id" "text", "p_schema_version" "text", "p_plan_id" "text", "p_status" "text", "p_source" "text", "p_entitlements" "jsonb", "p_seat_quantity" integer, "p_catalog_version" "text", "p_source_revision" bigint, "p_effective_at" timestamp with time zone, "p_effective_until" timestamp with time zone, "p_current_period_end" timestamp with time zone, "p_payload_hash" "text", "p_source_event_id" "text" DEFAULT NULL::"text", "p_event_type" "text" DEFAULT 'entitlement.published'::"text") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $_$
DECLARE
    current_row public.organization_entitlements%ROWTYPE;
    stored_row public.organization_entitlements%ROWTYPE;
    outcome text;
BEGIN
    IF NULLIF(btrim(p_org_id), '') IS NULL
       OR NULLIF(btrim(p_plan_id), '') IS NULL
       OR NULLIF(btrim(p_status), '') IS NULL THEN
        RAISE EXCEPTION 'organization, plan, and status are required'
            USING ERRCODE = '22023';
    END IF;
    IF p_source IS DISTINCT FROM 'puppypay' THEN
        RAISE EXCEPTION 'entitlement source must be puppypay'
            USING ERRCODE = '22023';
    END IF;
    IF NULLIF(btrim(p_catalog_version), '') IS NULL
       OR p_catalog_version = 'legacy'
       OR p_effective_at IS NULL THEN
        RAISE EXCEPTION 'catalog_version and effective_at are required'
            USING ERRCODE = '22023';
    END IF;
    IF p_source_revision <= 0 THEN
        RAISE EXCEPTION 'source_revision must be positive'
            USING ERRCODE = '22023';
    END IF;
    IF p_seat_quantity < 0 THEN
        RAISE EXCEPTION 'seat_quantity must be nonnegative'
            USING ERRCODE = '22023';
    END IF;
    IF p_schema_version IS NULL OR p_schema_version !~ '^1([.][0-9]+)?$' THEN
        RAISE EXCEPTION 'unsupported entitlement schema_version'
            USING ERRCODE = '22023';
    END IF;
    IF p_payload_hash IS NULL OR p_payload_hash !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'payload_hash must be a lowercase sha256 hex digest'
            USING ERRCODE = '22023';
    END IF;
    IF jsonb_typeof(COALESCE(p_entitlements, '{}'::jsonb)) <> 'object' THEN
        RAISE EXCEPTION 'entitlements must be a JSON object'
            USING ERRCODE = '22023';
    END IF;

    -- FOR UPDATE cannot lock an absent row. A transaction-scoped advisory
    -- lock serializes the first publication for an organization as well, so a
    -- concurrent exact replay is acknowledged rather than surfacing a false
    -- revision race.
    PERFORM pg_advisory_xact_lock(hashtextextended(p_org_id, 0));

    SELECT * INTO current_row
    FROM public.organization_entitlements
    WHERE org_id = p_org_id
    FOR UPDATE;

    IF FOUND AND p_source_revision < current_row.source_revision THEN
        RAISE EXCEPTION 'stale entitlement revision: incoming %, stored %',
            p_source_revision, current_row.source_revision
            USING ERRCODE = '40001';
    END IF;

    IF FOUND AND p_source_revision = current_row.source_revision THEN
        IF p_payload_hash <> current_row.payload_hash THEN
            RAISE EXCEPTION 'entitlement revision conflict for org % revision %',
                p_org_id, p_source_revision
                USING ERRCODE = '23505';
        END IF;
        RETURN jsonb_build_object(
            'outcome', 'idempotent',
            'source_revision', current_row.source_revision,
            'payload_hash', current_row.payload_hash,
            'snapshot', to_jsonb(current_row)
        );
    END IF;

    INSERT INTO public.organization_entitlements (
        org_id,
        schema_version,
        plan_id,
        status,
        source,
        entitlements,
        seat_quantity,
        catalog_version,
        source_revision,
        effective_at,
        effective_until,
        current_period_end,
        payload_hash
    )
    VALUES (
        p_org_id,
        p_schema_version,
        p_plan_id,
        p_status,
        p_source,
        COALESCE(p_entitlements, '{}'::jsonb),
        p_seat_quantity,
        p_catalog_version,
        p_source_revision,
        p_effective_at,
        p_effective_until,
        p_current_period_end,
        p_payload_hash
    )
    ON CONFLICT (org_id) DO UPDATE SET
        schema_version = EXCLUDED.schema_version,
        plan_id = EXCLUDED.plan_id,
        status = EXCLUDED.status,
        source = EXCLUDED.source,
        entitlements = EXCLUDED.entitlements,
        seat_quantity = EXCLUDED.seat_quantity,
        catalog_version = EXCLUDED.catalog_version,
        source_revision = EXCLUDED.source_revision,
        effective_at = EXCLUDED.effective_at,
        effective_until = EXCLUDED.effective_until,
        current_period_end = EXCLUDED.current_period_end,
        payload_hash = EXCLUDED.payload_hash
    WHERE public.organization_entitlements.source_revision < EXCLUDED.source_revision
    RETURNING * INTO stored_row;

    IF stored_row.org_id IS NULL THEN
        RAISE EXCEPTION 'entitlement publication lost a concurrent revision race'
            USING ERRCODE = '40001';
    END IF;

    outcome := CASE WHEN current_row.org_id IS NULL THEN 'inserted' ELSE 'updated' END;

    INSERT INTO public.organization_entitlement_events (
        org_id,
        source,
        source_event_id,
        event_type,
        old_plan_id,
        new_plan_id,
        old_entitlements,
        new_entitlements,
        schema_version,
        catalog_version,
        source_revision,
        seat_quantity,
        payload_hash,
        publication_outcome
    )
    VALUES (
        p_org_id,
        p_source,
        p_source_event_id,
        p_event_type,
        current_row.plan_id,
        stored_row.plan_id,
        current_row.entitlements,
        stored_row.entitlements,
        p_schema_version,
        p_catalog_version,
        p_source_revision,
        p_seat_quantity,
        p_payload_hash,
        outcome
    )
    ON CONFLICT (source_event_id) WHERE source_event_id IS NOT NULL DO NOTHING;

    RETURN jsonb_build_object(
        'outcome', outcome,
        'source_revision', stored_row.source_revision,
        'payload_hash', stored_row.payload_hash,
        'snapshot', to_jsonb(stored_row)
    );
END;
$_$;


ALTER FUNCTION "public"."publish_organization_entitlement"("p_org_id" "text", "p_schema_version" "text", "p_plan_id" "text", "p_status" "text", "p_source" "text", "p_entitlements" "jsonb", "p_seat_quantity" integer, "p_catalog_version" "text", "p_source_revision" bigint, "p_effective_at" timestamp with time zone, "p_effective_until" timestamp with time zone, "p_current_period_end" timestamp with time zone, "p_payload_hash" "text", "p_source_event_id" "text", "p_event_type" "text") OWNER TO "postgres";

--
-- Name: publish_organization_entitlement_v2("text", "text", "text", "text", "text", "jsonb", integer, "text", bigint, timestamp with time zone, timestamp with time zone, timestamp with time zone, "text", "text", "text", "text"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."publish_organization_entitlement_v2"("p_org_id" "text", "p_schema_version" "text", "p_plan_id" "text", "p_status" "text", "p_source" "text", "p_entitlements" "jsonb", "p_seat_quantity" integer, "p_catalog_version" "text", "p_source_revision" bigint, "p_effective_at" timestamp with time zone, "p_effective_until" timestamp with time zone, "p_current_period_end" timestamp with time zone, "p_payload_hash" "text", "p_source_event_id" "text" DEFAULT NULL::"text", "p_source_quote_id" "text" DEFAULT NULL::"text", "p_event_type" "text" DEFAULT 'entitlement.published'::"text") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $_$
DECLARE
    publication jsonb;
    stored_row public.organization_entitlements%ROWTYPE;
BEGIN
    IF p_source_quote_id IS NOT NULL
       AND (NULLIF(btrim(p_source_quote_id), '') IS NULL OR length(p_source_quote_id) > 255) THEN
        RAISE EXCEPTION 'source_quote_id must be non-empty and at most 255 characters'
            USING ERRCODE = '22023';
    END IF;
    IF p_source_quote_id IS NOT NULL
       AND p_schema_version !~ '^1[.]0*[1-9][0-9]*$' THEN
        RAISE EXCEPTION 'source_quote_id requires entitlement schema_version 1.1 or newer'
            USING ERRCODE = '22023';
    END IF;

    publication := public.publish_organization_entitlement(
        p_org_id,
        p_schema_version,
        p_plan_id,
        p_status,
        p_source,
        p_entitlements,
        p_seat_quantity,
        p_catalog_version,
        p_source_revision,
        p_effective_at,
        p_effective_until,
        p_current_period_end,
        p_payload_hash,
        p_source_event_id,
        p_event_type
    );

    IF p_source_quote_id IS NOT NULL THEN
        UPDATE public.organization_entitlements
        SET source_quote_id = p_source_quote_id
        WHERE org_id = p_org_id
          AND source_revision = p_source_revision
          AND (source_quote_id IS NULL OR source_quote_id = p_source_quote_id)
        RETURNING * INTO stored_row;

        IF stored_row.org_id IS NULL THEN
            RAISE EXCEPTION 'entitlement quote correlation conflict for org % revision %',
                p_org_id, p_source_revision
                USING ERRCODE = '23505';
        END IF;

        UPDATE public.organization_entitlement_events
        SET source_quote_id = p_source_quote_id
        WHERE org_id = p_org_id
          AND source_revision = p_source_revision
          AND (source_quote_id IS NULL OR source_quote_id = p_source_quote_id);
    ELSE
        SELECT * INTO stored_row
        FROM public.organization_entitlements
        WHERE org_id = p_org_id
          AND source_revision = p_source_revision;
    END IF;

    RETURN publication || jsonb_build_object('snapshot', to_jsonb(stored_row));
END;
$_$;


ALTER FUNCTION "public"."publish_organization_entitlement_v2"("p_org_id" "text", "p_schema_version" "text", "p_plan_id" "text", "p_status" "text", "p_source" "text", "p_entitlements" "jsonb", "p_seat_quantity" integer, "p_catalog_version" "text", "p_source_revision" bigint, "p_effective_at" timestamp with time zone, "p_effective_until" timestamp with time zone, "p_current_period_end" timestamp with time zone, "p_payload_hash" "text", "p_source_event_id" "text", "p_source_quote_id" "text", "p_event_type" "text") OWNER TO "postgres";

--
-- Name: publish_version_project_update("text", "text", "text", "text", "text", "text", "text", "jsonb", "jsonb", "text", "text", "jsonb", "text", "text", "text", "text", "text", "text", "text", "text", "text", "text"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."publish_version_project_update"("p_project_id" "text", "p_old_root_hash" "text", "p_new_root_hash" "text", "p_head_commit_id" "text", "p_who" "text", "p_message" "text", "p_event_type" "text", "p_changes" "jsonb", "p_conflicts" "jsonb", "p_created_at" "text", "p_audit_agent_id" "text", "p_audit_detail" "jsonb", "p_source_channel" "text" DEFAULT ''::"text", "p_policy" "text" DEFAULT ''::"text", "p_base_commit_id" "text" DEFAULT ''::"text", "p_client_commit_id" "text" DEFAULT ''::"text", "p_proposed_tree_id" "text" DEFAULT ''::"text", "p_intent_type" "text" DEFAULT 'operation'::"text", "p_scope_path" "text" DEFAULT ''::"text", "p_scope_hash" "text" DEFAULT ''::"text", "p_scope_head_commit_id" "text" DEFAULT ''::"text", "p_expected_scope_head_commit_id" "text" DEFAULT NULL::"text") RETURNS TABLE("published" boolean, "txn_id" bigint)
    LANGUAGE "plpgsql"
    AS $$
DECLARE
    rows_affected INT;
    v_created_at  TIMESTAMPTZ;
    v_txn_id      BIGINT;
    v_scope_path  TEXT;
    v_scope_hash  TEXT;
    v_scope_head_commit_id TEXT;
    v_current_scope_head_commit_id TEXT;
BEGIN
    v_created_at := COALESCE(NULLIF(p_created_at, '')::TIMESTAMPTZ, NOW());
    v_scope_path := TRIM(BOTH '/' FROM COALESCE(p_scope_path, ''));
    v_scope_hash := COALESCE(NULLIF(p_scope_hash, ''), p_new_root_hash);
    v_scope_head_commit_id := COALESCE(
        NULLIF(p_scope_head_commit_id, ''),
        NULLIF(p_client_commit_id, ''),
        p_head_commit_id
    );

    IF v_scope_path <> '' AND p_expected_scope_head_commit_id IS NOT NULL THEN
        SELECT head_commit_id
          INTO v_current_scope_head_commit_id
          FROM public.mut_scope_state
         WHERE project_id = p_project_id
           AND scope_path = v_scope_path
         FOR UPDATE;

        IF NOT FOUND THEN
            IF COALESCE(p_expected_scope_head_commit_id, '') <> '' THEN
                RETURN QUERY SELECT FALSE::BOOLEAN, NULL::BIGINT;
                RETURN;
            END IF;
        ELSIF COALESCE(v_current_scope_head_commit_id, '') <>
              COALESCE(p_expected_scope_head_commit_id, '') THEN
            RETURN QUERY SELECT FALSE::BOOLEAN, NULL::BIGINT;
            RETURN;
        END IF;
    END IF;

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

    IF v_scope_path <> '' THEN
        INSERT INTO public.mut_scope_state
            (project_id, scope_path, scope_hash, head_commit_id)
        VALUES
            (
                p_project_id,
                v_scope_path,
                v_scope_hash,
                v_scope_head_commit_id
            )
        ON CONFLICT (project_id, scope_path) DO UPDATE
           SET scope_hash = EXCLUDED.scope_hash,
               head_commit_id = EXCLUDED.head_commit_id,
               updated_at = NOW();
    END IF;

    INSERT INTO public.mut_commits
        (project_id, commit_id, root_hash, scope_path, scope_hash, who, message, changes, conflicts, created_at)
    VALUES
        (
            p_project_id,
            p_head_commit_id,
            p_new_root_hash,
            v_scope_path,
            v_scope_hash,
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
            v_scope_path,
            COALESCE(NULLIF(p_source_channel, ''), 'papi'),
            COALESCE(p_who, ''),
            COALESCE(NULLIF(p_intent_type, ''), 'operation'),
            'committed',
            COALESCE(p_policy, ''),
            COALESCE(p_base_commit_id, ''),
            COALESCE(p_client_commit_id, ''),
            COALESCE(NULLIF(p_proposed_tree_id, ''), v_scope_hash),
            COALESCE(NULLIF(p_base_commit_id, ''), p_old_root_hash, ''),
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
            v_scope_path,
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
                'scope_path', v_scope_path,
                'scope_hash', v_scope_hash,
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


ALTER FUNCTION "public"."publish_version_project_update"("p_project_id" "text", "p_old_root_hash" "text", "p_new_root_hash" "text", "p_head_commit_id" "text", "p_who" "text", "p_message" "text", "p_event_type" "text", "p_changes" "jsonb", "p_conflicts" "jsonb", "p_created_at" "text", "p_audit_agent_id" "text", "p_audit_detail" "jsonb", "p_source_channel" "text", "p_policy" "text", "p_base_commit_id" "text", "p_client_commit_id" "text", "p_proposed_tree_id" "text", "p_intent_type" "text", "p_scope_path" "text", "p_scope_hash" "text", "p_scope_head_commit_id" "text", "p_expected_scope_head_commit_id" "text") OWNER TO "postgres";

--
-- Name: publish_version_project_update_with_usage("text", "text", "text", "text", "text", "text", "text", "jsonb", "jsonb", "text", "text", "jsonb", "text", "text", "text", "text", "text", "text", "text", "text", "text", "text", "text", bigint, bigint, bigint, boolean, bigint); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."publish_version_project_update_with_usage"("p_project_id" "text", "p_old_root_hash" "text", "p_new_root_hash" "text", "p_head_commit_id" "text", "p_who" "text", "p_message" "text", "p_event_type" "text", "p_changes" "jsonb", "p_conflicts" "jsonb", "p_created_at" "text", "p_audit_agent_id" "text", "p_audit_detail" "jsonb", "p_source_channel" "text" DEFAULT ''::"text", "p_policy" "text" DEFAULT ''::"text", "p_base_commit_id" "text" DEFAULT ''::"text", "p_client_commit_id" "text" DEFAULT ''::"text", "p_proposed_tree_id" "text" DEFAULT ''::"text", "p_intent_type" "text" DEFAULT 'operation'::"text", "p_scope_path" "text" DEFAULT ''::"text", "p_scope_hash" "text" DEFAULT ''::"text", "p_scope_head_commit_id" "text" DEFAULT ''::"text", "p_expected_scope_head_commit_id" "text" DEFAULT NULL::"text", "p_org_id" "text" DEFAULT NULL::"text", "p_storage_old_value" bigint DEFAULT 0, "p_storage_delta" bigint DEFAULT 0, "p_storage_limit" bigint DEFAULT NULL::bigint, "p_storage_enforce" boolean DEFAULT false, "p_entitlement_source_revision" bigint DEFAULT NULL::bigint) RETURNS TABLE("published" boolean, "txn_id" bigint)
    LANGUAGE "plpgsql"
    AS $_$
DECLARE
    actual_org_id text;
    current_value bigint;
    new_value bigint;
    effective_storage_limit bigint;
    entitlement_limit jsonb;
    entitlement_limit_text text;
    entitlement_row public.organization_entitlements%ROWTYPE;
    projection_error text;
    limit_source text := 'entitlement_projection';
    publish_row record;
BEGIN
    SELECT org_id INTO actual_org_id
    FROM public.projects
    WHERE id = p_project_id;
    IF actual_org_id IS NULL OR actual_org_id IS DISTINCT FROM p_org_id THEN
        RAISE EXCEPTION 'storage_billing_context_mismatch'
            USING ERRCODE = '22023';
    END IF;
    IF p_storage_old_value < 0 OR (p_storage_limit IS NOT NULL AND p_storage_limit < 0)
       OR (p_entitlement_source_revision IS NOT NULL
           AND p_entitlement_source_revision <= 0) THEN
        RAISE EXCEPTION 'invalid_storage_usage_input'
            USING ERRCODE = '22023';
    END IF;

    -- Serialize with entitlement publication first, then with other storage
    -- commits. The application-provided limit is diagnostic only: required
    -- enforcement is always derived from the current, acknowledged PuppyPay
    -- projection and pinned to the revision used for the file-size preflight.
    PERFORM pg_advisory_xact_lock(hashtextextended(p_org_id, 0));
    PERFORM pg_advisory_xact_lock(
        hashtextextended(p_org_id || ':storage.logical_bytes', 0)
    );

    -- Run the canonical project CAS before quota evaluation. A retry whose
    -- old root is already stale now returns the normal `published = false`
    -- result instead of applying its delta to the post-commit counter and
    -- surfacing a false quota denial. Any later quota exception still rolls
    -- the publication back because both calls share this SQL transaction.
    SELECT * INTO publish_row
    FROM public.publish_version_project_update(
        p_project_id,
        p_old_root_hash,
        p_new_root_hash,
        p_head_commit_id,
        p_who,
        p_message,
        p_event_type,
        p_changes,
        p_conflicts,
        p_created_at,
        p_audit_agent_id,
        p_audit_detail,
        p_source_channel,
        p_policy,
        p_base_commit_id,
        p_client_commit_id,
        p_proposed_tree_id,
        p_intent_type,
        p_scope_path,
        p_scope_hash,
        p_scope_head_commit_id,
        p_expected_scope_head_commit_id
    );
    IF NOT COALESCE(publish_row.published, false) THEN
        RETURN QUERY SELECT false, NULL::bigint;
        RETURN;
    END IF;

    SELECT * INTO entitlement_row
    FROM public.organization_entitlements
    WHERE org_id = p_org_id;
    IF NOT FOUND THEN
        projection_error := 'storage_billing_entitlement_unavailable';
    ELSIF entitlement_row.source IS DISTINCT FROM 'puppypay'
       OR entitlement_row.source_revision <= 0
       OR entitlement_row.payload_hash !~ '^[0-9a-f]{64}$'
       OR entitlement_row.schema_version !~ '^1([.][0-9]+)?$'
       OR (entitlement_row.effective_until IS NOT NULL
           AND entitlement_row.effective_until <= now())
       OR jsonb_typeof(entitlement_row.entitlements) IS DISTINCT FROM 'object'
       OR jsonb_typeof(entitlement_row.entitlements -> 'limits')
           IS DISTINCT FROM 'object' THEN
        projection_error := 'storage_billing_entitlement_invalid';
    ELSE
        entitlement_limit := entitlement_row.entitlements
            -> 'limits' -> 'storage.max_bytes';
        IF entitlement_limit IS NULL THEN
            projection_error := 'storage_billing_entitlement_invalid';
        ELSIF jsonb_typeof(entitlement_limit) = 'null' THEN
            effective_storage_limit := NULL;
        ELSIF jsonb_typeof(entitlement_limit) = 'number' THEN
            entitlement_limit_text := entitlement_limit #>> '{}';
            IF entitlement_limit_text !~ '^[0-9]+$'
               OR entitlement_limit_text::numeric > 9223372036854775807::numeric THEN
                projection_error := 'storage_billing_entitlement_invalid';
            ELSE
                effective_storage_limit := entitlement_limit_text::bigint;
            END IF;
        ELSE
            projection_error := 'storage_billing_entitlement_invalid';
        END IF;
    END IF;

    IF projection_error IS NOT NULL THEN
        IF p_storage_enforce THEN
            RAISE EXCEPTION '%', projection_error USING ERRCODE = 'P0001';
        END IF;
        -- Shadow rollout must remain non-blocking while still recording what
        -- the application observed. This fallback is never used to enforce.
        effective_storage_limit := p_storage_limit;
        limit_source := 'caller_shadow_fallback';
    ELSIF p_storage_enforce
       AND p_entitlement_source_revision IS DISTINCT FROM entitlement_row.source_revision THEN
        RAISE EXCEPTION 'storage_billing_entitlement_changed:%:%',
            p_entitlement_source_revision, entitlement_row.source_revision
            USING ERRCODE = 'P0001';
    END IF;

    SELECT value INTO current_value
    FROM public.organization_usage_counters
    WHERE org_id = p_org_id AND metric = 'storage.logical_bytes'
    FOR UPDATE;
    current_value := COALESCE(current_value, p_storage_old_value);
    -- Keep the quota preflight on the same overflow-safe arithmetic contract
    -- as adjust_organization_usage_counter. PostgreSQL bigint addition raises
    -- before GREATEST can clamp it, while numeric lets the final cast fail
    -- deterministically when the requested logical size is unrepresentable.
    new_value := GREATEST(
        0::numeric,
        current_value::numeric + p_storage_delta::numeric
    )::bigint;

    IF p_storage_enforce
       AND p_storage_delta > 0
       AND effective_storage_limit IS NOT NULL
       AND new_value > effective_storage_limit THEN
        RAISE EXCEPTION 'storage_quota_exceeded:%:%', new_value, effective_storage_limit
            USING ERRCODE = 'P0001';
    END IF;

    PERFORM public.reconcile_organization_usage_counter(
        p_org_id,
        'storage.logical_bytes',
        new_value,
        effective_storage_limit,
        'storage-commit:' || p_project_id || ':' || p_head_commit_id,
        'version_engine',
        jsonb_build_object(
            'project_id', p_project_id,
            'commit_id', p_head_commit_id,
            'delta_bytes', p_storage_delta,
            'limit_source', limit_source,
            'entitlement_source_revision', entitlement_row.source_revision,
            'shadow_would_deny', (
                NOT p_storage_enforce
                AND p_storage_delta > 0
                AND effective_storage_limit IS NOT NULL
                AND new_value > effective_storage_limit
            )
        )
    );
    RETURN QUERY SELECT true, publish_row.txn_id::bigint;
END;
$_$;


ALTER FUNCTION "public"."publish_version_project_update_with_usage"("p_project_id" "text", "p_old_root_hash" "text", "p_new_root_hash" "text", "p_head_commit_id" "text", "p_who" "text", "p_message" "text", "p_event_type" "text", "p_changes" "jsonb", "p_conflicts" "jsonb", "p_created_at" "text", "p_audit_agent_id" "text", "p_audit_detail" "jsonb", "p_source_channel" "text", "p_policy" "text", "p_base_commit_id" "text", "p_client_commit_id" "text", "p_proposed_tree_id" "text", "p_intent_type" "text", "p_scope_path" "text", "p_scope_hash" "text", "p_scope_head_commit_id" "text", "p_expected_scope_head_commit_id" "text", "p_org_id" "text", "p_storage_old_value" bigint, "p_storage_delta" bigint, "p_storage_limit" bigint, "p_storage_enforce" boolean, "p_entitlement_source_revision" bigint) OWNER TO "postgres";

--
-- Name: reconcile_billing_operation_from_entitlement("text", "text"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."reconcile_billing_operation_from_entitlement"("p_org_id" "text", "p_operation_id" "text") RETURNS SETOF "public"."organization_billing_operations"
    LANGUAGE "sql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
    UPDATE public.organization_billing_operations AS operation
    SET status = 'confirmed',
        confirmed_revision = entitlement.source_revision,
        completed_at = COALESCE(operation.completed_at, now()),
        last_error = NULL
    FROM public.organization_entitlements AS entitlement
    WHERE operation.id = p_operation_id
      AND operation.org_id = p_org_id
      AND entitlement.org_id = operation.org_id
      AND entitlement.source_quote_id = operation.quote_id
      AND operation.kind IN (
          'checkout', 'plan_change', 'seat_increase', 'seat_decrease',
          'member_activation', 'member_deactivation'
      )
      AND operation.status IN (
          'pending', 'quoted', 'awaiting_confirmation', 'submitted'
      )
      AND operation.target_plan_id = entitlement.plan_id
      AND operation.target_seat_quantity = entitlement.seat_quantity
      AND operation.baseline_source_revision IS NOT NULL
      AND operation.baseline_source_revision < entitlement.source_revision
    RETURNING operation.*;
$$;


ALTER FUNCTION "public"."reconcile_billing_operation_from_entitlement"("p_org_id" "text", "p_operation_id" "text") OWNER TO "postgres";

--
-- Name: reconcile_organization_usage_counter("text", "text", bigint, bigint, "text", "text", "jsonb"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."reconcile_organization_usage_counter"("p_org_id" "text", "p_metric" "text", "p_value" bigint, "p_limit" bigint, "p_idempotency_key" "text", "p_source" "text", "p_metadata" "jsonb" DEFAULT '{}'::"jsonb") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
DECLARE
    existing_event public.organization_usage_events%ROWTYPE;
    current_value bigint := 0;
    counter_row public.organization_usage_counters%ROWTYPE;
    threshold integer := 0;
    previous_threshold integer := 0;
BEGIN
    IF p_metric <> 'storage.logical_bytes' OR p_value < 0 THEN
        RAISE EXCEPTION 'invalid absolute usage measurement'
            USING ERRCODE = '22023';
    END IF;
    IF p_limit IS NOT NULL AND p_limit < 0 THEN
        RAISE EXCEPTION 'usage limit must be nonnegative when supplied'
            USING ERRCODE = '22023';
    END IF;
    IF p_idempotency_key IS NULL OR length(p_idempotency_key) < 8 THEN
        RAISE EXCEPTION 'usage idempotency key is required'
            USING ERRCODE = '22023';
    END IF;

    PERFORM pg_advisory_xact_lock(hashtextextended(p_org_id || ':' || p_metric, 0));
    SELECT * INTO existing_event
    FROM public.organization_usage_events
    WHERE org_id = p_org_id
      AND metric = p_metric
      AND idempotency_key = p_idempotency_key;
    IF FOUND THEN
        IF existing_event.source IS DISTINCT FROM p_source
           OR existing_event.metadata -> 'requested_value'
                IS DISTINCT FROM to_jsonb(p_value)
           OR existing_event.metadata -> 'requested_limit'
                IS DISTINCT FROM COALESCE(to_jsonb(p_limit), 'null'::jsonb) THEN
            RAISE EXCEPTION 'usage_idempotency_payload_mismatch'
                USING ERRCODE = '23505';
        END IF;
        IF p_source = 'storage_reconciler' THEN
            UPDATE public.organization_usage_counters
            SET reconciled_at = now(),
                full_reconciled_at = now(),
                reconciliation_claimed_at = NULL
            WHERE org_id = p_org_id AND metric = p_metric;
        END IF;
        RETURN jsonb_build_object(
            'outcome', 'idempotent',
            'value', existing_event.value_after
        );
    END IF;

    SELECT value, threshold_percent INTO current_value, previous_threshold
    FROM public.organization_usage_counters
    WHERE org_id = p_org_id AND metric = p_metric
    FOR UPDATE;
    current_value := COALESCE(current_value, 0);
    previous_threshold := COALESCE(previous_threshold, 0);
    IF p_limit IS NOT NULL THEN
        threshold := CASE
            WHEN p_limit = 0 AND p_value = 0 THEN 0
            WHEN p_limit = 0 THEN 100
            WHEN p_value >= p_limit THEN 100
            WHEN p_value::numeric * 100 >= p_limit::numeric * 95 THEN 95
            WHEN p_value::numeric * 100 >= p_limit::numeric * 80 THEN 80
            ELSE 0
        END;
    END IF;

    INSERT INTO public.organization_usage_counters (
        org_id, metric, value, version, threshold_percent, reconciled_at,
        full_reconciled_at
    )
    VALUES (
        p_org_id, p_metric, p_value, 1, threshold, now(),
        CASE WHEN p_source = 'storage_reconciler' THEN now() ELSE NULL END
    )
    ON CONFLICT (org_id, metric) DO UPDATE SET
        value = EXCLUDED.value,
        version = public.organization_usage_counters.version + 1,
        threshold_percent = EXCLUDED.threshold_percent,
        reconciled_at = now(),
        full_reconciled_at = CASE
            WHEN p_source = 'storage_reconciler' THEN now()
            ELSE public.organization_usage_counters.full_reconciled_at
        END,
        reconciliation_claimed_at = CASE
            WHEN p_source = 'storage_reconciler' THEN NULL
            ELSE public.organization_usage_counters.reconciliation_claimed_at
        END
    RETURNING * INTO counter_row;

    INSERT INTO public.organization_usage_events (
        org_id, metric, idempotency_key, delta, value_after, source, metadata
    ) VALUES (
        p_org_id, p_metric, p_idempotency_key, p_value - current_value,
        p_value, p_source,
        COALESCE(p_metadata, '{}'::jsonb) || jsonb_build_object(
            'requested_value', p_value,
            'requested_limit', p_limit,
            'previous_threshold_percent', previous_threshold,
            'threshold_percent', threshold,
            'threshold_changed', previous_threshold IS DISTINCT FROM threshold
        )
    );

    RETURN jsonb_build_object(
        'outcome', 'reconciled',
        'value', counter_row.value,
        'version', counter_row.version,
        'threshold_percent', counter_row.threshold_percent,
        'previous_threshold_percent', previous_threshold,
        'threshold_changed', previous_threshold IS DISTINCT FROM counter_row.threshold_percent
    );
END;
$$;


ALTER FUNCTION "public"."reconcile_organization_usage_counter"("p_org_id" "text", "p_metric" "text", "p_value" bigint, "p_limit" bigint, "p_idempotency_key" "text", "p_source" "text", "p_metadata" "jsonb") OWNER TO "postgres";

--
-- Name: record_project_storage_inventory_batch("text", "jsonb", "jsonb", integer, integer); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."record_project_storage_inventory_batch"("p_batch_key" "text", "p_principals" "jsonb", "p_checkpoint" "jsonb", "p_observed_object_count" integer, "p_observed_multipart_count" integer) RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $_$
DECLARE
    inserted_batch boolean;
    inserted_principals bigint;
BEGIN
    IF p_batch_key !~ '^[0-9a-f]{64}$'
       OR jsonb_typeof(p_principals) IS DISTINCT FROM 'array'
       OR jsonb_typeof(p_checkpoint) IS DISTINCT FROM 'object'
       OR p_observed_object_count < 0
       OR p_observed_multipart_count < 0
       OR EXISTS (
           SELECT 1
           FROM jsonb_to_recordset(p_principals) pair(project_id text, principal text)
           WHERE pair.project_id !~ '^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$'
              OR pair.principal !~ '^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$'
       ) THEN
        RETURN jsonb_build_object('outcome', 'invalid');
    END IF;

    INSERT INTO public.project_storage_inventory_batches (
        batch_key, checkpoint, observed_object_count, observed_multipart_count
    ) VALUES (
        p_batch_key, p_checkpoint, p_observed_object_count,
        p_observed_multipart_count
    ) ON CONFLICT (batch_key) DO NOTHING;
    GET DIAGNOSTICS inserted_principals = ROW_COUNT;
    inserted_batch := inserted_principals = 1;
    IF NOT inserted_batch THEN
        RETURN jsonb_build_object('outcome', 'replayed');
    END IF;

    INSERT INTO public.project_storage_principals (project_id, principal)
    SELECT DISTINCT pair.project_id, pair.principal
    FROM jsonb_to_recordset(p_principals) pair(project_id text, principal text)
    JOIN public.projects project ON project.id = pair.project_id
    ON CONFLICT (project_id, principal) DO NOTHING;
    GET DIAGNOSTICS inserted_principals = ROW_COUNT;

    INSERT INTO public.project_storage_orphan_prefixes (project_id, principal)
    SELECT DISTINCT pair.project_id, pair.principal
    FROM jsonb_to_recordset(p_principals) pair(project_id text, principal text)
    LEFT JOIN public.projects project ON project.id = pair.project_id
    WHERE project.id IS NULL
    ON CONFLICT (project_id, principal) DO UPDATE
      SET status = 'pending', cleaned_at = NULL;

    UPDATE public.project_storage_inventory_state
    SET checkpoint = p_checkpoint,
        updated_at = now()
    WHERE singleton;
    RETURN jsonb_build_object(
        'outcome', 'recorded',
        'inserted_principals', inserted_principals
    );
END;
$_$;


ALTER FUNCTION "public"."record_project_storage_inventory_batch"("p_batch_key" "text", "p_principals" "jsonb", "p_checkpoint" "jsonb", "p_observed_object_count" integer, "p_observed_multipart_count" integer) OWNER TO "postgres";

--
-- Name: register_version_object_gc_candidates("text", "text"[], timestamp with time zone); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."register_version_object_gc_candidates"("p_project_id" "text", "p_object_ids" "text"[], "p_now" timestamp with time zone) RETURNS "void"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'public', 'pg_temp'
    AS $$
BEGIN
    INSERT INTO public.version_object_gc_candidates (
        project_id, object_id, first_seen_at, last_seen_at
    )
    SELECT p_project_id, object_id, p_now, p_now
    FROM unnest(COALESCE(p_object_ids, ARRAY[]::text[])) AS object_id
    ON CONFLICT (project_id, object_id) DO UPDATE
    SET last_seen_at = EXCLUDED.last_seen_at;
END;
$$;


ALTER FUNCTION "public"."register_version_object_gc_candidates"("p_project_id" "text", "p_object_ids" "text"[], "p_now" timestamp with time zone) OWNER TO "postgres";

--
-- Name: release_project_write_lease("uuid", "text"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."release_project_write_lease"("p_lease_id" "uuid", "p_holder_id" "text") RETURNS boolean
    LANGUAGE "sql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
DELETE FROM public.project_write_leases
WHERE id = p_lease_id AND holder_id = p_holder_id
RETURNING true;
$$;


ALTER FUNCTION "public"."release_project_write_lease"("p_lease_id" "uuid", "p_holder_id" "text") OWNER TO "postgres";

--
-- Name: remove_project_member_authorized("text", "uuid", "uuid"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."remove_project_member_authorized"("p_project_id" "text", "p_target_user_id" "uuid", "p_actor_user_id" "uuid") RETURNS boolean
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
DECLARE
    actor_role text;
    removed_count integer;
    previous_role text;
BEGIN
    SELECT r.effective_role INTO actor_role
    FROM public.resolve_project_role(p_project_id, p_actor_user_id) r;
    IF actor_role IS DISTINCT FROM 'admin' THEN
        RAISE EXCEPTION 'project member management denied'
            USING ERRCODE = '42501';
    END IF;

    SELECT role INTO previous_role
    FROM public.project_members
    WHERE project_id = p_project_id AND user_id = p_target_user_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN false;
    END IF;

    DELETE FROM public.project_members
    WHERE project_id = p_project_id AND user_id = p_target_user_id;
    GET DIAGNOSTICS removed_count = ROW_COUNT;

    INSERT INTO public.audit_logs (
        action, path, project_id, operator_type, operator_id, status, metadata
    ) VALUES (
        'project_member.remove', '', p_project_id, 'user',
        p_actor_user_id::text, 'success',
        jsonb_build_object(
            'target_user_id', p_target_user_id,
            'previous_role', previous_role
        )
    );
    RETURN removed_count > 0;
END;
$$;


ALTER FUNCTION "public"."remove_project_member_authorized"("p_project_id" "text", "p_target_user_id" "uuid", "p_actor_user_id" "uuid") OWNER TO "postgres";

--
-- Name: renew_project_write_lease("uuid", "text", integer); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."renew_project_write_lease"("p_lease_id" "uuid", "p_holder_id" "text", "p_ttl_seconds" integer DEFAULT 120) RETURNS boolean
    LANGUAGE "sql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
UPDATE public.project_write_leases
SET renewed_at = now(),
    expires_at = now() + make_interval(
        secs => GREATEST(30, LEAST(COALESCE(p_ttl_seconds, 120), 7200))
    )
WHERE id = p_lease_id
  AND holder_id = p_holder_id
  AND expires_at > now()
RETURNING true;
$$;


ALTER FUNCTION "public"."renew_project_write_lease"("p_lease_id" "uuid", "p_holder_id" "text", "p_ttl_seconds" integer) OWNER TO "postgres";

--
-- Name: replace_mcp_surface_policy("text", "jsonb", "jsonb", "jsonb"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."replace_mcp_surface_policy"("p_surface_id" "text", "p_accesses" "jsonb", "p_tools_policy" "jsonb", "p_bindings" "jsonb") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'public', 'pg_temp'
    AS $$
DECLARE
    v_policy public.access_surface_policies%ROWTYPE;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM public.access_surfaces
        WHERE id = p_surface_id AND kind = 'mcp'
    ) THEN
        RAISE EXCEPTION 'MCP access surface not found';
    END IF;

    INSERT INTO public.access_surface_policies (
        access_surface_id, version, fs_policy, tools_policy,
        shell_policy, network_policy
    ) VALUES (
        p_surface_id,
        1,
        jsonb_build_object('accesses', COALESCE(p_accesses, '[]'::jsonb)),
        COALESCE(p_tools_policy, '{}'::jsonb),
        jsonb_build_object('enabled', false),
        '{}'::jsonb
    )
    ON CONFLICT (access_surface_id) DO UPDATE SET
        version = EXCLUDED.version,
        fs_policy = EXCLUDED.fs_policy,
        tools_policy = EXCLUDED.tools_policy,
        shell_policy = EXCLUDED.shell_policy,
        network_policy = EXCLUDED.network_policy
    RETURNING * INTO v_policy;

    DELETE FROM public.access_tools WHERE access_point_id = p_surface_id;
    INSERT INTO public.access_tools (
        id, access_point_id, tool_id, enabled, mcp_exposed
    )
    SELECT
        gen_random_uuid()::text,
        p_surface_id,
        binding ->> 'tool_id',
        COALESCE((binding ->> 'enabled')::boolean, true),
        true
    FROM jsonb_array_elements(COALESCE(p_bindings, '[]'::jsonb)) binding
    JOIN public.tools t ON t.id = binding ->> 'tool_id'
    WHERE NULLIF(binding ->> 'tool_id', '') IS NOT NULL;

    RETURN to_jsonb(v_policy);
END;
$$;


ALTER FUNCTION "public"."replace_mcp_surface_policy"("p_surface_id" "text", "p_accesses" "jsonb", "p_tools_policy" "jsonb", "p_bindings" "jsonb") OWNER TO "postgres";

--
-- Name: repository_target_integrity_report(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."repository_target_integrity_report"() RETURNS "jsonb"
    LANGUAGE "sql" STABLE
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
SELECT jsonb_build_object(
    'empty_scope_paths', (
        SELECT count(*) FROM public.repository_scopes WHERE path = ''
    ),
    'orphan_surface_targets', (
        SELECT count(*)
        FROM public.access_surfaces s
        LEFT JOIN public.projects p
          ON p.id = s.project_id AND p.org_id = s.org_id
        LEFT JOIN public.repository_scopes rs
          ON rs.id = s.scope_id AND rs.project_id = s.project_id
        WHERE p.id IS NULL OR (s.scope_id IS NOT NULL AND rs.id IS NULL)
    ),
    'credential_target_mismatches', (
        SELECT count(*)
        FROM public.access_surface_credentials c
        LEFT JOIN public.access_surfaces s
          ON s.id = c.access_surface_id
         AND s.project_id = c.project_id
         AND s.org_id = c.org_id
        LEFT JOIN public.org_members om
          ON om.org_id = c.org_id AND om.user_id = c.user_id
        WHERE s.id IS NULL
           OR (c.credential_lifecycle = 'user' AND om.id IS NULL)
    ),
    'duplicate_builtin_targets', (
        SELECT count(*) FROM (
            SELECT project_id, scope_id, kind
            FROM public.access_surfaces
            WHERE kind IN ('git_remote', 'cli', 'filesystem')
            GROUP BY project_id, scope_id, kind
            HAVING count(*) > 1
        ) duplicates
    ),
    'legacy_agent_scope_configs', (
        SELECT count(*)
        FROM public.access_surfaces
        WHERE kind = 'agent' AND config ? 'scope'
    ),
    'orphan_scope_dependents', (
        SELECT count(*)
        FROM (
            SELECT c.project_id, c.scope_id
            FROM public.connections c
            LEFT JOIN public.repository_scopes rs
              ON rs.id = c.scope_id AND rs.project_id = c.project_id
            WHERE c.scope_id IS NOT NULL AND rs.id IS NULL
            UNION ALL
            SELECT s.project_id, s.scope_id
            FROM public.scope_sandbox_sessions s
            LEFT JOIN public.repository_scopes rs
              ON rs.id = s.scope_id AND rs.project_id = s.project_id
            WHERE rs.id IS NULL
            UNION ALL
            SELECT e.project_id, e.scope_id
            FROM public.scope_sync_events e
            LEFT JOIN public.repository_scopes rs
              ON rs.id = e.scope_id AND rs.project_id = e.project_id
            WHERE rs.id IS NULL
            UNION ALL
            SELECT s.project_id, s.scope_id
            FROM public.scope_sync_settings s
            LEFT JOIN public.repository_scopes rs
              ON rs.id = s.scope_id AND rs.project_id = s.project_id
            WHERE rs.id IS NULL
        ) invalid_dependents
    )
);
$$;


ALTER FUNCTION "public"."repository_target_integrity_report"() OWNER TO "postgres";

--
-- Name: reserve_billable_member_activation("text", "uuid", "uuid", "text", "text", "text"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."reserve_billable_member_activation"("p_org_id" "text", "p_subject_user_id" "uuid", "p_actor_user_id" "uuid", "p_invitation_id" "text", "p_role" "text", "p_idempotency_key" "text") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    AS $$
DECLARE
    operation_row public.organization_billing_operations%ROWTYPE;
    current_quantity bigint;
    reserved_quantity bigint;
    target_quantity bigint;
    effective_purchased integer;
    effective_revision bigint;
    capacity_confirmed boolean;
BEGIN
    IF COALESCE(p_idempotency_key, '') = '' OR length(p_idempotency_key) > 255 THEN
        RAISE EXCEPTION 'seat admission idempotency key is invalid'
            USING ERRCODE = '22023';
    END IF;

    PERFORM pg_advisory_xact_lock(hashtextextended(p_org_id || ':billable-seats', 0));

    -- The persisted PuppyPay projection is the only seat authority. The RPC
    -- accepts no caller-supplied commercial facts, so forged plan/seat values
    -- cannot bootstrap access when the projection is absent or expired.
    SELECT seat_quantity, source_revision
      INTO effective_purchased, effective_revision
    FROM public.organization_entitlements
    WHERE org_id = p_org_id
      AND (effective_until IS NULL OR effective_until > now());
    IF NOT FOUND THEN
        RAISE EXCEPTION 'an effective entitlement snapshot is required for seat admission'
            USING ERRCODE = 'P0001';
    END IF;

    -- A confirmed reservation is an immediate handoff lease. Expire a lease
    -- left without a capability mutation for fifteen minutes; a normal API
    -- call completes this handoff in the same request.
    UPDATE public.organization_billing_operations candidate
    SET status = 'failed',
        last_error = 'seat_admission_lease_expired',
        attempts = candidate.attempts + 1
    WHERE candidate.org_id = p_org_id
      AND candidate.kind = 'member_activation'
      AND candidate.status = 'confirmed'
      AND candidate.completed_at IS NULL
      AND candidate.updated_at < now() - interval '15 minutes'
      AND candidate.subject_user_id IS NOT NULL
      AND NOT public.is_billable_organization_member(
          candidate.org_id,
          candidate.subject_user_id
      );

    SELECT * INTO operation_row
    FROM public.organization_billing_operations candidate
    WHERE candidate.org_id = p_org_id
      AND candidate.kind = 'member_activation'
      AND candidate.subject_user_id = p_subject_user_id
      AND candidate.completed_at IS NULL
      AND candidate.status IN (
          'awaiting_confirmation', 'quoted', 'submitted', 'confirmed'
      )
    ORDER BY candidate.created_at DESC
    LIMIT 1
    FOR UPDATE;

    IF public.is_billable_organization_member(p_org_id, p_subject_user_id) THEN
        IF operation_row.id IS NULL THEN
            INSERT INTO public.organization_billing_operations (
                org_id, kind, status, idempotency_key, actor_user_id,
                subject_user_id, invitation_id, current_seat_quantity,
                target_seat_quantity, confirmed_revision, request_payload,
                response_payload, completed_at
            ) VALUES (
                p_org_id, 'member_activation', 'confirmed', p_idempotency_key,
                p_actor_user_id, p_subject_user_id, p_invitation_id,
                public.count_billable_organization_members(p_org_id),
                public.count_billable_organization_members(p_org_id),
                effective_revision,
                jsonb_build_object('schema_version', '1.0', 'role', p_role),
                jsonb_build_object('already_billable', true),
                now()
            ) RETURNING * INTO operation_row;
        ELSE
            UPDATE public.organization_billing_operations
            SET status = 'confirmed',
                confirmed_revision = effective_revision,
                completed_at = now(),
                last_error = NULL
            WHERE id = operation_row.id
            RETURNING * INTO operation_row;
        END IF;
        RETURN to_jsonb(operation_row);
    END IF;

    IF operation_row.status = 'confirmed' THEN
        RETURN to_jsonb(operation_row);
    END IF;

    current_quantity := public.count_billable_organization_members(p_org_id);
    SELECT count(DISTINCT candidate.subject_user_id)::bigint
      INTO reserved_quantity
    FROM public.organization_billing_operations candidate
    WHERE candidate.org_id = p_org_id
      AND candidate.kind = 'member_activation'
      AND candidate.status = 'confirmed'
      AND candidate.completed_at IS NULL
      AND candidate.subject_user_id IS NOT NULL
      AND NOT public.is_billable_organization_member(
          candidate.org_id,
          candidate.subject_user_id
      );
    target_quantity := current_quantity + COALESCE(reserved_quantity, 0) + 1;
    capacity_confirmed := target_quantity <= effective_purchased;

    IF operation_row.id IS NULL THEN
        INSERT INTO public.organization_billing_operations (
            org_id, kind, status, idempotency_key, actor_user_id,
            subject_user_id, invitation_id, current_seat_quantity,
            target_seat_quantity, confirmed_revision, request_payload,
            response_payload
        ) VALUES (
            p_org_id,
            'member_activation',
            CASE WHEN capacity_confirmed THEN 'confirmed' ELSE 'awaiting_confirmation' END,
            p_idempotency_key,
            p_actor_user_id,
            p_subject_user_id,
            p_invitation_id,
            current_quantity,
            target_quantity,
            CASE WHEN capacity_confirmed THEN effective_revision ELSE NULL END,
            jsonb_build_object(
                'schema_version', '1.0',
                'role', p_role,
                'purchased_seat_quantity', effective_purchased
            ),
            jsonb_build_object(
                'requires_checkout', NOT capacity_confirmed
            )
        ) RETURNING * INTO operation_row;
    ELSE
        UPDATE public.organization_billing_operations
        SET status = CASE
                WHEN capacity_confirmed THEN 'confirmed'
                ELSE 'awaiting_confirmation'
            END,
            actor_user_id = p_actor_user_id,
            invitation_id = COALESCE(p_invitation_id, invitation_id),
            current_seat_quantity = current_quantity,
            target_seat_quantity = target_quantity,
            confirmed_revision = CASE
                WHEN capacity_confirmed THEN effective_revision
                ELSE NULL
            END,
            request_payload = jsonb_build_object(
                'schema_version', '1.0',
                'role', p_role,
                'purchased_seat_quantity', effective_purchased
            ),
            response_payload = jsonb_build_object(
                'requires_checkout', NOT capacity_confirmed
            ),
            last_error = NULL
        WHERE id = operation_row.id
        RETURNING * INTO operation_row;
    END IF;
    RETURN to_jsonb(operation_row);
END;
$$;


ALTER FUNCTION "public"."reserve_billable_member_activation"("p_org_id" "text", "p_subject_user_id" "uuid", "p_actor_user_id" "uuid", "p_invitation_id" "text", "p_role" "text", "p_idempotency_key" "text") OWNER TO "postgres";

--
-- Name: resolve_git_runtime_credential("text"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."resolve_git_runtime_credential"("p_key_hash" "text") RETURNS TABLE("credential_id" "text", "org_id" "text", "project_id" "text", "access_surface_id" "text", "target_kind" "text", "scope_id" "text", "path_prefix" "text", "excludes" "jsonb", "target_max_mode" "text", "user_id" "uuid", "effective_mode" "text")
    LANGUAGE "sql" STABLE SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
SELECT
    c.id,
    c.org_id,
    c.project_id,
    c.access_surface_id,
    CASE WHEN s.scope_id IS NULL THEN 'project_root' ELSE 'scope' END,
    s.scope_id,
    COALESCE(rs.path, ''),
    COALESCE(rs.exclude, '[]'::jsonb)
      || COALESCE(
          (
              SELECT jsonb_agg(descendant.path ORDER BY descendant.path)
              FROM public.repository_scopes descendant
              WHERE s.scope_id IS NOT NULL
                AND descendant.project_id = s.project_id
                AND descendant.id <> s.scope_id
                AND left(descendant.path, length(rs.path) + 1) = rs.path || '/'
          ),
          '[]'::jsonb
      ),
    COALESCE(rs.max_mode, 'rw'),
    c.user_id,
    CASE
        WHEN c.grant_mode <> 'rw'
          OR COALESCE(rs.max_mode, 'rw') <> 'rw'
          OR COALESCE(s.config ->> 'mode', 'rw') <> 'rw'
          OR (c.credential_lifecycle = 'user' AND role.effective_role = 'viewer')
        THEN 'r'
        ELSE 'rw'
    END
FROM public.access_surface_credentials c
JOIN public.projects p
  ON p.id = c.project_id
 AND p.org_id = c.org_id
 AND p.lifecycle_status = 'ready'
JOIN public.access_surfaces s
  ON s.id = c.access_surface_id
 AND s.project_id = c.project_id
 AND s.org_id = c.org_id
 AND s.kind = 'git_remote'
 AND s.status = 'active'
LEFT JOIN public.repository_scopes rs
  ON rs.id = s.scope_id AND rs.project_id = s.project_id
LEFT JOIN LATERAL public.resolve_project_role(c.project_id, c.user_id) role
  ON c.credential_lifecycle = 'user'
WHERE c.key_hash = p_key_hash
  AND c.credential_type = 'git_http_token'
  AND c.status = 'active'
  AND (c.expires_at IS NULL OR c.expires_at > now())
  AND (s.scope_id IS NULL OR rs.id IS NOT NULL)
  AND (
      c.credential_lifecycle <> 'user'
      OR (
          c.user_id IS NOT NULL
          AND role.org_id = c.org_id
          AND role.effective_role IS NOT NULL
      )
  )
LIMIT 1;
$$;


ALTER FUNCTION "public"."resolve_git_runtime_credential"("p_key_hash" "text") OWNER TO "postgres";

--
-- Name: resolve_project_role("text", "uuid"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."resolve_project_role"("p_project_id" "text", "p_user_id" "uuid") RETURNS TABLE("org_id" "text", "effective_role" "text", "grant_source" "text")
    LANGUAGE "sql" STABLE SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
WITH facts AS (
    SELECT
        p.org_id,
        p.visibility,
        om.role AS org_role,
        pm.role AS project_role,
        pm.org_id AS project_member_org_id
    FROM public.projects p
    JOIN public.org_members om
      ON om.org_id = p.org_id AND om.user_id = p_user_id
    LEFT JOIN public.project_members pm
      ON pm.project_id = p.id
     AND pm.org_id = p.org_id
     AND pm.user_id = p_user_id
    WHERE p.id = p_project_id
      AND p.lifecycle_status = 'ready'
), resolved AS (
    SELECT
        org_id,
        CASE
            WHEN org_role = 'owner' THEN 'admin'
            WHEN project_role IN ('admin', 'editor', 'viewer')
             AND project_member_org_id = org_id THEN project_role
            WHEN visibility = 'org' THEN 'viewer'
            ELSE NULL
        END AS effective_role,
        CASE
            WHEN org_role = 'owner' THEN 'org_owner'
            WHEN project_role IN ('admin', 'editor', 'viewer')
             AND project_member_org_id = org_id THEN 'project_member'
            WHEN visibility = 'org' THEN 'org_visibility'
            ELSE NULL
        END AS grant_source
    FROM facts
)
SELECT org_id, effective_role, grant_source
FROM resolved
WHERE effective_role IS NOT NULL;
$$;


ALTER FUNCTION "public"."resolve_project_role"("p_project_id" "text", "p_user_id" "uuid") OWNER TO "postgres";

--
-- Name: revoke_user_git_http_credential("text", "text", "uuid"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."revoke_user_git_http_credential"("p_credential_id" "text", "p_project_id" "text", "p_user_id" "uuid") RETURNS boolean
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
DECLARE
    affected integer;
BEGIN
    UPDATE public.access_surface_credentials
       SET status = 'revoked', revoked_at = COALESCE(revoked_at, now())
     WHERE id = p_credential_id
       AND project_id = p_project_id
       AND user_id = p_user_id
       AND credential_type = 'git_http_token'
       AND credential_lifecycle = 'user'
       AND status = 'active';
    GET DIAGNOSTICS affected = ROW_COUNT;
    IF affected = 0 THEN
        RETURN false;
    END IF;

    INSERT INTO public.audit_logs (
        action, path, project_id, operator_type, operator_id, status, metadata
    ) VALUES (
        'git_credential.revoke', '', p_project_id, 'user', p_user_id::text,
        'success', jsonb_build_object('credential_id', p_credential_id)
    );
    RETURN true;
END;
$$;


ALTER FUNCTION "public"."revoke_user_git_http_credential"("p_credential_id" "text", "p_project_id" "text", "p_user_id" "uuid") OWNER TO "postgres";

--
-- Name: rotate_access_surface_bearer_token("text", "text", "text", "text", "text", "text", "text", "uuid", timestamp with time zone); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."rotate_access_surface_bearer_token"("p_access_surface_id" "text", "p_org_id" "text", "p_project_id" "text", "p_key_prefix" "text", "p_key_last4" "text", "p_key_hash" "text", "p_hash_alg" "text", "p_created_by" "uuid" DEFAULT NULL::"uuid", "p_expires_at" timestamp with time zone DEFAULT NULL::timestamp with time zone) RETURNS "text"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
DECLARE
    new_id text;
BEGIN
    PERFORM 1
    FROM public.access_surfaces s
    WHERE s.id = p_access_surface_id
      AND s.project_id = p_project_id
      AND (
          p_org_id IS NULL
          OR COALESCE(
              s.org_id,
              (SELECT p.org_id FROM public.projects p WHERE p.id = s.project_id)
          ) IS NOT DISTINCT FROM p_org_id
      )
    FOR UPDATE OF s;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'access Surface/Project/Organization mismatch';
    END IF;

    UPDATE public.access_surface_credentials
       SET status = 'revoked', revoked_at = now()
     WHERE access_surface_id = p_access_surface_id
       AND credential_type = 'bearer_token'
       AND credential_lifecycle = 'shared'
       AND status = 'active';

    INSERT INTO public.access_surface_credentials (
        org_id, project_id, access_surface_id,
        credential_type, credential_lifecycle,
        key_prefix, key_last4, key_hash, hash_alg, status,
        created_by, expires_at
    ) VALUES (
        p_org_id, p_project_id, p_access_surface_id,
        'bearer_token', 'shared', p_key_prefix, p_key_last4,
        p_key_hash, p_hash_alg, 'active', p_created_by, p_expires_at
    ) RETURNING id INTO new_id;
    RETURN new_id;
END;
$$;


ALTER FUNCTION "public"."rotate_access_surface_bearer_token"("p_access_surface_id" "text", "p_org_id" "text", "p_project_id" "text", "p_key_prefix" "text", "p_key_last4" "text", "p_key_hash" "text", "p_hash_alg" "text", "p_created_by" "uuid", "p_expires_at" timestamp with time zone) OWNER TO "postgres";

--
-- Name: rotate_access_surface_git_http_token("text", "text", "text", "text", "text", "text", "text", "text", "uuid", timestamp with time zone); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."rotate_access_surface_git_http_token"("p_access_surface_id" "text", "p_org_id" "text", "p_project_id" "text", "p_grant_mode" "text", "p_key_prefix" "text", "p_key_last4" "text", "p_key_hash" "text", "p_hash_alg" "text", "p_created_by" "uuid" DEFAULT NULL::"uuid", "p_expires_at" timestamp with time zone DEFAULT NULL::timestamp with time zone) RETURNS "text"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
DECLARE
    new_id text;
BEGIN
    IF p_grant_mode NOT IN ('r', 'rw') THEN
        RAISE EXCEPTION 'invalid Git credential mode';
    END IF;
    PERFORM 1
    FROM public.access_surfaces s
    LEFT JOIN public.repository_scopes rs
      ON rs.id = s.scope_id AND rs.project_id = s.project_id
    WHERE s.id = p_access_surface_id
      AND s.project_id = p_project_id
      AND s.org_id = p_org_id
      AND s.kind = 'git_remote'
      AND s.status = 'active'
      AND (s.scope_id IS NULL OR rs.id IS NOT NULL)
      AND (p_grant_mode = 'r' OR s.scope_id IS NULL OR rs.max_mode = 'rw')
    FOR UPDATE OF s;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Git credential Surface or capability is invalid';
    END IF;

    UPDATE public.access_surface_credentials
       SET status = 'revoked', revoked_at = now()
     WHERE access_surface_id = p_access_surface_id
       AND credential_type = 'git_http_token'
       AND credential_lifecycle = 'shared'
       AND grant_mode = p_grant_mode
       AND status = 'active';

    INSERT INTO public.access_surface_credentials (
        id, org_id, project_id, access_surface_id,
        credential_type, grant_mode, credential_lifecycle,
        key_prefix, key_last4, key_hash, hash_alg, status, created_by, expires_at
    ) VALUES (
        gen_random_uuid()::text, p_org_id, p_project_id,
        p_access_surface_id, 'git_http_token', p_grant_mode, 'shared',
        p_key_prefix, p_key_last4, p_key_hash, p_hash_alg,
        'active', p_created_by, p_expires_at
    ) RETURNING id INTO new_id;
    RETURN new_id;
END;
$$;


ALTER FUNCTION "public"."rotate_access_surface_git_http_token"("p_access_surface_id" "text", "p_org_id" "text", "p_project_id" "text", "p_grant_mode" "text", "p_key_prefix" "text", "p_key_last4" "text", "p_key_hash" "text", "p_hash_alg" "text", "p_created_by" "uuid", "p_expires_at" timestamp with time zone) OWNER TO "postgres";

--
-- Name: schedule_project_deletion_verification("text", "text", integer); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."schedule_project_deletion_verification"("p_job_id" "text", "p_worker_id" "text", "p_verify_after_seconds" integer DEFAULT 60) RETURNS boolean
    LANGUAGE "sql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
UPDATE public.project_deletion_jobs
SET phase = 'verify',
    status = 'pending',
    available_at = now() + make_interval(
        secs => GREATEST(COALESCE(p_verify_after_seconds, 60), 10)
    ),
    purged_at = now(),
    verification_cycles = verification_cycles + 1,
    claimed_at = NULL,
    claimed_by = NULL,
    updated_at = now(),
    last_error = NULL
WHERE id = p_job_id AND status = 'running' AND claimed_by = p_worker_id
RETURNING true;
$$;


ALTER FUNCTION "public"."schedule_project_deletion_verification"("p_job_id" "text", "p_worker_id" "text", "p_verify_after_seconds" integer) OWNER TO "postgres";

--
-- Name: sync_github_version_columns(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."sync_github_version_columns"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    NEW.version_commit_id := COALESCE(NEW.version_commit_id, NEW.mut_commit_id);
    NEW.mut_commit_id := NEW.version_commit_id;
  ELSIF NEW.version_commit_id IS DISTINCT FROM OLD.version_commit_id THEN
    NEW.mut_commit_id := NEW.version_commit_id;
  ELSIF NEW.mut_commit_id IS DISTINCT FROM OLD.mut_commit_id THEN
    NEW.version_commit_id := NEW.mut_commit_id;
  END IF;
  RETURN NEW;
END; $$;


ALTER FUNCTION "public"."sync_github_version_columns"() OWNER TO "postgres";

--
-- Name: sync_project_version_columns(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."sync_project_version_columns"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    NEW.version_root_hash := COALESCE(NEW.version_root_hash, NEW.mut_root_hash, '');
    NEW.mut_root_hash := NEW.version_root_hash;
  ELSIF NEW.version_root_hash IS DISTINCT FROM OLD.version_root_hash THEN
    NEW.mut_root_hash := NEW.version_root_hash;
  ELSIF NEW.mut_root_hash IS DISTINCT FROM OLD.mut_root_hash THEN
    NEW.version_root_hash := NEW.mut_root_hash;
  END IF;
  RETURN NEW;
END; $$;


ALTER FUNCTION "public"."sync_project_version_columns"() OWNER TO "postgres";

--
-- Name: sync_version_object_gc_candidates("text", "text"[], timestamp with time zone, integer); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."sync_version_object_gc_candidates"("p_project_id" "text", "p_object_ids" "text"[], "p_now" timestamp with time zone, "p_quarantine_seconds" integer) RETURNS TABLE("object_id" "text")
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'public', 'pg_temp'
    AS $$
BEGIN
    IF p_quarantine_seconds < 0 THEN
        RAISE EXCEPTION 'p_quarantine_seconds must be non-negative';
    END IF;

    DELETE FROM public.version_object_gc_candidates c
    WHERE c.project_id = p_project_id
      AND NOT (c.object_id = ANY(COALESCE(p_object_ids, ARRAY[]::text[])));

    INSERT INTO public.version_object_gc_candidates (
        project_id, object_id, first_seen_at, last_seen_at
    )
    SELECT p_project_id, candidate, p_now, p_now
    FROM unnest(COALESCE(p_object_ids, ARRAY[]::text[])) candidate
    ON CONFLICT ON CONSTRAINT version_object_gc_candidates_pkey DO UPDATE
    SET last_seen_at = EXCLUDED.last_seen_at;

    RETURN QUERY
    SELECT c.object_id
    FROM public.version_object_gc_candidates c
    WHERE c.project_id = p_project_id
      AND c.object_id = ANY(COALESCE(p_object_ids, ARRAY[]::text[]))
      AND c.first_seen_at <= p_now - make_interval(secs => p_quarantine_seconds);
END;
$$;


ALTER FUNCTION "public"."sync_version_object_gc_candidates"("p_project_id" "text", "p_object_ids" "text"[], "p_now" timestamp with time zone, "p_quarantine_seconds" integer) OWNER TO "postgres";

--
-- Name: transfer_organization_ownership("text", "uuid", "uuid"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."transfer_organization_ownership"("p_org_id" "text", "p_current_owner" "uuid", "p_new_owner" "uuid") RETURNS boolean
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO ''
    AS $$
DECLARE
    stored_current_role text;
    stored_target_role text;
BEGIN
    IF p_current_owner = p_new_owner THEN
        RAISE EXCEPTION 'ownership_transfer_same_user' USING ERRCODE = '22023';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(p_org_id || ':owner', 0));

    SELECT role INTO stored_current_role
    FROM public.org_members
    WHERE org_id = p_org_id AND user_id = p_current_owner
    FOR UPDATE;
    SELECT role INTO stored_target_role
    FROM public.org_members
    WHERE org_id = p_org_id AND user_id = p_new_owner
    FOR UPDATE;

    IF stored_current_role IS DISTINCT FROM 'owner' THEN
        RAISE EXCEPTION 'ownership_transfer_not_owner' USING ERRCODE = '42501';
    END IF;
    IF stored_target_role IS NULL OR stored_target_role = 'owner' THEN
        RAISE EXCEPTION 'ownership_transfer_invalid_target' USING ERRCODE = '22023';
    END IF;

    UPDATE public.org_members
    SET role = 'member'
    WHERE org_id = p_org_id AND user_id = p_current_owner;
    UPDATE public.org_members
    SET role = 'owner'
    WHERE org_id = p_org_id AND user_id = p_new_owner;
    RETURN true;
END;
$$;


ALTER FUNCTION "public"."transfer_organization_ownership"("p_org_id" "text", "p_current_owner" "uuid", "p_new_owner" "uuid") OWNER TO "postgres";

--
-- Name: unified_authorization_preflight(); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."unified_authorization_preflight"() RETURNS "jsonb"
    LANGUAGE "sql" STABLE
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
SELECT jsonb_build_object(
    'invalid_project_members', (
        SELECT count(*) FROM public.project_members pm
        LEFT JOIN public.projects p
          ON p.id = pm.project_id AND p.org_id = pm.org_id
        LEFT JOIN public.org_members om
          ON om.org_id = pm.org_id AND om.user_id = pm.user_id
        WHERE p.id IS NULL OR om.id IS NULL
    ),
    'creator_admin_unresolved', (
        SELECT count(*) FROM public.projects p
        LEFT JOIN public.org_members om
          ON om.org_id = p.org_id AND om.user_id = p.created_by
        LEFT JOIN public.project_members pm
          ON pm.project_id = p.id AND pm.user_id = p.created_by
        WHERE p.created_by IS NOT NULL
          AND (om.id IS NULL OR pm.role IS DISTINCT FROM 'admin')
    ),
    'invalid_repository_scopes', (
        SELECT count(*) FROM public.repository_scopes
        WHERE path = '' OR path LIKE '/%' OR path LIKE '%/' OR path LIKE '%//%'
    ),
    'orphan_access_surfaces', (
        SELECT count(*) FROM public.access_surfaces s
        LEFT JOIN public.projects p
          ON p.id = s.project_id AND p.org_id = s.org_id
        LEFT JOIN public.repository_scopes rs
          ON rs.id = s.scope_id AND rs.project_id = s.project_id
        WHERE p.id IS NULL OR (s.scope_id IS NOT NULL AND rs.id IS NULL)
    ),
    'orphan_access_credentials', (
        SELECT count(*) FROM public.access_surface_credentials c
        LEFT JOIN public.access_surfaces s
          ON s.id = c.access_surface_id
         AND s.project_id = c.project_id
         AND s.org_id = c.org_id
        LEFT JOIN public.org_members om
          ON om.org_id = c.org_id AND om.user_id = c.user_id
        WHERE s.id IS NULL
           OR (c.credential_lifecycle = 'user' AND om.id IS NULL)
    ),
    'invalid_access_tool_bindings', (
        SELECT count(*) FROM public.access_tools at
        LEFT JOIN public.access_surfaces s ON s.id = at.access_point_id
        LEFT JOIN public.tools t ON t.id = at.tool_id
        WHERE s.id IS NULL
           OR t.id IS NULL
           OR t.org_id IS DISTINCT FROM s.org_id
           OR (t.project_id IS NOT NULL
               AND t.project_id IS DISTINCT FROM s.project_id)
    ),
    'legacy_table_present', to_regclass('public.repo_user_permissions') IS NOT NULL
);
$$;


ALTER FUNCTION "public"."unified_authorization_preflight"() OWNER TO "postgres";

--
-- Name: update_project_member_role_authorized("text", "uuid", "text", "uuid"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."update_project_member_role_authorized"("p_project_id" "text", "p_target_user_id" "uuid", "p_role" "text", "p_actor_user_id" "uuid") RETURNS SETOF "public"."project_members"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $$
DECLARE
    actor_role text;
    previous_role text;
    updated_member public.project_members%ROWTYPE;
BEGIN
    IF p_role NOT IN ('admin', 'editor', 'viewer') THEN
        RAISE EXCEPTION 'invalid project role' USING ERRCODE = '22023';
    END IF;
    SELECT r.effective_role INTO actor_role
    FROM public.resolve_project_role(p_project_id, p_actor_user_id) r;
    IF actor_role IS DISTINCT FROM 'admin' THEN
        RAISE EXCEPTION 'project member management denied'
            USING ERRCODE = '42501';
    END IF;

    SELECT role INTO previous_role
    FROM public.project_members
    WHERE project_id = p_project_id AND user_id = p_target_user_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN;
    END IF;

    UPDATE public.project_members
    SET role = p_role, granted_by = p_actor_user_id
    WHERE project_id = p_project_id AND user_id = p_target_user_id
    RETURNING * INTO updated_member;

    INSERT INTO public.audit_logs (
        action, path, project_id, operator_type, operator_id, status, metadata
    ) VALUES (
        'project_member.role.update', '', p_project_id, 'user',
        p_actor_user_id::text, 'success',
        jsonb_build_object(
            'target_user_id', p_target_user_id,
            'previous_role', previous_role,
            'role', p_role
        )
    );
    RETURN NEXT updated_member;
END;
$$;


ALTER FUNCTION "public"."update_project_member_role_authorized"("p_project_id" "text", "p_target_user_id" "uuid", "p_role" "text", "p_actor_user_id" "uuid") OWNER TO "postgres";

--
-- Name: verify_project_storage_inventory(bigint, bigint, "text"); Type: FUNCTION; Schema: public; Owner: postgres
--

CREATE FUNCTION "public"."verify_project_storage_inventory"("p_observed_object_count" bigint, "p_observed_multipart_count" bigint, "p_inventory_digest" "text") RETURNS "jsonb"
    LANGUAGE "plpgsql" SECURITY DEFINER
    SET "search_path" TO 'pg_catalog', 'public', 'pg_temp'
    AS $_$
DECLARE
    state public.project_storage_inventory_state%ROWTYPE;
BEGIN
    SELECT * INTO state
    FROM public.project_storage_inventory_state
    WHERE singleton
    FOR UPDATE;
    IF COALESCE((state.checkpoint ->> 'scan_complete')::boolean, false) IS NOT TRUE
       OR state.inventory_digest IS NULL
       OR p_inventory_digest !~ '^[0-9a-f]{64}$'
       OR state.observed_object_count IS DISTINCT FROM p_observed_object_count
       OR state.observed_multipart_count IS DISTINCT FROM p_observed_multipart_count
       OR state.inventory_digest IS DISTINCT FROM p_inventory_digest THEN
        RETURN jsonb_build_object('outcome', 'verification_failed');
    END IF;
    UPDATE public.project_storage_inventory_state
    SET verification_object_count = p_observed_object_count,
        verification_multipart_count = p_observed_multipart_count,
        verification_digest = p_inventory_digest,
        updated_at = now()
    WHERE singleton;
    RETURN jsonb_build_object('outcome', 'verified');
END;
$_$;


ALTER FUNCTION "public"."verify_project_storage_inventory"("p_observed_object_count" bigint, "p_observed_multipart_count" bigint, "p_inventory_digest" "text") OWNER TO "postgres";

--
-- Name: access_logs; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."access_logs" (
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

--
-- Name: access_logs_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE "public"."access_logs" ALTER COLUMN "id" ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME "public"."access_logs_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: access_surface_credentials; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."access_surface_credentials" (
    "id" "text" DEFAULT ("gen_random_uuid"())::"text" NOT NULL,
    "org_id" "text" NOT NULL,
    "project_id" "text" NOT NULL,
    "access_surface_id" "text" NOT NULL,
    "credential_type" "text" NOT NULL,
    "key_prefix" "text" NOT NULL,
    "key_last4" "text" NOT NULL,
    "key_hash" "text" NOT NULL,
    "hash_alg" "text" DEFAULT 'hmac_sha256_v1'::"text" NOT NULL,
    "status" "text" DEFAULT 'active'::"text" NOT NULL,
    "expires_at" timestamp with time zone,
    "last_used_at" timestamp with time zone,
    "created_by" "uuid",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "revoked_at" timestamp with time zone,
    "grant_mode" "text" NOT NULL,
    "credential_lifecycle" "text" NOT NULL,
    "user_id" "uuid",
    CONSTRAINT "access_surface_credentials_credential_type_check" CHECK (("credential_type" = ANY (ARRAY['bearer_token'::"text", 'git_http_token'::"text", 'ssh_public_key'::"text"]))),
    CONSTRAINT "access_surface_credentials_grant_mode_check" CHECK (("grant_mode" = ANY (ARRAY['r'::"text", 'rw'::"text"]))),
    CONSTRAINT "access_surface_credentials_hash_check" CHECK (("key_hash" <> ''::"text")),
    CONSTRAINT "access_surface_credentials_last4_check" CHECK (("key_last4" <> ''::"text")),
    CONSTRAINT "access_surface_credentials_lifecycle_check" CHECK (("credential_lifecycle" = ANY (ARRAY['shared'::"text", 'session'::"text", 'user'::"text"]))),
    CONSTRAINT "access_surface_credentials_lifecycle_shape_check" CHECK (((("credential_lifecycle" = 'user'::"text") AND ("user_id" IS NOT NULL) AND ("credential_type" = 'git_http_token'::"text")) OR (("credential_lifecycle" = ANY (ARRAY['shared'::"text", 'session'::"text"])) AND ("user_id" IS NULL) AND (("credential_lifecycle" <> 'session'::"text") OR ("expires_at" IS NOT NULL))))),
    CONSTRAINT "access_surface_credentials_prefix_check" CHECK (("key_prefix" <> ''::"text")),
    CONSTRAINT "access_surface_credentials_status_check" CHECK (("status" = ANY (ARRAY['active'::"text", 'revoked'::"text"])))
);


ALTER TABLE "public"."access_surface_credentials" OWNER TO "postgres";

--
-- Name: COLUMN "access_surface_credentials"."grant_mode"; Type: COMMENT; Schema: public; Owner: postgres
--

COMMENT ON COLUMN "public"."access_surface_credentials"."grant_mode" IS 'Credential capability ceiling. Effective Git mode is the minimum of credential, target, Surface policy, and the owner current ProjectGrant.';


--
-- Name: COLUMN "access_surface_credentials"."credential_lifecycle"; Type: COMMENT; Schema: public; Owner: postgres
--

COMMENT ON COLUMN "public"."access_surface_credentials"."credential_lifecycle" IS 'Independent credential domain: shared service slot, expiring session, or user-owned Git credential.';


--
-- Name: COLUMN "access_surface_credentials"."user_id"; Type: COMMENT; Schema: public; Owner: postgres
--

COMMENT ON COLUMN "public"."access_surface_credentials"."user_id" IS 'Owner of a user-issued Git credential. This identifies a human principal, never a device, folder, or checkout.';


--
-- Name: access_surface_policies; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."access_surface_policies" (
    "access_surface_id" "text" NOT NULL,
    "version" integer DEFAULT 1 NOT NULL,
    "fs_policy" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "tools_policy" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "shell_policy" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "network_policy" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "access_surface_policies_version_check" CHECK (("version" >= 1))
);


ALTER TABLE "public"."access_surface_policies" OWNER TO "postgres";

--
-- Name: access_tools; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."access_tools" (
    "id" "text" DEFAULT ("extensions"."uuid_generate_v4"())::"text" NOT NULL,
    "access_point_id" "text" NOT NULL,
    "tool_id" "text" NOT NULL,
    "enabled" boolean DEFAULT true NOT NULL,
    "mcp_exposed" boolean DEFAULT false NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."access_tools" OWNER TO "postgres";

--
-- Name: agent_execution_logs; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."agent_execution_logs" (
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

--
-- Name: agent_execution_log_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE "public"."agent_execution_logs" ALTER COLUMN "id" ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME "public"."agent_execution_log_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: agent_logs; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."agent_logs" (
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

--
-- Name: agent_logs_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE "public"."agent_logs" ALTER COLUMN "id" ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME "public"."agent_logs_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: agent_profiles; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."agent_profiles" (
    "access_point_id" "text" NOT NULL,
    "model" "text",
    "system_prompt" "text",
    "agent_type" "text" DEFAULT 'chat'::"text",
    "temperature" real,
    "max_tokens" integer
);


ALTER TABLE "public"."agent_profiles" OWNER TO "postgres";

--
-- Name: api_keys; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."api_keys" (
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

--
-- Name: audit_logs; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."audit_logs" (
    "id" bigint NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "action" "text" NOT NULL,
    "path" "text",
    "operator_type" "text" DEFAULT 'user'::"text" NOT NULL,
    "operator_id" "text",
    "status" "text",
    "strategy" "text",
    "conflict_details" "text",
    "metadata" "jsonb",
    "project_id" "text",
    "transaction_id" bigint,
    "canonical_commit_id" "text",
    "original_commit_id" "text",
    "project_view_commit_id" "text",
    "scope_view_commit_id" "text",
    "scope_path" "text",
    "source_channel" "text",
    "policy" "text"
);


ALTER TABLE "public"."audit_logs" OWNER TO "postgres";

--
-- Name: audit_logs_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE "public"."audit_logs_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."audit_logs_id_seq" OWNER TO "postgres";

--
-- Name: audit_logs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: postgres
--

ALTER SEQUENCE "public"."audit_logs_id_seq" OWNED BY "public"."audit_logs"."id";


--
-- Name: bookmarks; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."bookmarks" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "project_id" "text" NOT NULL,
    "path" "text" NOT NULL,
    "label" "text",
    "type" "text" DEFAULT 'pin'::"text" NOT NULL,
    "created_by" "text",
    "created_at" timestamp with time zone DEFAULT "now"()
);


ALTER TABLE "public"."bookmarks" OWNER TO "postgres";

--
-- Name: TABLE "bookmarks"; Type: COMMENT; Schema: public; Owner: postgres
--

COMMENT ON TABLE "public"."bookmarks" IS 'Lightweight stable handles for shared/pinned paths. Not every file has one.';


--
-- Name: chat_messages; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."chat_messages" (
    "id" "text" DEFAULT ("extensions"."uuid_generate_v4"())::"text" NOT NULL,
    "session_id" "text" NOT NULL,
    "role" "text" NOT NULL,
    "content" "text",
    "parts" "jsonb",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."chat_messages" OWNER TO "postgres";

--
-- Name: chat_sessions; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."chat_sessions" (
    "id" "text" DEFAULT ("extensions"."uuid_generate_v4"())::"text" NOT NULL,
    "user_id" "uuid" NOT NULL,
    "agent_id" "text",
    "title" "text",
    "mode" "text" DEFAULT 'agent'::"text",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."chat_sessions" OWNER TO "postgres";

--
-- Name: chunks; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."chunks" (
    "id" bigint NOT NULL,
    "path" "text" NOT NULL,
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

--
-- Name: chunks_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE "public"."chunks" ALTER COLUMN "id" ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME "public"."chunks_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: connections; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."connections" (
    "id" "text" DEFAULT ("gen_random_uuid"())::"text" NOT NULL,
    "org_id" "text",
    "project_id" "text" NOT NULL,
    "scope_id" "text",
    "provider" "text" NOT NULL,
    "name" "text" NOT NULL,
    "direction" "text" NOT NULL,
    "external_resource_id" "text",
    "external_resource_label" "text",
    "external_url" "text",
    "oauth_connection_id" bigint,
    "credential_ref" "text",
    "config" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "trigger_type" "text" DEFAULT 'manual'::"text" NOT NULL,
    "trigger_config" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "status" "text" DEFAULT 'active'::"text" NOT NULL,
    "cursor" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "remote_hash" "text",
    "external_version" "text",
    "last_sync_run_id" "text",
    "last_synced_at" timestamp with time zone,
    "last_sync_commit_id" "text",
    "error_message" "text",
    "created_by" "uuid",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "target_path" "text" DEFAULT ''::"text" NOT NULL,
    CONSTRAINT "connections_direction_check" CHECK (("direction" = ANY (ARRAY['inbound'::"text", 'outbound'::"text", 'bidirectional'::"text"]))),
    CONSTRAINT "connections_name_check" CHECK (("name" <> ''::"text")),
    CONSTRAINT "connections_provider_check" CHECK (("provider" <> ''::"text")),
    CONSTRAINT "connections_status_check" CHECK (("status" = ANY (ARRAY['active'::"text", 'paused'::"text", 'syncing'::"text", 'error'::"text", 'disabled'::"text"]))),
    CONSTRAINT "connections_target_path_canonical" CHECK ((("target_path" = ''::"text") OR (("target_path" !~~ '/%'::"text") AND ("target_path" !~~ '%/'::"text") AND ("target_path" !~~ '%//%'::"text")))),
    CONSTRAINT "connections_trigger_type_check" CHECK (("trigger_type" = ANY (ARRAY['manual'::"text", 'scheduled'::"text", 'webhook'::"text", 'realtime'::"text"])))
);


ALTER TABLE "public"."connections" OWNER TO "postgres";

--
-- Name: COLUMN "connections"."target_path"; Type: COMMENT; Schema: public; Owner: postgres
--

COMMENT ON COLUMN "public"."connections"."target_path" IS 'Project-root destination path for Integration writes. scope_id is legacy/root compatibility, not the Integration write destination.';


--
-- Name: connector_runs; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."connector_runs" (
    "id" "text" DEFAULT ("gen_random_uuid"())::"text" NOT NULL,
    "connector_id" "text" NOT NULL,
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


ALTER TABLE "public"."connector_runs" OWNER TO "postgres";

--
-- Name: connectors; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."connectors" (
    "id" "text" DEFAULT ("gen_random_uuid"())::"text" NOT NULL,
    "project_id" "text" NOT NULL,
    "scope_id" "text" NOT NULL,
    "provider" "text" NOT NULL,
    "name" "text" NOT NULL,
    "direction" "text" NOT NULL,
    "config" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "oauth_connection_id" bigint,
    "trigger" "jsonb" DEFAULT '{"type": "manual"}'::"jsonb" NOT NULL,
    "status" "text" DEFAULT 'active'::"text" NOT NULL,
    "last_run_at" timestamp with time zone,
    "last_run_id" "text",
    "error_message" "text",
    "created_by" "uuid",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "policy" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    CONSTRAINT "connectors_direction_check" CHECK (("direction" = ANY (ARRAY['bidirectional'::"text", 'inbound'::"text", 'outbound'::"text"]))),
    CONSTRAINT "connectors_status_check" CHECK (("status" = ANY (ARRAY['active'::"text", 'paused'::"text", 'syncing'::"text", 'error'::"text"])))
);


ALTER TABLE "public"."connectors" OWNER TO "postgres";

--
-- Name: import_jobs; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."import_jobs" (
    "id" "text" DEFAULT ("gen_random_uuid"())::"text" NOT NULL,
    "org_id" "text",
    "project_id" "text" NOT NULL,
    "created_by" "uuid" NOT NULL,
    "provider" "text" NOT NULL,
    "source_url" "text" NOT NULL,
    "name" "text",
    "target_path" "text" DEFAULT ''::"text" NOT NULL,
    "config" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "status" "text" DEFAULT 'queued'::"text" NOT NULL,
    "phase" "text" DEFAULT 'queued'::"text" NOT NULL,
    "progress" integer DEFAULT 0 NOT NULL,
    "message" "text",
    "result_path" "text",
    "result_commit_id" "text",
    "error_message" "text",
    "worker_job_id" "text",
    "started_at" timestamp with time zone,
    "completed_at" timestamp with time zone,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "source_kind" "text",
    "source_ref" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "idempotency_key" "text",
    CONSTRAINT "import_jobs_progress_check" CHECK ((("progress" >= 0) AND ("progress" <= 100))),
    CONSTRAINT "import_jobs_provider_check" CHECK (("provider" <> ''::"text")),
    CONSTRAINT "import_jobs_source_kind_check" CHECK ((("source_kind" IS NULL) OR ("source_kind" = ANY (ARRAY['repository'::"text", 'url'::"text", 'website'::"text", 'template'::"text", 'document'::"text", 'other'::"text"])))),
    CONSTRAINT "import_jobs_source_url_check" CHECK (("source_url" <> ''::"text")),
    CONSTRAINT "import_jobs_status_check" CHECK (("status" = ANY (ARRAY['queued'::"text", 'running'::"text", 'completed'::"text", 'failed'::"text", 'cancelled'::"text"])))
);


ALTER TABLE "public"."import_jobs" OWNER TO "postgres";

--
-- Name: sync_runs; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."sync_runs" (
    "id" "text" DEFAULT ("gen_random_uuid"())::"text" NOT NULL,
    "connection_id" "text" NOT NULL,
    "project_id" "text" NOT NULL,
    "triggered_by" "text" NOT NULL,
    "triggered_by_user_id" "uuid",
    "direction" "text" NOT NULL,
    "status" "text" DEFAULT 'queued'::"text" NOT NULL,
    "phase" "text" DEFAULT 'queued'::"text" NOT NULL,
    "progress" integer DEFAULT 0 NOT NULL,
    "message" "text",
    "error_message" "text",
    "stdout" "text",
    "exit_code" integer,
    "duration_ms" integer,
    "worker_job_id" "text",
    "external_version" "text",
    "remote_hash" "text",
    "result_path" "text",
    "result_commit_id" "text",
    "files_changed" integer,
    "result" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "started_at" timestamp with time zone,
    "finished_at" timestamp with time zone,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "heartbeat_at" timestamp with time zone,
    "lease_expires_at" timestamp with time zone,
    CONSTRAINT "sync_runs_direction_check" CHECK (("direction" = ANY (ARRAY['inbound'::"text", 'outbound'::"text", 'bidirectional'::"text"]))),
    CONSTRAINT "sync_runs_progress_check" CHECK ((("progress" >= 0) AND ("progress" <= 100))),
    CONSTRAINT "sync_runs_status_check" CHECK (("status" = ANY (ARRAY['queued'::"text", 'running'::"text", 'completed'::"text", 'failed'::"text", 'cancelled'::"text", 'conflict'::"text", 'skipped'::"text"]))),
    CONSTRAINT "sync_runs_triggered_by_check" CHECK (("triggered_by" = ANY (ARRAY['manual'::"text", 'scheduled'::"text", 'webhook'::"text", 'realtime'::"text", 'initial'::"text", 'push'::"text"])))
);


ALTER TABLE "public"."sync_runs" OWNER TO "postgres";

--
-- Name: upload_jobs; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."upload_jobs" (
    "id" "text" DEFAULT ("gen_random_uuid"())::"text" NOT NULL,
    "org_id" "text",
    "project_id" "text" NOT NULL,
    "created_by" "uuid",
    "target_path" "text" DEFAULT ''::"text" NOT NULL,
    "source_kind" "text" DEFAULT 'browser'::"text" NOT NULL,
    "mode" "text" DEFAULT 'raw'::"text" NOT NULL,
    "status" "text" DEFAULT 'queued'::"text" NOT NULL,
    "phase" "text" DEFAULT 'queued'::"text" NOT NULL,
    "progress" integer DEFAULT 0 NOT NULL,
    "message" "text",
    "error_message" "text",
    "policy_summary" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "config" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "result_path" "text",
    "result_commit_id" "text",
    "worker_job_id" "text",
    "started_at" timestamp with time zone,
    "completed_at" timestamp with time zone,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "upload_jobs_mode_check" CHECK (("mode" = ANY (ARRAY['raw'::"text", 'ocr_parse'::"text", 'structured'::"text"]))),
    CONSTRAINT "upload_jobs_progress_check" CHECK ((("progress" >= 0) AND ("progress" <= 100))),
    CONSTRAINT "upload_jobs_source_kind_check" CHECK (("source_kind" = ANY (ARRAY['browser'::"text", 'desktop'::"text", 'cli'::"text"]))),
    CONSTRAINT "upload_jobs_status_check" CHECK (("status" = ANY (ARRAY['queued'::"text", 'running'::"text", 'completed'::"text", 'failed'::"text", 'cancelled'::"text"]))),
    CONSTRAINT "upload_jobs_target_path_canonical" CHECK ((("target_path" = ''::"text") OR (("target_path" !~~ '/%'::"text") AND ("target_path" !~~ '%/'::"text") AND ("target_path" !~~ '%//%'::"text"))))
);


ALTER TABLE "public"."upload_jobs" OWNER TO "postgres";

--
-- Name: context_activity_items; Type: VIEW; Schema: public; Owner: postgres
--

CREATE VIEW "public"."context_activity_items" WITH ("security_invoker"='true') AS
 SELECT "u"."id",
    'upload'::"text" AS "kind",
    "u"."project_id",
    "u"."created_by",
    COALESCE(NULLIF("u"."target_path", ''::"text"), 'Upload'::"text") AS "label",
    "u"."status",
    "u"."phase",
    "u"."progress",
    "u"."message",
    "u"."error_message",
    "u"."result_path",
    "u"."result_commit_id",
    "u"."created_at",
    "u"."completed_at"
   FROM "public"."upload_jobs" "u"
UNION ALL
 SELECT "i"."id",
    'import'::"text" AS "kind",
    "i"."project_id",
    "i"."created_by",
    COALESCE("i"."name", "i"."source_url", "i"."provider") AS "label",
    "i"."status",
    "i"."phase",
    "i"."progress",
    "i"."message",
    "i"."error_message",
    "i"."result_path",
    "i"."result_commit_id",
    "i"."created_at",
    "i"."completed_at"
   FROM "public"."import_jobs" "i"
UNION ALL
 SELECT "s"."id",
    'sync_run'::"text" AS "kind",
    "s"."project_id",
    "c"."created_by",
    COALESCE("c"."name", "c"."provider") AS "label",
    "s"."status",
    "s"."phase",
    "s"."progress",
    "s"."message",
    "s"."error_message",
    "s"."result_path",
    "s"."result_commit_id",
    "s"."created_at",
    "s"."finished_at" AS "completed_at"
   FROM ("public"."sync_runs" "s"
     JOIN "public"."connections" "c" ON (("c"."id" = "s"."connection_id")));


ALTER VIEW "public"."context_activity_items" OWNER TO "postgres";

--
-- Name: context_publishes; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."context_publishes" (
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


ALTER TABLE "public"."context_publishes" OWNER TO "postgres";

--
-- Name: context_publish_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE "public"."context_publishes" ALTER COLUMN "id" ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME "public"."context_publish_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: etl_rules; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."etl_rules" (
    "id" bigint NOT NULL,
    "created_by" "uuid",
    "name" "text" NOT NULL,
    "description" "text",
    "json_schema" "jsonb",
    "system_prompt" "text",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "org_id" "text"
);


ALTER TABLE "public"."etl_rules" OWNER TO "postgres";

--
-- Name: etl_rule_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE "public"."etl_rules" ALTER COLUMN "id" ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME "public"."etl_rule_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: fs_path_index; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."fs_path_index" (
    "id" bigint NOT NULL,
    "project_id" "text" NOT NULL,
    "scope_path" "text" DEFAULT ''::"text" NOT NULL,
    "full_path" "text" NOT NULL,
    "blob_hash" "text" DEFAULT ''::"text" NOT NULL,
    "size_bytes" bigint DEFAULT 0 NOT NULL,
    "mime_type" "text" DEFAULT ''::"text" NOT NULL,
    "last_who" "text" DEFAULT ''::"text" NOT NULL,
    "last_commit_id" "text" DEFAULT ''::"text" NOT NULL,
    "last_updated_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."fs_path_index" OWNER TO "postgres";

--
-- Name: fs_path_index_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE "public"."fs_path_index" ALTER COLUMN "id" ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME "public"."fs_path_index_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: git_credential_issue_operations; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."git_credential_issue_operations" (
    "actor_user_id" "uuid" NOT NULL,
    "operation_key" "text" NOT NULL,
    "payload_hash" "text" NOT NULL,
    "org_id" "text" NOT NULL,
    "project_id" "text" NOT NULL,
    "credential_id" "text" NOT NULL,
    "credential_hash" "text" NOT NULL,
    "status" "text" DEFAULT 'active'::"text" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "replayed_at" timestamp with time zone,
    "revoked_at" timestamp with time zone,
    CONSTRAINT "git_credential_issue_operations_hash_check" CHECK (("payload_hash" ~ '^[0-9a-f]{64}$'::"text")),
    CONSTRAINT "git_credential_issue_operations_key_check" CHECK (("operation_key" ~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'::"text")),
    CONSTRAINT "git_credential_issue_operations_status_check" CHECK (("status" = ANY (ARRAY['active'::"text", 'revoked'::"text", 'deleted'::"text"])))
);


ALTER TABLE "public"."git_credential_issue_operations" OWNER TO "postgres";

--
-- Name: github_integrations; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."github_integrations" (
    "id" "text" DEFAULT ("gen_random_uuid"())::"text" NOT NULL,
    "project_id" "text" NOT NULL,
    "oauth_connection_id" bigint,
    "github_repo_owner" "text" NOT NULL,
    "github_repo_name" "text" NOT NULL,
    "default_branch" "text" DEFAULT 'main'::"text" NOT NULL,
    "webhook_secret" "text",
    "auto_import" boolean DEFAULT false NOT NULL,
    "last_imported_sha" "text",
    "last_imported_at" timestamp with time zone,
    "last_exported_sha" "text",
    "last_exported_at" timestamp with time zone,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "github_integrations_auto_import_needs_webhook" CHECK ((("auto_import" = false) OR ("webhook_secret" IS NOT NULL))),
    CONSTRAINT "github_integrations_default_branch_check" CHECK (("length"("default_branch") > 0)),
    CONSTRAINT "github_integrations_github_repo_name_check" CHECK (("length"("github_repo_name") > 0)),
    CONSTRAINT "github_integrations_github_repo_owner_check" CHECK (("length"("github_repo_owner") > 0))
);


ALTER TABLE "public"."github_integrations" OWNER TO "postgres";

--
-- Name: github_sync_log; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."github_sync_log" (
    "id" "text" DEFAULT ("gen_random_uuid"())::"text" NOT NULL,
    "integration_id" "text" NOT NULL,
    "direction" "text" NOT NULL,
    "git_sha" "text",
    "mut_commit_id" "text",
    "status" "text" NOT NULL,
    "error_message" "text",
    "files_changed" integer,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "version_commit_id" "text",
    CONSTRAINT "github_sync_log_direction_check" CHECK (("direction" = ANY (ARRAY['import'::"text", 'export'::"text"]))),
    CONSTRAINT "github_sync_log_status_check" CHECK (("status" = ANY (ARRAY['pending'::"text", 'success'::"text", 'failed'::"text", 'conflict'::"text"])))
);


ALTER TABLE "public"."github_sync_log" OWNER TO "postgres";

--
-- Name: local_shadow_snapshots; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."local_shadow_snapshots" (
    "id" "uuid" DEFAULT "gen_random_uuid"() NOT NULL,
    "project_id" "text" NOT NULL,
    "user_id" "uuid" NOT NULL,
    "machine_id" "text" DEFAULT ''::"text" NOT NULL,
    "ref_name" "text" DEFAULT ''::"text" NOT NULL,
    "tree_hash" "text" DEFAULT ''::"text" NOT NULL,
    "file_count" integer DEFAULT 0 NOT NULL,
    "total_bytes" bigint DEFAULT 0 NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "grep_shared" boolean DEFAULT false NOT NULL
);


ALTER TABLE "public"."local_shadow_snapshots" OWNER TO "postgres";

--
-- Name: migration_log; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."migration_log" (
    "name" "text" NOT NULL,
    "applied_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "summary" "jsonb"
);


ALTER TABLE "public"."migration_log" OWNER TO "postgres";

--
-- Name: version_commits; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."version_commits" (
    "id" bigint NOT NULL,
    "project_id" "text" NOT NULL,
    "root_hash" "text" DEFAULT ''::"text" NOT NULL,
    "scope_path" "text" DEFAULT ''::"text" NOT NULL,
    "who" "text" NOT NULL,
    "message" "text" DEFAULT ''::"text" NOT NULL,
    "changes" "jsonb" DEFAULT '[]'::"jsonb" NOT NULL,
    "conflicts" "jsonb",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "scope_hash" "text" DEFAULT ''::"text",
    "commit_id" "text" NOT NULL,
    CONSTRAINT "scope_path_canonical" CHECK (("scope_path" = TRIM(BOTH '/'::"text" FROM COALESCE("scope_path", ''::"text"))))
);


ALTER TABLE "public"."version_commits" OWNER TO "postgres";

--
-- Name: mut_commits; Type: VIEW; Schema: public; Owner: postgres
--

CREATE VIEW "public"."mut_commits" WITH ("security_invoker"='true') AS
 SELECT "id",
    "project_id",
    "root_hash",
    "scope_path",
    "who",
    "message",
    "changes",
    "conflicts",
    "created_at",
    "scope_hash",
    "commit_id"
   FROM "public"."version_commits";


ALTER VIEW "public"."mut_commits" OWNER TO "postgres";

--
-- Name: mut_commits_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE "public"."version_commits" ALTER COLUMN "id" ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME "public"."mut_commits_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: version_conflicts; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."version_conflicts" (
    "id" bigint NOT NULL,
    "pending_conflict_id" "text" NOT NULL,
    "transaction_id" bigint,
    "project_id" "text" NOT NULL,
    "scope_path" "text" DEFAULT ''::"text" NOT NULL,
    "base_commit_id" "text" DEFAULT ''::"text" NOT NULL,
    "base_tree_id" "text" DEFAULT ''::"text" NOT NULL,
    "current_commit_id" "text" DEFAULT ''::"text" NOT NULL,
    "current_tree_id" "text" DEFAULT ''::"text" NOT NULL,
    "client_commit_id" "text" DEFAULT ''::"text" NOT NULL,
    "proposed_tree_id" "text" DEFAULT ''::"text" NOT NULL,
    "changed_paths" "jsonb" DEFAULT '[]'::"jsonb" NOT NULL,
    "conflict_records" "jsonb" DEFAULT '[]'::"jsonb" NOT NULL,
    "policy" "text" DEFAULT 'manual_review'::"text" NOT NULL,
    "status" "text" DEFAULT 'pending'::"text" NOT NULL,
    "resolver_actor" "text" DEFAULT ''::"text" NOT NULL,
    "resolver_kind" "text" DEFAULT ''::"text" NOT NULL,
    "resolution_commit_id" "text" DEFAULT ''::"text" NOT NULL,
    "resolution_detail" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "resolved_at" timestamp with time zone,
    CONSTRAINT "mut_conflicts_status_valid" CHECK (("status" = ANY (ARRAY['pending'::"text", 'resolving'::"text", 'resolved'::"text", 'rejected'::"text"])))
);


ALTER TABLE "public"."version_conflicts" OWNER TO "postgres";

--
-- Name: mut_conflicts; Type: VIEW; Schema: public; Owner: postgres
--

CREATE VIEW "public"."mut_conflicts" WITH ("security_invoker"='true') AS
 SELECT "id",
    "pending_conflict_id",
    "transaction_id",
    "project_id",
    "scope_path",
    "base_commit_id",
    "base_tree_id",
    "current_commit_id",
    "current_tree_id",
    "client_commit_id",
    "proposed_tree_id",
    "changed_paths",
    "conflict_records",
    "policy",
    "status",
    "resolver_actor",
    "resolver_kind",
    "resolution_commit_id",
    "resolution_detail",
    "created_at",
    "resolved_at"
   FROM "public"."version_conflicts";


ALTER VIEW "public"."mut_conflicts" OWNER TO "postgres";

--
-- Name: mut_conflicts_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE "public"."version_conflicts" ALTER COLUMN "id" ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME "public"."mut_conflicts_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: version_object_locations; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."version_object_locations" (
    "project_id" "text" NOT NULL,
    "object_id" "text" NOT NULL,
    "pack_key" "text" NOT NULL,
    "offset_bytes" bigint NOT NULL,
    "size_bytes" bigint NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."version_object_locations" OWNER TO "postgres";

--
-- Name: mut_object_locations; Type: VIEW; Schema: public; Owner: postgres
--

CREATE VIEW "public"."mut_object_locations" WITH ("security_invoker"='true') AS
 SELECT "project_id",
    "object_id",
    "pack_key",
    "offset_bytes",
    "size_bytes",
    "created_at"
   FROM "public"."version_object_locations";


ALTER VIEW "public"."mut_object_locations" OWNER TO "postgres";

--
-- Name: version_scope_state; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."version_scope_state" (
    "id" bigint NOT NULL,
    "project_id" "text" NOT NULL,
    "scope_path" "text" DEFAULT ''::"text" NOT NULL,
    "scope_hash" "text" DEFAULT ''::"text" NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "head_commit_id" "text" DEFAULT ''::"text" NOT NULL,
    CONSTRAINT "scope_path_canonical" CHECK (("scope_path" = TRIM(BOTH '/'::"text" FROM COALESCE("scope_path", ''::"text"))))
);


ALTER TABLE "public"."version_scope_state" OWNER TO "postgres";

--
-- Name: mut_scope_state; Type: VIEW; Schema: public; Owner: postgres
--

CREATE VIEW "public"."mut_scope_state" WITH ("security_invoker"='true') AS
 SELECT "id",
    "project_id",
    "scope_path",
    "scope_hash",
    "updated_at",
    "head_commit_id"
   FROM "public"."version_scope_state";


ALTER VIEW "public"."mut_scope_state" OWNER TO "postgres";

--
-- Name: mut_scope_state_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE "public"."version_scope_state" ALTER COLUMN "id" ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME "public"."mut_scope_state_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: version_view_commits; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."version_view_commits" (
    "id" bigint NOT NULL,
    "project_id" "text" NOT NULL,
    "scope_path" "text" DEFAULT ''::"text" NOT NULL,
    "source_commit_id" "text" NOT NULL,
    "source_scope_hash" "text" DEFAULT ''::"text" NOT NULL,
    "project_root_hash" "text" DEFAULT ''::"text" NOT NULL,
    "project_view_commit_id" "text" DEFAULT ''::"text" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."version_view_commits" OWNER TO "postgres";

--
-- Name: mut_version_index; Type: VIEW; Schema: public; Owner: postgres
--

CREATE VIEW "public"."mut_version_index" WITH ("security_invoker"='true') AS
 SELECT "id",
    "project_id",
    "scope_path",
    "source_commit_id",
    "source_scope_hash",
    "project_root_hash",
    "project_view_commit_id",
    "created_at"
   FROM "public"."version_view_commits";


ALTER VIEW "public"."mut_version_index" OWNER TO "postgres";

--
-- Name: mut_version_index_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE "public"."version_view_commits" ALTER COLUMN "id" ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME "public"."mut_version_index_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: version_outbox; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."version_outbox" (
    "id" bigint NOT NULL,
    "project_id" "text" NOT NULL,
    "commit_id" "text" NOT NULL,
    "event_type" "text" NOT NULL,
    "payload" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "attempts" integer DEFAULT 0 NOT NULL,
    "locked_at" timestamp with time zone,
    "last_error" "text",
    "processed_at" timestamp with time zone,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."version_outbox" OWNER TO "postgres";

--
-- Name: mut_version_outbox; Type: VIEW; Schema: public; Owner: postgres
--

CREATE VIEW "public"."mut_version_outbox" WITH ("security_invoker"='true') AS
 SELECT "id",
    "project_id",
    "commit_id",
    "event_type",
    "payload",
    "attempts",
    "locked_at",
    "last_error",
    "processed_at",
    "created_at"
   FROM "public"."version_outbox";


ALTER VIEW "public"."mut_version_outbox" OWNER TO "postgres";

--
-- Name: mut_version_outbox_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE "public"."version_outbox" ALTER COLUMN "id" ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME "public"."mut_version_outbox_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: oauth_connections; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."oauth_connections" (
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

--
-- Name: oauth_connection_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE "public"."oauth_connections" ALTER COLUMN "id" ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME "public"."oauth_connection_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: oauth_states; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."oauth_states" (
    "state" "text" NOT NULL,
    "user_id" "uuid" NOT NULL,
    "provider" "text" NOT NULL,
    "redirect_uri" "text",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "expires_at" timestamp with time zone DEFAULT ("now"() + '00:10:00'::interval) NOT NULL
);


ALTER TABLE "public"."oauth_states" OWNER TO "postgres";

--
-- Name: org_invitations; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."org_invitations" (
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

--
-- Name: org_members; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."org_members" (
    "id" "text" DEFAULT ("extensions"."uuid_generate_v4"())::"text" NOT NULL,
    "org_id" "text" NOT NULL,
    "user_id" "uuid" NOT NULL,
    "role" "text" DEFAULT 'member'::"text" NOT NULL,
    "joined_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "org_members_role_check" CHECK (("role" = ANY (ARRAY['owner'::"text", 'member'::"text", 'viewer'::"text"])))
);


ALTER TABLE "public"."org_members" OWNER TO "postgres";

--
-- Name: organization_entitlement_events; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."organization_entitlement_events" (
    "id" "text" DEFAULT ("extensions"."uuid_generate_v4"())::"text" NOT NULL,
    "org_id" "text" NOT NULL,
    "source" "text" DEFAULT 'puppypay'::"text" NOT NULL,
    "source_event_id" "text",
    "event_type" "text",
    "old_plan_id" "text",
    "new_plan_id" "text",
    "old_entitlements" "jsonb",
    "new_entitlements" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "schema_version" "text",
    "catalog_version" "text",
    "source_revision" bigint,
    "seat_quantity" integer,
    "payload_hash" "text",
    "publication_outcome" "text",
    "source_quote_id" "text",
    CONSTRAINT "organization_entitlement_events_json_object_check" CHECK (("jsonb_typeof"("new_entitlements") = 'object'::"text")),
    CONSTRAINT "organization_entitlement_events_source_quote_shape" CHECK ((("source_quote_id" IS NULL) OR ((NULLIF("btrim"("source_quote_id"), ''::"text") IS NOT NULL) AND ("length"("source_quote_id") <= 255))))
);


ALTER TABLE "public"."organization_entitlement_events" OWNER TO "postgres";

--
-- Name: organization_entitlements; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."organization_entitlements" (
    "org_id" "text" NOT NULL,
    "plan_id" "text" DEFAULT 'free'::"text" NOT NULL,
    "status" "text" DEFAULT 'free'::"text" NOT NULL,
    "source" "text" DEFAULT 'local'::"text" NOT NULL,
    "entitlements" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "current_period_end" timestamp with time zone,
    "effective_until" timestamp with time zone,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "schema_version" "text" DEFAULT '1.0'::"text" NOT NULL,
    "catalog_version" "text" DEFAULT 'legacy'::"text" NOT NULL,
    "source_revision" bigint DEFAULT 0 NOT NULL,
    "seat_quantity" integer DEFAULT 0 NOT NULL,
    "effective_at" timestamp with time zone,
    "payload_hash" "text" DEFAULT ''::"text" NOT NULL,
    "source_quote_id" "text",
    CONSTRAINT "organization_entitlements_json_object_check" CHECK (("jsonb_typeof"("entitlements") = 'object'::"text")),
    CONSTRAINT "organization_entitlements_payload_hash_shape" CHECK ((("payload_hash" = ''::"text") OR ("payload_hash" ~ '^[0-9a-f]{64}$'::"text"))),
    CONSTRAINT "organization_entitlements_revision_nonnegative" CHECK (("source_revision" >= 0)),
    CONSTRAINT "organization_entitlements_seats_nonnegative" CHECK (("seat_quantity" >= 0)),
    CONSTRAINT "organization_entitlements_source_check" CHECK (("source" = ANY (ARRAY['local'::"text", 'puppypay'::"text", 'admin'::"text", 'system'::"text"]))),
    CONSTRAINT "organization_entitlements_source_quote_shape" CHECK ((("source_quote_id" IS NULL) OR ((NULLIF("btrim"("source_quote_id"), ''::"text") IS NOT NULL) AND ("length"("source_quote_id") <= 255)))),
    CONSTRAINT "organization_entitlements_status_check" CHECK (("status" = ANY (ARRAY['free'::"text", 'trialing'::"text", 'checkout_pending'::"text", 'active'::"text", 'change_pending'::"text", 'cancel_scheduled'::"text", 'past_due'::"text", 'canceled'::"text", 'expired'::"text", 'grace'::"text", 'revoked'::"text", 'disputed'::"text"])))
);


ALTER TABLE "public"."organization_entitlements" OWNER TO "postgres";

--
-- Name: organization_usage_counters; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."organization_usage_counters" (
    "org_id" "text" NOT NULL,
    "metric" "text" NOT NULL,
    "value" bigint DEFAULT 0 NOT NULL,
    "version" bigint DEFAULT 0 NOT NULL,
    "threshold_percent" integer DEFAULT 0 NOT NULL,
    "reconciled_at" timestamp with time zone,
    "full_reconciled_at" timestamp with time zone,
    "reconciliation_claimed_at" timestamp with time zone,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "organization_usage_counters_metric_check" CHECK (("metric" = 'storage.logical_bytes'::"text")),
    CONSTRAINT "organization_usage_counters_threshold_check" CHECK (("threshold_percent" = ANY (ARRAY[0, 80, 95, 100]))),
    CONSTRAINT "organization_usage_counters_value_check" CHECK (("value" >= 0)),
    CONSTRAINT "organization_usage_counters_version_check" CHECK (("version" >= 0))
);


ALTER TABLE "public"."organization_usage_counters" OWNER TO "postgres";

--
-- Name: organization_usage_events; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."organization_usage_events" (
    "id" "text" DEFAULT ("extensions"."uuid_generate_v4"())::"text" NOT NULL,
    "org_id" "text" NOT NULL,
    "metric" "text" NOT NULL,
    "idempotency_key" "text" NOT NULL,
    "delta" bigint NOT NULL,
    "value_after" bigint NOT NULL,
    "source" "text" NOT NULL,
    "metadata" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "organization_usage_events_metadata_object" CHECK (("jsonb_typeof"("metadata") = 'object'::"text")),
    CONSTRAINT "organization_usage_events_metric_check" CHECK (("metric" = 'storage.logical_bytes'::"text")),
    CONSTRAINT "organization_usage_events_value_check" CHECK (("value_after" >= 0))
);


ALTER TABLE "public"."organization_usage_events" OWNER TO "postgres";

--
-- Name: organizations; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."organizations" (
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

--
-- Name: profiles; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."profiles" (
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

--
-- Name: project_storage_inventory_batches; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."project_storage_inventory_batches" (
    "batch_key" "text" NOT NULL,
    "checkpoint" "jsonb" NOT NULL,
    "observed_object_count" integer NOT NULL,
    "observed_multipart_count" integer NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "project_storage_inventory_batch_key_check" CHECK (("batch_key" ~ '^[0-9a-f]{64}$'::"text")),
    CONSTRAINT "project_storage_inventory_batche_observed_multipart_count_check" CHECK (("observed_multipart_count" >= 0)),
    CONSTRAINT "project_storage_inventory_batches_observed_object_count_check" CHECK (("observed_object_count" >= 0))
);


ALTER TABLE "public"."project_storage_inventory_batches" OWNER TO "postgres";

--
-- Name: project_storage_inventory_state; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."project_storage_inventory_state" (
    "singleton" boolean DEFAULT true NOT NULL,
    "inventory_complete" boolean DEFAULT false NOT NULL,
    "checkpoint" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "observed_object_count" bigint DEFAULT 0 NOT NULL,
    "observed_multipart_count" bigint DEFAULT 0 NOT NULL,
    "inventory_digest" "text",
    "verification_object_count" bigint,
    "verification_multipart_count" bigint,
    "verification_digest" "text",
    "completed_at" timestamp with time zone,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "project_storage_inventory_st_verification_multipart_count_check" CHECK (("verification_multipart_count" >= 0)),
    CONSTRAINT "project_storage_inventory_state_inventory_digest_check" CHECK (("inventory_digest" ~ '^[0-9a-f]{64}$'::"text")),
    CONSTRAINT "project_storage_inventory_state_observed_multipart_count_check" CHECK (("observed_multipart_count" >= 0)),
    CONSTRAINT "project_storage_inventory_state_observed_object_count_check" CHECK (("observed_object_count" >= 0)),
    CONSTRAINT "project_storage_inventory_state_singleton_check" CHECK ("singleton"),
    CONSTRAINT "project_storage_inventory_state_verification_digest_check" CHECK (("verification_digest" ~ '^[0-9a-f]{64}$'::"text")),
    CONSTRAINT "project_storage_inventory_state_verification_object_count_check" CHECK (("verification_object_count" >= 0))
);


ALTER TABLE "public"."project_storage_inventory_state" OWNER TO "postgres";

--
-- Name: project_storage_orphan_prefixes; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."project_storage_orphan_prefixes" (
    "project_id" "text" NOT NULL,
    "principal" "text" NOT NULL,
    "status" "text" DEFAULT 'pending'::"text" NOT NULL,
    "first_seen_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "cleaned_at" timestamp with time zone,
    CONSTRAINT "project_storage_orphan_prefixes_segment_check" CHECK ((("project_id" ~ '^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$'::"text") AND ("principal" ~ '^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$'::"text"))),
    CONSTRAINT "project_storage_orphan_prefixes_status_check" CHECK (("status" = ANY (ARRAY['pending'::"text", 'cleaned'::"text"])))
);


ALTER TABLE "public"."project_storage_orphan_prefixes" OWNER TO "postgres";

--
-- Name: project_storage_principals; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."project_storage_principals" (
    "project_id" "text" NOT NULL,
    "principal" "text" NOT NULL,
    "first_seen_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "project_storage_principals_segment_check" CHECK ((("project_id" ~ '^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$'::"text") AND ("principal" ~ '^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$'::"text")))
);


ALTER TABLE "public"."project_storage_principals" OWNER TO "postgres";

--
-- Name: project_write_leases; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."project_write_leases" (
    "id" "uuid" NOT NULL,
    "project_id" "text" NOT NULL,
    "holder_id" "text" NOT NULL,
    "operation" "text" NOT NULL,
    "acquired_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "renewed_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "expires_at" timestamp with time zone NOT NULL,
    CONSTRAINT "project_write_leases_holder_check" CHECK (("holder_id" ~ '^[A-Za-z0-9][A-Za-z0-9:._-]{0,255}$'::"text")),
    CONSTRAINT "project_write_leases_operation_check" CHECK (("operation" ~ '^[A-Za-z0-9][A-Za-z0-9:._/-]{0,255}$'::"text"))
);


ALTER TABLE "public"."project_write_leases" OWNER TO "postgres";

--
-- Name: projects; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."projects" (
    "id" "text" DEFAULT ("extensions"."uuid_generate_v4"())::"text" NOT NULL,
    "name" "text" NOT NULL,
    "description" "text",
    "created_by" "uuid",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "org_id" "text" NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "visibility" "text" DEFAULT 'org'::"text" NOT NULL,
    "mut_root_hash" "text" DEFAULT ''::"text",
    "prompt_template" "text" DEFAULT 'You are connected to a PuppyOne repo via the mut protocol.

The mut protocol gives you read+write access to a versioned, scoped subtree of files. You can clone the current state, push your changes back, and pull the latest from other agents working on the same repo.

To work with this repo:
  - Use `mut clone <url>` to fetch the current state of your scope.
  - Use `mut push` to commit and upload your changes.
  - Use `mut pull` to get changes from other agents or web users.

Your working scope is constrained — paths outside the scope are invisible. The repo URL above already encodes which scope you have access to.

When the user asks you to make a change to the repo, prefer making it locally first, running tests if applicable, then `mut push` once the change is verified.'::"text" NOT NULL,
    "bound_git_branch" "text" DEFAULT 'main'::"text" NOT NULL,
    "share_token" "text" DEFAULT ('prj_'::"text" || "replace"(("gen_random_uuid"())::"text", '-'::"text", ''::"text")) NOT NULL,
    "version_root_hash" "text" DEFAULT ''::"text",
    "lifecycle_status" "text" NOT NULL,
    CONSTRAINT "projects_bound_git_branch_nonempty" CHECK (("length"("bound_git_branch") > 0)),
    CONSTRAINT "projects_lifecycle_status_check" CHECK (("lifecycle_status" = ANY (ARRAY['initializing'::"text", 'ready'::"text", 'deleting'::"text"]))),
    CONSTRAINT "projects_visibility_check" CHECK (("visibility" = ANY (ARRAY['org'::"text", 'private'::"text"])))
);


ALTER TABLE "public"."projects" OWNER TO "postgres";

--
-- Name: COLUMN "projects"."share_token"; Type: COMMENT; Schema: public; Owner: postgres
--

COMMENT ON COLUMN "public"."projects"."share_token" IS 'Per-project URL-safe token. Holder can join the project as viewer via POST /projects/share/{token}/join. Rotate (regenerate) to revoke outstanding links.';


--
-- Name: repository_scopes; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."repository_scopes" (
    "id" "text" DEFAULT ("gen_random_uuid"())::"text" NOT NULL,
    "project_id" "text" NOT NULL,
    "name" "text" NOT NULL,
    "path" "text" NOT NULL,
    "exclude" "jsonb" DEFAULT '[]'::"jsonb" NOT NULL,
    "max_mode" "text" DEFAULT 'rw'::"text" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "repo_scopes_mode_check" CHECK (("max_mode" = ANY (ARRAY['r'::"text", 'rw'::"text"]))),
    CONSTRAINT "repository_scopes_path_canonical" CHECK ((("path" <> ''::"text") AND ("path" !~~ '/%'::"text") AND ("path" !~~ '%/'::"text") AND ("path" !~~ '%//%'::"text")))
);


ALTER TABLE "public"."repository_scopes" OWNER TO "postgres";

--
-- Name: runtime_billing_runs; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."runtime_billing_runs" (
    "run_id" "text" NOT NULL,
    "org_id" "text" NOT NULL,
    "project_id" "text",
    "runtime_kind" "text" NOT NULL,
    "compute_profile" "text" DEFAULT 'standard'::"text" NOT NULL,
    "status" "text" DEFAULT 'pending_reservation'::"text" NOT NULL,
    "idempotency_key" "text" NOT NULL,
    "reservation_id" "text",
    "estimated_units" bigint DEFAULT 0 NOT NULL,
    "actual_units" bigint,
    "started_at" timestamp with time zone,
    "heartbeat_at" timestamp with time zone,
    "settled_at" timestamp with time zone,
    "expires_at" timestamp with time zone,
    "attempts" integer DEFAULT 0 NOT NULL,
    "last_error" "text",
    "metadata" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "runtime_billing_runs_kind_check" CHECK (("runtime_kind" = ANY (ARRAY['automation'::"text", 'sandbox'::"text", 'workspace'::"text", 'connector'::"text"]))),
    CONSTRAINT "runtime_billing_runs_metadata_object" CHECK (("jsonb_typeof"("metadata") = 'object'::"text")),
    CONSTRAINT "runtime_billing_runs_status_check" CHECK (("status" = ANY (ARRAY['pending_reservation'::"text", 'reserved'::"text", 'running'::"text", 'settling'::"text", 'settled'::"text", 'canceled'::"text", 'denied'::"text", 'expired'::"text", 'failed'::"text", 'reservation_failed'::"text", 'unmetered'::"text"]))),
    CONSTRAINT "runtime_billing_runs_units_check" CHECK ((("estimated_units" >= 0) AND (("actual_units" IS NULL) OR ("actual_units" >= 0))))
);


ALTER TABLE "public"."runtime_billing_runs" OWNER TO "postgres";

--
-- Name: sandbox_endpoints; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."sandbox_endpoints" (
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

--
-- Name: sandbox_execution_sessions; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."sandbox_execution_sessions" (
    "session_id" "text" NOT NULL,
    "provider" "text" NOT NULL,
    "resource_id" "text" NOT NULL,
    "readonly" boolean DEFAULT false NOT NULL,
    "temp_path" "text" DEFAULT ''::"text" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "last_activity" timestamp with time zone DEFAULT "now"() NOT NULL,
    "project_id" "text",
    CONSTRAINT "sandbox_execution_sessions_provider_check" CHECK (("provider" = ANY (ARRAY['docker'::"text", 'e2b'::"text"])))
);


ALTER TABLE "public"."sandbox_execution_sessions" OWNER TO "postgres";

--
-- Name: TABLE "sandbox_execution_sessions"; Type: COMMENT; Schema: public; Owner: postgres
--

COMMENT ON TABLE "public"."sandbox_execution_sessions" IS 'Internal durable ownership records for request-oriented sandbox providers.';


--
-- Name: scope_sandbox_sessions; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."scope_sandbox_sessions" (
    "scope_id" "text" NOT NULL,
    "project_id" "text" NOT NULL,
    "provider" "text" NOT NULL,
    "sandbox_id" "text" NOT NULL,
    "state" "text" NOT NULL,
    "connected_users" "jsonb" DEFAULT '[]'::"jsonb" NOT NULL,
    "activity_events" "jsonb" DEFAULT '[]'::"jsonb" NOT NULL,
    "recent_user_events" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "connection" "jsonb",
    "last_full_pull_seconds" double precision DEFAULT 0 NOT NULL,
    "repo_size_bytes" bigint DEFAULT 0 NOT NULL,
    "created_at" double precision NOT NULL,
    "last_active_at" double precision NOT NULL,
    "last_state_change_at" double precision NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "scope_sandbox_sessions_provider_check" CHECK (("provider" <> ''::"text")),
    CONSTRAINT "scope_sandbox_sessions_sandbox_id_check" CHECK (("sandbox_id" <> ''::"text")),
    CONSTRAINT "scope_sandbox_sessions_state_check" CHECK (("state" = ANY (ARRAY['pending'::"text", 'running'::"text", 'stopped'::"text", 'destroyed'::"text", 'unknown'::"text"])))
);


ALTER TABLE "public"."scope_sandbox_sessions" OWNER TO "postgres";

--
-- Name: scope_sync_events; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."scope_sync_events" (
    "id" bigint NOT NULL,
    "project_id" "text" NOT NULL,
    "scope_id" "text" NOT NULL,
    "head_version" "text" NOT NULL,
    "affected_paths" "jsonb" DEFAULT '[]'::"jsonb" NOT NULL,
    "source" "text" DEFAULT 'publish'::"text" NOT NULL,
    "origin_user" "text",
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "scope_sync_events_source_check" CHECK (("source" = ANY (ARRAY['publish'::"text", 'scope-sync'::"text"])))
);


ALTER TABLE "public"."scope_sync_events" OWNER TO "postgres";

--
-- Name: scope_sync_events_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE "public"."scope_sync_events_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."scope_sync_events_id_seq" OWNER TO "postgres";

--
-- Name: scope_sync_events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: postgres
--

ALTER SEQUENCE "public"."scope_sync_events_id_seq" OWNED BY "public"."scope_sync_events"."id";


--
-- Name: scope_sync_settings; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."scope_sync_settings" (
    "project_id" "text" NOT NULL,
    "scope_id" "text" NOT NULL,
    "persona" "text" DEFAULT 'dev'::"text" NOT NULL,
    "auto_sync" boolean DEFAULT true NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "scope_sync_settings_persona_check" CHECK (("persona" = ANY (ARRAY['non_dev'::"text", 'dev'::"text", 'reviewer'::"text"])))
);


ALTER TABLE "public"."scope_sync_settings" OWNER TO "postgres";

--
-- Name: subscriptions; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."subscriptions" (
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

--
-- Name: sync_changelog; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."sync_changelog" (
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

--
-- Name: TABLE "sync_changelog"; Type: COMMENT; Schema: public; Owner: postgres
--

COMMENT ON TABLE "public"."sync_changelog" IS 'Append-only change log for cursor-based incremental sync. Each row represents a content_node mutation. Clients store the last-seen id as their cursor and pull only newer entries. Rows older than 30 days are periodically cleaned up; expired cursors trigger a full-sync reset.';


--
-- Name: sync_changelog_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE "public"."sync_changelog_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."sync_changelog_id_seq" OWNER TO "postgres";

--
-- Name: sync_changelog_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: postgres
--

ALTER SEQUENCE "public"."sync_changelog_id_seq" OWNED BY "public"."sync_changelog"."id";


--
-- Name: sync_state; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."sync_state" (
    "access_point_id" "text" NOT NULL,
    "direction" "text" DEFAULT 'inbound'::"text" NOT NULL,
    "authority" "text" DEFAULT 'mirror'::"text" NOT NULL,
    "trigger" "jsonb" DEFAULT '{"type": "manual"}'::"jsonb" NOT NULL,
    "conflict_strategy" "text" DEFAULT 'source_wins'::"text" NOT NULL,
    "cursor" bigint DEFAULT 0,
    "last_synced_at" timestamp with time zone,
    "remote_hash" "text",
    "credentials_ref" "text",
    "last_sync_commit_id" "text" DEFAULT ''::"text" NOT NULL
);


ALTER TABLE "public"."sync_state" OWNER TO "postgres";

--
-- Name: tables; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."tables" (
    "id" "text" NOT NULL,
    "name" "text",
    "project_id" "text" NOT NULL,
    "created_by" "text",
    "description" "text",
    "data" "jsonb",
    "created_at" timestamp with time zone DEFAULT "now"()
);


ALTER TABLE "public"."tables" OWNER TO "postgres";

--
-- Name: TABLE "tables"; Type: COMMENT; Schema: public; Owner: postgres
--

COMMENT ON TABLE "public"."tables" IS 'Structured data tables (JSON Pointer). Previously stored as content_nodes rows.';


--
-- Name: tools; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."tools" (
    "id" "text" DEFAULT ("extensions"."uuid_generate_v4"())::"text" NOT NULL,
    "created_by" "uuid",
    "project_id" "text",
    "path" "text",
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

--
-- Name: upload_items; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."upload_items" (
    "id" "text" DEFAULT ("gen_random_uuid"())::"text" NOT NULL,
    "upload_job_id" "text" NOT NULL,
    "relative_path" "text" NOT NULL,
    "original_name" "text" NOT NULL,
    "size_bytes" bigint DEFAULT 0 NOT NULL,
    "mime_type" "text",
    "s3_key" "text",
    "content_hash" "text",
    "status" "text" DEFAULT 'pending'::"text" NOT NULL,
    "skip_reason" "text",
    "result_path" "text",
    "error_message" "text",
    "metadata" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "upload_items_relative_path_canonical" CHECK ((("relative_path" <> ''::"text") AND ("relative_path" !~~ '/%'::"text") AND ("relative_path" !~~ '%/'::"text") AND ("relative_path" !~~ '%//%'::"text") AND ("relative_path" !~~ '../%'::"text") AND ("relative_path" !~~ '%/../%'::"text"))),
    CONSTRAINT "upload_items_size_bytes_check" CHECK (("size_bytes" >= 0)),
    CONSTRAINT "upload_items_status_check" CHECK (("status" = ANY (ARRAY['pending'::"text", 'uploaded'::"text", 'processing'::"text", 'completed'::"text", 'failed'::"text", 'cancelled'::"text", 'skipped'::"text"])))
);


ALTER TABLE "public"."upload_items" OWNER TO "postgres";

--
-- Name: uploads; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."uploads" (
    "id" "text" DEFAULT ("gen_random_uuid"())::"text" NOT NULL,
    "created_by" "uuid",
    "project_id" "text" NOT NULL,
    "path" "text",
    "type" "text" NOT NULL,
    "config" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "status" "text" DEFAULT 'pending'::"text" NOT NULL,
    "progress" integer DEFAULT 0 NOT NULL,
    "message" "text",
    "error" "text",
    "result_path" "text",
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

--
-- Name: version_transactions; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."version_transactions" (
    "id" bigint NOT NULL,
    "project_id" "text" NOT NULL,
    "scope_path" "text" DEFAULT ''::"text" NOT NULL,
    "source_channel" "text" NOT NULL,
    "actor" "text" DEFAULT ''::"text" NOT NULL,
    "intent_type" "text" NOT NULL,
    "status" "text" DEFAULT 'received'::"text" NOT NULL,
    "policy" "text" DEFAULT ''::"text" NOT NULL,
    "base_commit_id" "text" DEFAULT ''::"text" NOT NULL,
    "client_commit_id" "text" DEFAULT ''::"text" NOT NULL,
    "proposed_tree_id" "text" DEFAULT ''::"text" NOT NULL,
    "current_head_at_start" "text" DEFAULT ''::"text" NOT NULL,
    "committed_commit_id" "text" DEFAULT ''::"text" NOT NULL,
    "project_view_commit_id" "text" DEFAULT ''::"text" NOT NULL,
    "message" "text" DEFAULT ''::"text" NOT NULL,
    "audit_detail" "jsonb" DEFAULT '{}'::"jsonb" NOT NULL,
    "reason" "text" DEFAULT ''::"text" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "version_transactions_intent_type_valid" CHECK (("intent_type" = ANY (ARRAY['operation'::"text", 'submission'::"text", 'rollback'::"text", 'resolution'::"text"]))),
    CONSTRAINT "version_transactions_status_valid" CHECK (("status" = ANY (ARRAY['received'::"text", 'validated'::"text", 'policy_selected'::"text", 'pending_manual_review'::"text", 'pending_agent_resolution'::"text", 'resolving'::"text", 'publish_attempt'::"text", 'committed'::"text", 'rejected'::"text", 'retryable_conflict'::"text"])))
);


ALTER TABLE "public"."version_transactions" OWNER TO "postgres";

--
-- Name: version_activity_feed; Type: VIEW; Schema: public; Owner: postgres
--

CREATE VIEW "public"."version_activity_feed" AS
 SELECT "al"."id" AS "audit_id",
    "al"."created_at" AS "event_at",
    "al"."project_id",
    "al"."action" AS "event_type",
    "al"."operator_type",
    "al"."operator_id",
    "al"."scope_path",
    "al"."source_channel",
    "al"."policy",
    "al"."status",
    "al"."canonical_commit_id",
    "al"."original_commit_id",
    "al"."project_view_commit_id",
    "al"."scope_view_commit_id",
    "al"."metadata" AS "audit_metadata",
    "vt"."id" AS "transaction_id",
    "vt"."status" AS "transaction_status",
    "vt"."intent_type",
    "vt"."actor" AS "transaction_actor",
    "vt"."base_commit_id",
    "vt"."client_commit_id",
    "vt"."proposed_tree_id",
    "vt"."current_head_at_start",
    "vt"."committed_commit_id",
    "vt"."reason" AS "transaction_reason",
    "mc"."pending_conflict_id",
    "mc"."status" AS "conflict_status",
    "mc"."resolver_actor",
    "mc"."resolver_kind",
    "mc"."resolution_commit_id",
    "mc"."changed_paths" AS "conflict_changed_paths"
   FROM (("public"."audit_logs" "al"
     LEFT JOIN "public"."version_transactions" "vt" ON (("vt"."id" = "al"."transaction_id")))
     LEFT JOIN "public"."version_conflicts" "mc" ON (("mc"."transaction_id" = "vt"."id")));


ALTER VIEW "public"."version_activity_feed" OWNER TO "postgres";

--
-- Name: VIEW "version_activity_feed"; Type: COMMENT; Schema: public; Owner: postgres
--

COMMENT ON VIEW "public"."version_activity_feed" IS 'V1 activity feed: one row per audit event, joined to the linked version_transactions row (if any) and mut_conflicts row (if any). Read-only; the engine writes to the three base tables, never here.';


--
-- Name: version_object_gc_candidates; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."version_object_gc_candidates" (
    "project_id" "text" NOT NULL,
    "object_id" "text" NOT NULL,
    "first_seen_at" timestamp with time zone NOT NULL,
    "last_seen_at" timestamp with time zone NOT NULL,
    CONSTRAINT "version_object_gc_candidates_object_id_check" CHECK (("object_id" ~ '^[0-9a-f]{40}$'::"text"))
);


ALTER TABLE "public"."version_object_gc_candidates" OWNER TO "postgres";

--
-- Name: version_object_gc_runs; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."version_object_gc_runs" (
    "id" bigint NOT NULL,
    "project_id" "text" NOT NULL,
    "dry_run" boolean NOT NULL,
    "total_objects" bigint DEFAULT 0 NOT NULL,
    "reachable_objects" bigint DEFAULT 0 NOT NULL,
    "unreachable_objects" bigint DEFAULT 0 NOT NULL,
    "eligible_objects" bigint DEFAULT 0 NOT NULL,
    "quarantined_objects" bigint DEFAULT 0 NOT NULL,
    "deleted_objects" bigint DEFAULT 0 NOT NULL,
    "unreachable_bytes" bigint DEFAULT 0 NOT NULL,
    "eligible_bytes" bigint DEFAULT 0 NOT NULL,
    "deleted_bytes" bigint DEFAULT 0 NOT NULL,
    "sweep_skipped_for_safety" boolean DEFAULT false NOT NULL,
    "errors" "jsonb" DEFAULT '[]'::"jsonb" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."version_object_gc_runs" OWNER TO "postgres";

--
-- Name: version_object_gc_runs_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE "public"."version_object_gc_runs" ALTER COLUMN "id" ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME "public"."version_object_gc_runs_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: version_project_root_integrity_incidents; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."version_project_root_integrity_incidents" (
    "project_id" "text" NOT NULL,
    "root_hash" "text" NOT NULL,
    "status" "text" NOT NULL,
    "reason" "text" NOT NULL,
    "first_detected_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "last_detected_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "marked_by" "text" DEFAULT 'system:root-integrity-repair'::"text" NOT NULL,
    CONSTRAINT "version_project_root_integrity_incidents_root_hash_check" CHECK (("root_hash" ~ '^[0-9a-f]{40}$'::"text")),
    CONSTRAINT "version_project_root_integrity_incidents_status_check" CHECK (("status" = 'irrecoverable'::"text"))
);


ALTER TABLE "public"."version_project_root_integrity_incidents" OWNER TO "postgres";

--
-- Name: version_refs; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."version_refs" (
    "id" "text" DEFAULT ("gen_random_uuid"())::"text" NOT NULL,
    "project_id" "text" NOT NULL,
    "scope_path" "text" DEFAULT ''::"text" NOT NULL,
    "ref_name" "text" NOT NULL,
    "ref_type" "text" NOT NULL,
    "commit_id" "text" NOT NULL,
    "created_by" "text" DEFAULT ''::"text" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "version_refs_commit_id_hex" CHECK (("commit_id" ~ '^[0-9a-f]{40}$'::"text")),
    CONSTRAINT "version_refs_ref_name_shape" CHECK (((("ref_name" ~~ 'refs/heads/%'::"text") OR ("ref_name" ~~ 'refs/tags/%'::"text")) AND ("ref_name" <> 'refs/heads/main'::"text"))),
    CONSTRAINT "version_refs_ref_type_check" CHECK (("ref_type" = ANY (ARRAY['branch'::"text", 'tag'::"text"]))),
    CONSTRAINT "version_refs_scope_path_canonical" CHECK ((("scope_path" = ''::"text") OR (("scope_path" !~~ '/%'::"text") AND ("scope_path" !~~ '%/'::"text") AND ("scope_path" !~~ '%//%'::"text"))))
);


ALTER TABLE "public"."version_refs" OWNER TO "postgres";

--
-- Name: version_text_index; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."version_text_index" (
    "id" bigint NOT NULL,
    "project_id" "text" NOT NULL,
    "scope_path" "text" DEFAULT ''::"text" NOT NULL,
    "file_path" "text" NOT NULL,
    "content_hash" "text" NOT NULL,
    "chunk_idx" integer NOT NULL,
    "line_start" integer NOT NULL,
    "text" "text" NOT NULL,
    "indexed_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "version_text_index_chunk_idx_nonneg" CHECK (("chunk_idx" >= 0)),
    CONSTRAINT "version_text_index_line_start_pos" CHECK (("line_start" >= 1))
);


ALTER TABLE "public"."version_text_index" OWNER TO "postgres";

--
-- Name: version_text_index_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

CREATE SEQUENCE "public"."version_text_index_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "public"."version_text_index_id_seq" OWNER TO "postgres";

--
-- Name: version_text_index_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: postgres
--

ALTER SEQUENCE "public"."version_text_index_id_seq" OWNED BY "public"."version_text_index"."id";


--
-- Name: version_text_index_state; Type: TABLE; Schema: public; Owner: postgres
--

CREATE TABLE "public"."version_text_index_state" (
    "project_id" "text" NOT NULL,
    "scope_path" "text" DEFAULT ''::"text" NOT NULL,
    "indexed_commit_id" "text" NOT NULL,
    "indexed_at" timestamp with time zone DEFAULT "now"() NOT NULL
);


ALTER TABLE "public"."version_text_index_state" OWNER TO "postgres";

--
-- Name: version_transactions_id_seq; Type: SEQUENCE; Schema: public; Owner: postgres
--

ALTER TABLE "public"."version_transactions" ALTER COLUMN "id" ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME "public"."version_transactions_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: audit_logs id; Type: DEFAULT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."audit_logs" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."audit_logs_id_seq"'::"regclass");


--
-- Name: scope_sync_events id; Type: DEFAULT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."scope_sync_events" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."scope_sync_events_id_seq"'::"regclass");


--
-- Name: sync_changelog id; Type: DEFAULT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."sync_changelog" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."sync_changelog_id_seq"'::"regclass");


--
-- Name: version_text_index id; Type: DEFAULT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."version_text_index" ALTER COLUMN "id" SET DEFAULT "nextval"('"public"."version_text_index_id_seq"'::"regclass");


--
-- Name: access_logs access_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."access_logs"
    ADD CONSTRAINT "access_logs_pkey" PRIMARY KEY ("id");


--
-- Name: access_surface_credentials access_surface_credentials_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."access_surface_credentials"
    ADD CONSTRAINT "access_surface_credentials_pkey" PRIMARY KEY ("id");


--
-- Name: access_surface_policies access_surface_policies_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."access_surface_policies"
    ADD CONSTRAINT "access_surface_policies_pkey" PRIMARY KEY ("access_surface_id");


--
-- Name: access_surfaces access_surfaces_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."access_surfaces"
    ADD CONSTRAINT "access_surfaces_pkey" PRIMARY KEY ("id");


--
-- Name: agent_execution_logs agent_execution_log_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."agent_execution_logs"
    ADD CONSTRAINT "agent_execution_log_pkey" PRIMARY KEY ("id");


--
-- Name: agent_logs agent_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."agent_logs"
    ADD CONSTRAINT "agent_logs_pkey" PRIMARY KEY ("id");


--
-- Name: agent_profiles agent_profiles_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."agent_profiles"
    ADD CONSTRAINT "agent_profiles_pkey" PRIMARY KEY ("access_point_id");


--
-- Name: access_tools agent_tool_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."access_tools"
    ADD CONSTRAINT "agent_tool_pkey" PRIMARY KEY ("id");


--
-- Name: api_keys api_keys_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."api_keys"
    ADD CONSTRAINT "api_keys_pkey" PRIMARY KEY ("id");


--
-- Name: api_keys api_keys_prefix_unique; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."api_keys"
    ADD CONSTRAINT "api_keys_prefix_unique" UNIQUE ("prefix");


--
-- Name: audit_logs audit_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."audit_logs"
    ADD CONSTRAINT "audit_logs_pkey" PRIMARY KEY ("id");


--
-- Name: bookmarks bookmarks_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."bookmarks"
    ADD CONSTRAINT "bookmarks_pkey" PRIMARY KEY ("id");


--
-- Name: bookmarks bookmarks_project_id_path_type_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."bookmarks"
    ADD CONSTRAINT "bookmarks_project_id_path_type_key" UNIQUE ("project_id", "path", "type");


--
-- Name: chat_messages chat_messages_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."chat_messages"
    ADD CONSTRAINT "chat_messages_pkey" PRIMARY KEY ("id");


--
-- Name: chat_sessions chat_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."chat_sessions"
    ADD CONSTRAINT "chat_sessions_pkey" PRIMARY KEY ("id");


--
-- Name: chunks chunks_node_id_json_pointer_content_hash_chunk_index_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."chunks"
    ADD CONSTRAINT "chunks_node_id_json_pointer_content_hash_chunk_index_key" UNIQUE ("path", "json_pointer", "content_hash", "chunk_index");


--
-- Name: chunks chunks_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."chunks"
    ADD CONSTRAINT "chunks_pkey" PRIMARY KEY ("id");


--
-- Name: access_tools connection_tool_connection_id_tool_id_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."access_tools"
    ADD CONSTRAINT "connection_tool_connection_id_tool_id_key" UNIQUE ("access_point_id", "tool_id");


--
-- Name: connections connections_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."connections"
    ADD CONSTRAINT "connections_pkey" PRIMARY KEY ("id");


--
-- Name: connectors connectors_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."connectors"
    ADD CONSTRAINT "connectors_pkey" PRIMARY KEY ("id");


--
-- Name: context_publishes context_publish_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."context_publishes"
    ADD CONSTRAINT "context_publish_pkey" PRIMARY KEY ("id");


--
-- Name: context_publishes context_publish_publish_key_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."context_publishes"
    ADD CONSTRAINT "context_publish_publish_key_key" UNIQUE ("publish_key");


--
-- Name: etl_rules etl_rule_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."etl_rules"
    ADD CONSTRAINT "etl_rule_pkey" PRIMARY KEY ("id");


--
-- Name: fs_path_index fs_path_index_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."fs_path_index"
    ADD CONSTRAINT "fs_path_index_pkey" PRIMARY KEY ("id");


--
-- Name: fs_path_index fs_path_index_project_id_full_path_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."fs_path_index"
    ADD CONSTRAINT "fs_path_index_project_id_full_path_key" UNIQUE ("project_id", "full_path");


--
-- Name: git_credential_issue_operations git_credential_issue_operations_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."git_credential_issue_operations"
    ADD CONSTRAINT "git_credential_issue_operations_pkey" PRIMARY KEY ("actor_user_id", "operation_key");


--
-- Name: github_integrations github_integrations_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."github_integrations"
    ADD CONSTRAINT "github_integrations_pkey" PRIMARY KEY ("id");


--
-- Name: github_integrations github_integrations_project_id_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."github_integrations"
    ADD CONSTRAINT "github_integrations_project_id_key" UNIQUE ("project_id");


--
-- Name: github_sync_log github_sync_log_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."github_sync_log"
    ADD CONSTRAINT "github_sync_log_pkey" PRIMARY KEY ("id");


--
-- Name: import_jobs import_jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."import_jobs"
    ADD CONSTRAINT "import_jobs_pkey" PRIMARY KEY ("id");


--
-- Name: local_shadow_snapshots local_shadow_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."local_shadow_snapshots"
    ADD CONSTRAINT "local_shadow_snapshots_pkey" PRIMARY KEY ("id");


--
-- Name: local_shadow_snapshots local_shadow_snapshots_project_id_user_id_machine_id_ref_na_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."local_shadow_snapshots"
    ADD CONSTRAINT "local_shadow_snapshots_project_id_user_id_machine_id_ref_na_key" UNIQUE ("project_id", "user_id", "machine_id", "ref_name");


--
-- Name: migration_log migration_log_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."migration_log"
    ADD CONSTRAINT "migration_log_pkey" PRIMARY KEY ("name");


--
-- Name: version_commits mut_commits_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."version_commits"
    ADD CONSTRAINT "mut_commits_pkey" PRIMARY KEY ("id");


--
-- Name: version_commits mut_commits_project_commit_unique; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."version_commits"
    ADD CONSTRAINT "mut_commits_project_commit_unique" UNIQUE ("project_id", "commit_id");


--
-- Name: version_conflicts mut_conflicts_pending_conflict_id_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."version_conflicts"
    ADD CONSTRAINT "mut_conflicts_pending_conflict_id_key" UNIQUE ("pending_conflict_id");


--
-- Name: version_conflicts mut_conflicts_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."version_conflicts"
    ADD CONSTRAINT "mut_conflicts_pkey" PRIMARY KEY ("id");


--
-- Name: version_object_locations mut_object_locations_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."version_object_locations"
    ADD CONSTRAINT "mut_object_locations_pkey" PRIMARY KEY ("project_id", "object_id");


--
-- Name: version_scope_state mut_scope_state_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."version_scope_state"
    ADD CONSTRAINT "mut_scope_state_pkey" PRIMARY KEY ("id");


--
-- Name: version_scope_state mut_scope_state_project_id_scope_path_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."version_scope_state"
    ADD CONSTRAINT "mut_scope_state_project_id_scope_path_key" UNIQUE ("project_id", "scope_path");


--
-- Name: version_view_commits mut_version_index_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."version_view_commits"
    ADD CONSTRAINT "mut_version_index_pkey" PRIMARY KEY ("id");


--
-- Name: version_view_commits mut_version_index_project_id_source_commit_id_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."version_view_commits"
    ADD CONSTRAINT "mut_version_index_project_id_source_commit_id_key" UNIQUE ("project_id", "source_commit_id");


--
-- Name: version_outbox mut_version_outbox_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."version_outbox"
    ADD CONSTRAINT "mut_version_outbox_pkey" PRIMARY KEY ("id");


--
-- Name: oauth_connections oauth_connection_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."oauth_connections"
    ADD CONSTRAINT "oauth_connection_pkey" PRIMARY KEY ("id");


--
-- Name: oauth_states oauth_states_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."oauth_states"
    ADD CONSTRAINT "oauth_states_pkey" PRIMARY KEY ("state");


--
-- Name: org_invitations org_invitations_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."org_invitations"
    ADD CONSTRAINT "org_invitations_pkey" PRIMARY KEY ("id");


--
-- Name: org_invitations org_invitations_token_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."org_invitations"
    ADD CONSTRAINT "org_invitations_token_key" UNIQUE ("token");


--
-- Name: org_members org_members_org_id_user_id_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."org_members"
    ADD CONSTRAINT "org_members_org_id_user_id_key" UNIQUE ("org_id", "user_id");


--
-- Name: org_members org_members_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."org_members"
    ADD CONSTRAINT "org_members_pkey" PRIMARY KEY ("id");


--
-- Name: organization_billing_operations organization_billing_operations_org_id_idempotency_key_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."organization_billing_operations"
    ADD CONSTRAINT "organization_billing_operations_org_id_idempotency_key_key" UNIQUE ("org_id", "idempotency_key");


--
-- Name: organization_billing_operations organization_billing_operations_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."organization_billing_operations"
    ADD CONSTRAINT "organization_billing_operations_pkey" PRIMARY KEY ("id");


--
-- Name: organization_entitlement_events organization_entitlement_events_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."organization_entitlement_events"
    ADD CONSTRAINT "organization_entitlement_events_pkey" PRIMARY KEY ("id");


--
-- Name: organization_entitlements organization_entitlements_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."organization_entitlements"
    ADD CONSTRAINT "organization_entitlements_pkey" PRIMARY KEY ("org_id");


--
-- Name: organization_usage_counters organization_usage_counters_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."organization_usage_counters"
    ADD CONSTRAINT "organization_usage_counters_pkey" PRIMARY KEY ("org_id", "metric");


--
-- Name: organization_usage_events organization_usage_events_org_id_metric_idempotency_key_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."organization_usage_events"
    ADD CONSTRAINT "organization_usage_events_org_id_metric_idempotency_key_key" UNIQUE ("org_id", "metric", "idempotency_key");


--
-- Name: organization_usage_events organization_usage_events_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."organization_usage_events"
    ADD CONSTRAINT "organization_usage_events_pkey" PRIMARY KEY ("id");


--
-- Name: organizations organizations_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."organizations"
    ADD CONSTRAINT "organizations_pkey" PRIMARY KEY ("id");


--
-- Name: organizations organizations_slug_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."organizations"
    ADD CONSTRAINT "organizations_slug_key" UNIQUE ("slug");


--
-- Name: profiles profiles_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."profiles"
    ADD CONSTRAINT "profiles_pkey" PRIMARY KEY ("user_id");


--
-- Name: project_create_operations project_create_operations_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."project_create_operations"
    ADD CONSTRAINT "project_create_operations_pkey" PRIMARY KEY ("actor_user_id", "operation_key");


--
-- Name: project_deletion_jobs project_deletion_jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."project_deletion_jobs"
    ADD CONSTRAINT "project_deletion_jobs_pkey" PRIMARY KEY ("id");


--
-- Name: project_deletion_jobs project_deletion_jobs_project_id_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."project_deletion_jobs"
    ADD CONSTRAINT "project_deletion_jobs_project_id_key" UNIQUE ("project_id");


--
-- Name: project_members project_members_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."project_members"
    ADD CONSTRAINT "project_members_pkey" PRIMARY KEY ("id");


--
-- Name: project_members project_members_project_id_user_id_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."project_members"
    ADD CONSTRAINT "project_members_project_id_user_id_key" UNIQUE ("project_id", "user_id");


--
-- Name: projects project_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."projects"
    ADD CONSTRAINT "project_pkey" PRIMARY KEY ("id");


--
-- Name: project_storage_inventory_batches project_storage_inventory_batches_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."project_storage_inventory_batches"
    ADD CONSTRAINT "project_storage_inventory_batches_pkey" PRIMARY KEY ("batch_key");


--
-- Name: project_storage_inventory_state project_storage_inventory_state_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."project_storage_inventory_state"
    ADD CONSTRAINT "project_storage_inventory_state_pkey" PRIMARY KEY ("singleton");


--
-- Name: project_storage_orphan_prefixes project_storage_orphan_prefixes_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."project_storage_orphan_prefixes"
    ADD CONSTRAINT "project_storage_orphan_prefixes_pkey" PRIMARY KEY ("project_id", "principal");


--
-- Name: project_storage_principals project_storage_principals_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."project_storage_principals"
    ADD CONSTRAINT "project_storage_principals_pkey" PRIMARY KEY ("project_id", "principal");


--
-- Name: project_write_leases project_write_leases_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."project_write_leases"
    ADD CONSTRAINT "project_write_leases_pkey" PRIMARY KEY ("id");


--
-- Name: projects projects_share_token_unique; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."projects"
    ADD CONSTRAINT "projects_share_token_unique" UNIQUE ("share_token");


--
-- Name: repository_scopes repository_scopes_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."repository_scopes"
    ADD CONSTRAINT "repository_scopes_pkey" PRIMARY KEY ("id");


--
-- Name: repository_scopes repository_scopes_project_id_path_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."repository_scopes"
    ADD CONSTRAINT "repository_scopes_project_id_path_key" UNIQUE ("project_id", "path");


--
-- Name: runtime_billing_runs runtime_billing_runs_org_id_idempotency_key_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."runtime_billing_runs"
    ADD CONSTRAINT "runtime_billing_runs_org_id_idempotency_key_key" UNIQUE ("org_id", "idempotency_key");


--
-- Name: runtime_billing_runs runtime_billing_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."runtime_billing_runs"
    ADD CONSTRAINT "runtime_billing_runs_pkey" PRIMARY KEY ("run_id");


--
-- Name: runtime_billing_runs runtime_billing_runs_reservation_id_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."runtime_billing_runs"
    ADD CONSTRAINT "runtime_billing_runs_reservation_id_key" UNIQUE ("reservation_id");


--
-- Name: sandbox_endpoints sandbox_endpoints_access_key_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."sandbox_endpoints"
    ADD CONSTRAINT "sandbox_endpoints_access_key_key" UNIQUE ("access_key");


--
-- Name: sandbox_endpoints sandbox_endpoints_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."sandbox_endpoints"
    ADD CONSTRAINT "sandbox_endpoints_pkey" PRIMARY KEY ("id");


--
-- Name: sandbox_execution_sessions sandbox_execution_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."sandbox_execution_sessions"
    ADD CONSTRAINT "sandbox_execution_sessions_pkey" PRIMARY KEY ("session_id");


--
-- Name: scope_sandbox_sessions scope_sandbox_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."scope_sandbox_sessions"
    ADD CONSTRAINT "scope_sandbox_sessions_pkey" PRIMARY KEY ("scope_id");


--
-- Name: scope_sync_events scope_sync_events_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."scope_sync_events"
    ADD CONSTRAINT "scope_sync_events_pkey" PRIMARY KEY ("id");


--
-- Name: scope_sync_settings scope_sync_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."scope_sync_settings"
    ADD CONSTRAINT "scope_sync_settings_pkey" PRIMARY KEY ("project_id", "scope_id");


--
-- Name: subscriptions subscriptions_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."subscriptions"
    ADD CONSTRAINT "subscriptions_pkey" PRIMARY KEY ("id");


--
-- Name: sync_changelog sync_changelog_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."sync_changelog"
    ADD CONSTRAINT "sync_changelog_pkey" PRIMARY KEY ("id");


--
-- Name: connector_runs sync_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."connector_runs"
    ADD CONSTRAINT "sync_runs_pkey" PRIMARY KEY ("id");


--
-- Name: sync_runs sync_runs_pkey1; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."sync_runs"
    ADD CONSTRAINT "sync_runs_pkey1" PRIMARY KEY ("id");


--
-- Name: sync_state sync_state_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."sync_state"
    ADD CONSTRAINT "sync_state_pkey" PRIMARY KEY ("access_point_id");


--
-- Name: tables tables_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."tables"
    ADD CONSTRAINT "tables_pkey" PRIMARY KEY ("id");


--
-- Name: tools tool_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."tools"
    ADD CONSTRAINT "tool_pkey" PRIMARY KEY ("id");


--
-- Name: upload_items upload_items_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."upload_items"
    ADD CONSTRAINT "upload_items_pkey" PRIMARY KEY ("id");


--
-- Name: upload_jobs upload_jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."upload_jobs"
    ADD CONSTRAINT "upload_jobs_pkey" PRIMARY KEY ("id");


--
-- Name: uploads uploads_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."uploads"
    ADD CONSTRAINT "uploads_pkey" PRIMARY KEY ("id");


--
-- Name: version_object_gc_candidates version_object_gc_candidates_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."version_object_gc_candidates"
    ADD CONSTRAINT "version_object_gc_candidates_pkey" PRIMARY KEY ("project_id", "object_id");


--
-- Name: version_object_gc_runs version_object_gc_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."version_object_gc_runs"
    ADD CONSTRAINT "version_object_gc_runs_pkey" PRIMARY KEY ("id");


--
-- Name: version_project_root_integrity_incidents version_project_root_integrity_incidents_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."version_project_root_integrity_incidents"
    ADD CONSTRAINT "version_project_root_integrity_incidents_pkey" PRIMARY KEY ("project_id");


--
-- Name: version_refs version_refs_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."version_refs"
    ADD CONSTRAINT "version_refs_pkey" PRIMARY KEY ("id");


--
-- Name: version_refs version_refs_project_id_scope_path_ref_name_key; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."version_refs"
    ADD CONSTRAINT "version_refs_project_id_scope_path_ref_name_key" UNIQUE ("project_id", "scope_path", "ref_name");


--
-- Name: version_text_index version_text_index_pk_natural; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."version_text_index"
    ADD CONSTRAINT "version_text_index_pk_natural" UNIQUE ("project_id", "content_hash", "chunk_idx");


--
-- Name: version_text_index version_text_index_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."version_text_index"
    ADD CONSTRAINT "version_text_index_pkey" PRIMARY KEY ("id");


--
-- Name: version_text_index_state version_text_index_state_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."version_text_index_state"
    ADD CONSTRAINT "version_text_index_state_pkey" PRIMARY KEY ("project_id", "scope_path");


--
-- Name: version_transactions version_transactions_pkey; Type: CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."version_transactions"
    ADD CONSTRAINT "version_transactions_pkey" PRIMARY KEY ("id");


--
-- Name: git_credential_issue_operations_project_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "git_credential_issue_operations_project_idx" ON "public"."git_credential_issue_operations" USING "btree" ("project_id");


--
-- Name: idx_access_logs_created_at; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_access_logs_created_at" ON "public"."access_logs" USING "btree" ("created_at");


--
-- Name: idx_access_logs_project_created_at; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_access_logs_project_created_at" ON "public"."access_logs" USING "btree" ("project_id", "created_at" DESC);


--
-- Name: idx_access_logs_project_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_access_logs_project_id" ON "public"."access_logs" USING "btree" ("project_id");


--
-- Name: idx_access_surface_credentials_active_hash; Type: INDEX; Schema: public; Owner: postgres
--

CREATE UNIQUE INDEX "idx_access_surface_credentials_active_hash" ON "public"."access_surface_credentials" USING "btree" ("key_hash") WHERE ("status" = 'active'::"text");


--
-- Name: idx_access_surface_credentials_project; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_access_surface_credentials_project" ON "public"."access_surface_credentials" USING "btree" ("project_id", "created_at" DESC);


--
-- Name: idx_access_surface_credentials_surface; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_access_surface_credentials_surface" ON "public"."access_surface_credentials" USING "btree" ("access_surface_id", "status");


--
-- Name: idx_access_surface_credentials_user; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_access_surface_credentials_user" ON "public"."access_surface_credentials" USING "btree" ("user_id", "project_id", "status") WHERE ("user_id" IS NOT NULL);


--
-- Name: idx_access_surfaces_kind; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_access_surfaces_kind" ON "public"."access_surfaces" USING "btree" ("project_id", "kind");


--
-- Name: idx_access_surfaces_project; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_access_surfaces_project" ON "public"."access_surfaces" USING "btree" ("project_id", "created_at" DESC);


--
-- Name: idx_access_surfaces_scope; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_access_surfaces_scope" ON "public"."access_surfaces" USING "btree" ("scope_id");


--
-- Name: idx_access_surfaces_target; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_access_surfaces_target" ON "public"."access_surfaces" USING "btree" ("project_id", "scope_id", "status", "kind");


--
-- Name: idx_access_tools_surface_tool_unique; Type: INDEX; Schema: public; Owner: postgres
--

CREATE UNIQUE INDEX "idx_access_tools_surface_tool_unique" ON "public"."access_tools" USING "btree" ("access_point_id", "tool_id");


--
-- Name: idx_agent_execution_log_agent_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_agent_execution_log_agent_id" ON "public"."agent_execution_logs" USING "btree" ("agent_id");


--
-- Name: idx_agent_logs_agent_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_agent_logs_agent_id" ON "public"."agent_logs" USING "btree" ("agent_id");


--
-- Name: idx_agent_logs_created_at; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_agent_logs_created_at" ON "public"."agent_logs" USING "btree" ("created_at");


--
-- Name: idx_agent_profiles_model; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_agent_profiles_model" ON "public"."agent_profiles" USING "btree" ("model");


--
-- Name: idx_api_keys_user; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_api_keys_user" ON "public"."api_keys" USING "btree" ("user_id", "created_at" DESC);


--
-- Name: idx_audit_logs_action; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_audit_logs_action" ON "public"."audit_logs" USING "btree" ("action", "created_at" DESC);


--
-- Name: idx_audit_logs_canonical_commit; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_audit_logs_canonical_commit" ON "public"."audit_logs" USING "btree" ("project_id", "canonical_commit_id") WHERE ("canonical_commit_id" IS NOT NULL);


--
-- Name: idx_audit_logs_created_at; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_audit_logs_created_at" ON "public"."audit_logs" USING "btree" ("created_at" DESC);


--
-- Name: idx_audit_logs_node_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_audit_logs_node_id" ON "public"."audit_logs" USING "btree" ("path", "created_at" DESC);


--
-- Name: idx_audit_logs_operator; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_audit_logs_operator" ON "public"."audit_logs" USING "btree" ("operator_type", "operator_id", "created_at" DESC);


--
-- Name: idx_audit_logs_project_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_audit_logs_project_id" ON "public"."audit_logs" USING "btree" ("project_id", "created_at" DESC) WHERE ("project_id" IS NOT NULL);


--
-- Name: idx_audit_logs_scope_channel; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_audit_logs_scope_channel" ON "public"."audit_logs" USING "btree" ("project_id", "scope_path", "source_channel", "created_at" DESC);


--
-- Name: idx_audit_logs_transaction; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_audit_logs_transaction" ON "public"."audit_logs" USING "btree" ("transaction_id") WHERE ("transaction_id" IS NOT NULL);


--
-- Name: idx_chat_messages_session_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_chat_messages_session_id" ON "public"."chat_messages" USING "btree" ("session_id");


--
-- Name: idx_chat_sessions_agent_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_chat_sessions_agent_id" ON "public"."chat_sessions" USING "btree" ("agent_id");


--
-- Name: idx_chat_sessions_user_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_chat_sessions_user_id" ON "public"."chat_sessions" USING "btree" ("user_id");


--
-- Name: idx_chunks_node_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_chunks_node_id" ON "public"."chunks" USING "btree" ("path");


--
-- Name: idx_chunks_path_pointer; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_chunks_path_pointer" ON "public"."chunks" USING "btree" ("path", "json_pointer");


--
-- Name: idx_connections_external_resource; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_connections_external_resource" ON "public"."connections" USING "btree" ("project_id", "provider", "external_resource_id") WHERE ("external_resource_id" IS NOT NULL);


--
-- Name: idx_connections_oauth; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_connections_oauth" ON "public"."connections" USING "btree" ("oauth_connection_id") WHERE ("oauth_connection_id" IS NOT NULL);


--
-- Name: idx_connections_project; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_connections_project" ON "public"."connections" USING "btree" ("project_id", "created_at" DESC);


--
-- Name: idx_connections_project_target_path; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_connections_project_target_path" ON "public"."connections" USING "btree" ("project_id", "target_path") WHERE ("target_path" IS NOT NULL);


--
-- Name: idx_connections_provider_project; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_connections_provider_project" ON "public"."connections" USING "btree" ("project_id", "provider");


--
-- Name: idx_connections_scope; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_connections_scope" ON "public"."connections" USING "btree" ("scope_id");


--
-- Name: idx_connections_status; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_connections_status" ON "public"."connections" USING "btree" ("project_id", "status");


--
-- Name: idx_connector_runs_started_at; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_connector_runs_started_at" ON "public"."connector_runs" USING "btree" ("started_at" DESC);


--
-- Name: idx_connectors_builtin_one_per_scope; Type: INDEX; Schema: public; Owner: postgres
--

CREATE UNIQUE INDEX "idx_connectors_builtin_one_per_scope" ON "public"."connectors" USING "btree" ("scope_id", "provider") WHERE ("provider" = ANY (ARRAY['cli'::"text", 'agent'::"text"]));


--
-- Name: idx_connectors_oauth; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_connectors_oauth" ON "public"."connectors" USING "btree" ("oauth_connection_id") WHERE ("oauth_connection_id" IS NOT NULL);


--
-- Name: idx_connectors_project; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_connectors_project" ON "public"."connectors" USING "btree" ("project_id");


--
-- Name: idx_connectors_provider_pid; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_connectors_provider_pid" ON "public"."connectors" USING "btree" ("project_id", "provider");


--
-- Name: idx_connectors_scope; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_connectors_scope" ON "public"."connectors" USING "btree" ("scope_id");


--
-- Name: idx_etl_rule_org; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_etl_rule_org" ON "public"."etl_rules" USING "btree" ("org_id");


--
-- Name: idx_fs_path_index_path_trgm; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_fs_path_index_path_trgm" ON "public"."fs_path_index" USING "gin" ("full_path" "public"."gin_trgm_ops");


--
-- Name: idx_fs_path_index_project_scope; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_fs_path_index_project_scope" ON "public"."fs_path_index" USING "btree" ("project_id", "scope_path", "full_path");


--
-- Name: idx_fs_path_index_recent; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_fs_path_index_recent" ON "public"."fs_path_index" USING "btree" ("project_id", "last_updated_at" DESC);


--
-- Name: idx_github_integrations_oauth_connection; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_github_integrations_oauth_connection" ON "public"."github_integrations" USING "btree" ("oauth_connection_id") WHERE ("oauth_connection_id" IS NOT NULL);


--
-- Name: idx_github_integrations_repo_coords; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_github_integrations_repo_coords" ON "public"."github_integrations" USING "btree" ("github_repo_owner", "github_repo_name");


--
-- Name: idx_github_sync_log_dedupe_lookup; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_github_sync_log_dedupe_lookup" ON "public"."github_sync_log" USING "btree" ("integration_id", "direction", "git_sha") WHERE ("git_sha" IS NOT NULL);


--
-- Name: idx_github_sync_log_integration_recent; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_github_sync_log_integration_recent" ON "public"."github_sync_log" USING "btree" ("integration_id", "created_at" DESC);


--
-- Name: idx_import_jobs_created_by; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_import_jobs_created_by" ON "public"."import_jobs" USING "btree" ("created_by", "created_at" DESC);


--
-- Name: idx_import_jobs_idempotency; Type: INDEX; Schema: public; Owner: postgres
--

CREATE UNIQUE INDEX "idx_import_jobs_idempotency" ON "public"."import_jobs" USING "btree" ("project_id", "provider", "idempotency_key") WHERE ("idempotency_key" IS NOT NULL);


--
-- Name: idx_import_jobs_project_active; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_import_jobs_project_active" ON "public"."import_jobs" USING "btree" ("project_id", "status", "created_at" DESC) WHERE ("status" = ANY (ARRAY['queued'::"text", 'running'::"text"]));


--
-- Name: idx_import_jobs_project_created; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_import_jobs_project_created" ON "public"."import_jobs" USING "btree" ("project_id", "created_at" DESC);


--
-- Name: idx_mut_commits_created_at; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_mut_commits_created_at" ON "public"."version_commits" USING "btree" ("created_at" DESC);


--
-- Name: idx_mut_commits_project_linear; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_mut_commits_project_linear" ON "public"."version_commits" USING "btree" ("project_id", "created_at" DESC, "commit_id" DESC);


--
-- Name: idx_mut_commits_project_scope_linear; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_mut_commits_project_scope_linear" ON "public"."version_commits" USING "btree" ("project_id", "scope_path", "created_at" DESC, "commit_id" DESC);


--
-- Name: idx_mut_commits_who; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_mut_commits_who" ON "public"."version_commits" USING "btree" ("who", "created_at" DESC);


--
-- Name: idx_mut_conflicts_project_status; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_mut_conflicts_project_status" ON "public"."version_conflicts" USING "btree" ("project_id", "status", "created_at" DESC);


--
-- Name: idx_mut_conflicts_scope; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_mut_conflicts_scope" ON "public"."version_conflicts" USING "btree" ("project_id", "scope_path", "status");


--
-- Name: idx_mut_conflicts_transaction; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_mut_conflicts_transaction" ON "public"."version_conflicts" USING "btree" ("transaction_id") WHERE ("transaction_id" IS NOT NULL);


--
-- Name: idx_mut_object_locations_pack; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_mut_object_locations_pack" ON "public"."version_object_locations" USING "btree" ("project_id", "pack_key");


--
-- Name: idx_mut_scope_state_project_scope; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_mut_scope_state_project_scope" ON "public"."version_scope_state" USING "btree" ("project_id", "scope_path");


--
-- Name: idx_mut_version_index_project_created; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_mut_version_index_project_created" ON "public"."version_view_commits" USING "btree" ("project_id", "created_at" DESC, "id" DESC);


--
-- Name: idx_mut_version_index_project_view_commit; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_mut_version_index_project_view_commit" ON "public"."version_view_commits" USING "btree" ("project_id", "project_view_commit_id");


--
-- Name: idx_mut_version_outbox_claimable; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_mut_version_outbox_claimable" ON "public"."version_outbox" USING "btree" ("locked_at", "created_at", "id") WHERE ("processed_at" IS NULL);


--
-- Name: idx_mut_version_outbox_event; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_mut_version_outbox_event" ON "public"."version_outbox" USING "btree" ("event_type", "processed_at") WHERE ("processed_at" IS NULL);


--
-- Name: idx_mut_version_outbox_unprocessed; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_mut_version_outbox_unprocessed" ON "public"."version_outbox" USING "btree" ("created_at", "id") WHERE ("processed_at" IS NULL);


--
-- Name: idx_oauth_connection_provider; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_oauth_connection_provider" ON "public"."oauth_connections" USING "btree" ("provider");


--
-- Name: idx_oauth_connection_user_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_oauth_connection_user_id" ON "public"."oauth_connections" USING "btree" ("user_id");


--
-- Name: idx_oauth_states_expires; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_oauth_states_expires" ON "public"."oauth_states" USING "btree" ("expires_at");


--
-- Name: idx_oauth_states_user_provider; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_oauth_states_user_provider" ON "public"."oauth_states" USING "btree" ("user_id", "provider");


--
-- Name: idx_org_members_org; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_org_members_org" ON "public"."org_members" USING "btree" ("org_id");


--
-- Name: idx_org_members_user; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_org_members_user" ON "public"."org_members" USING "btree" ("user_id");


--
-- Name: idx_project_members_project; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_project_members_project" ON "public"."project_members" USING "btree" ("project_id");


--
-- Name: idx_project_members_user; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_project_members_user" ON "public"."project_members" USING "btree" ("user_id");


--
-- Name: idx_project_members_user_org_project; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_project_members_user_org_project" ON "public"."project_members" USING "btree" ("user_id", "org_id", "project_id");


--
-- Name: idx_project_org; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_project_org" ON "public"."projects" USING "btree" ("org_id");


--
-- Name: idx_projects_share_token_unique; Type: INDEX; Schema: public; Owner: postgres
--

CREATE UNIQUE INDEX "idx_projects_share_token_unique" ON "public"."projects" USING "btree" ("share_token");


--
-- Name: idx_repository_scopes_project_path; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_repository_scopes_project_path" ON "public"."repository_scopes" USING "btree" ("project_id", "path");


--
-- Name: idx_sandbox_endpoints_access_key; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_sandbox_endpoints_access_key" ON "public"."sandbox_endpoints" USING "btree" ("access_key");


--
-- Name: idx_sandbox_endpoints_node; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_sandbox_endpoints_node" ON "public"."sandbox_endpoints" USING "btree" ("node_id");


--
-- Name: idx_sandbox_endpoints_project; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_sandbox_endpoints_project" ON "public"."sandbox_endpoints" USING "btree" ("project_id");


--
-- Name: idx_sandbox_execution_sessions_provider_activity; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_sandbox_execution_sessions_provider_activity" ON "public"."sandbox_execution_sessions" USING "btree" ("provider", "last_activity");


--
-- Name: idx_scope_sandbox_sessions_project; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_scope_sandbox_sessions_project" ON "public"."scope_sandbox_sessions" USING "btree" ("project_id");


--
-- Name: idx_scope_sandbox_sessions_state_active; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_scope_sandbox_sessions_state_active" ON "public"."scope_sandbox_sessions" USING "btree" ("state", "last_active_at");


--
-- Name: idx_scope_sync_events_scope_cursor; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_scope_sync_events_scope_cursor" ON "public"."scope_sync_events" USING "btree" ("project_id", "scope_id", "id");


--
-- Name: idx_shadow_snapshots_machine; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_shadow_snapshots_machine" ON "public"."local_shadow_snapshots" USING "btree" ("project_id", "user_id", "machine_id");


--
-- Name: idx_shadow_snapshots_project_user; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_shadow_snapshots_project_user" ON "public"."local_shadow_snapshots" USING "btree" ("project_id", "user_id", "updated_at" DESC);


--
-- Name: idx_subscriptions_user_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_subscriptions_user_id" ON "public"."subscriptions" USING "btree" ("user_id");


--
-- Name: idx_sync_changelog_cleanup; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_sync_changelog_cleanup" ON "public"."sync_changelog" USING "btree" ("created_at");


--
-- Name: idx_sync_changelog_folder_seq; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_sync_changelog_folder_seq" ON "public"."sync_changelog" USING "btree" ("folder_id", "id") WHERE ("folder_id" IS NOT NULL);


--
-- Name: idx_sync_changelog_project_seq; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_sync_changelog_project_seq" ON "public"."sync_changelog" USING "btree" ("project_id", "id");


--
-- Name: idx_sync_runs_active_lease; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_sync_runs_active_lease" ON "public"."sync_runs" USING "btree" ("status", "lease_expires_at", "created_at") WHERE ("status" = ANY (ARRAY['queued'::"text", 'running'::"text"]));


--
-- Name: idx_sync_runs_connection_recent; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_sync_runs_connection_recent" ON "public"."sync_runs" USING "btree" ("connection_id", "created_at" DESC);


--
-- Name: idx_sync_runs_one_active_per_connection; Type: INDEX; Schema: public; Owner: postgres
--

CREATE UNIQUE INDEX "idx_sync_runs_one_active_per_connection" ON "public"."sync_runs" USING "btree" ("connection_id") WHERE ("status" = ANY (ARRAY['queued'::"text", 'running'::"text"]));


--
-- Name: idx_sync_runs_project_active; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_sync_runs_project_active" ON "public"."sync_runs" USING "btree" ("project_id", "status", "created_at" DESC) WHERE ("status" = ANY (ARRAY['queued'::"text", 'running'::"text"]));


--
-- Name: idx_sync_runs_project_recent; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_sync_runs_project_recent" ON "public"."sync_runs" USING "btree" ("project_id", "created_at" DESC);


--
-- Name: idx_sync_runs_sync_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_sync_runs_sync_id" ON "public"."connector_runs" USING "btree" ("connector_id");


--
-- Name: idx_sync_state_last_synced; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_sync_state_last_synced" ON "public"."sync_state" USING "btree" ("last_synced_at" DESC);


--
-- Name: idx_tables_created_by; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_tables_created_by" ON "public"."tables" USING "btree" ("created_by");


--
-- Name: idx_tables_project_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_tables_project_id" ON "public"."tables" USING "btree" ("project_id");


--
-- Name: idx_tool_node_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_tool_node_id" ON "public"."tools" USING "btree" ("path");


--
-- Name: idx_tool_org; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_tool_org" ON "public"."tools" USING "btree" ("org_id");


--
-- Name: idx_tool_project_id; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_tool_project_id" ON "public"."tools" USING "btree" ("project_id");


--
-- Name: idx_tools_path; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_tools_path" ON "public"."tools" USING "btree" ("path");


--
-- Name: idx_upload_items_job; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_upload_items_job" ON "public"."upload_items" USING "btree" ("upload_job_id", "relative_path");


--
-- Name: idx_upload_items_job_status; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_upload_items_job_status" ON "public"."upload_items" USING "btree" ("upload_job_id", "status");


--
-- Name: idx_upload_jobs_created_by; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_upload_jobs_created_by" ON "public"."upload_jobs" USING "btree" ("created_by", "created_at" DESC);


--
-- Name: idx_upload_jobs_project_active; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_upload_jobs_project_active" ON "public"."upload_jobs" USING "btree" ("project_id", "status", "created_at" DESC) WHERE ("status" = ANY (ARRAY['queued'::"text", 'running'::"text"]));


--
-- Name: idx_upload_jobs_project_created; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_upload_jobs_project_created" ON "public"."upload_jobs" USING "btree" ("project_id", "created_at" DESC);


--
-- Name: idx_uploads_created; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_uploads_created" ON "public"."uploads" USING "btree" ("created_at" DESC);


--
-- Name: idx_uploads_node; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_uploads_node" ON "public"."uploads" USING "btree" ("path") WHERE ("path" IS NOT NULL);


--
-- Name: idx_uploads_project; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_uploads_project" ON "public"."uploads" USING "btree" ("project_id");


--
-- Name: idx_uploads_status; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_uploads_status" ON "public"."uploads" USING "btree" ("status");


--
-- Name: idx_uploads_type; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_uploads_type" ON "public"."uploads" USING "btree" ("type");


--
-- Name: idx_version_object_gc_candidates_maturity; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_version_object_gc_candidates_maturity" ON "public"."version_object_gc_candidates" USING "btree" ("project_id", "first_seen_at");


--
-- Name: idx_version_object_gc_runs_project_created; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_version_object_gc_runs_project_created" ON "public"."version_object_gc_runs" USING "btree" ("project_id", "created_at" DESC);


--
-- Name: idx_version_project_root_integrity_incidents_status; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_version_project_root_integrity_incidents_status" ON "public"."version_project_root_integrity_incidents" USING "btree" ("status", "last_detected_at" DESC);


--
-- Name: idx_version_refs_commit; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_version_refs_commit" ON "public"."version_refs" USING "btree" ("project_id", "commit_id");


--
-- Name: idx_version_refs_project_scope; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_version_refs_project_scope" ON "public"."version_refs" USING "btree" ("project_id", "scope_path");


--
-- Name: idx_version_transactions_committed; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_version_transactions_committed" ON "public"."version_transactions" USING "btree" ("project_id", "committed_commit_id") WHERE ("committed_commit_id" <> ''::"text");


--
-- Name: idx_version_transactions_committed_commit; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_version_transactions_committed_commit" ON "public"."version_transactions" USING "btree" ("project_id", "committed_commit_id") WHERE ("committed_commit_id" <> ''::"text");


--
-- Name: idx_version_transactions_project_status; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_version_transactions_project_status" ON "public"."version_transactions" USING "btree" ("project_id", "status", "created_at" DESC);


--
-- Name: idx_vti_project_file_path; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_vti_project_file_path" ON "public"."version_text_index" USING "btree" ("project_id", "file_path");


--
-- Name: idx_vti_project_scope; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_vti_project_scope" ON "public"."version_text_index" USING "btree" ("project_id", "scope_path");


--
-- Name: idx_vti_state_project; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_vti_state_project" ON "public"."version_text_index_state" USING "btree" ("project_id");


--
-- Name: idx_vti_trgm; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "idx_vti_trgm" ON "public"."version_text_index" USING "gin" ("text" "public"."gin_trgm_ops");


--
-- Name: organization_billing_operations_org_created_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "organization_billing_operations_org_created_idx" ON "public"."organization_billing_operations" USING "btree" ("org_id", "created_at" DESC);


--
-- Name: organization_billing_operations_pending_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "organization_billing_operations_pending_idx" ON "public"."organization_billing_operations" USING "btree" ("status", "next_attempt_at") WHERE ("status" = ANY (ARRAY['pending'::"text", 'submitted'::"text", 'failed'::"text"]));


--
-- Name: organization_billing_operations_quote_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE UNIQUE INDEX "organization_billing_operations_quote_idx" ON "public"."organization_billing_operations" USING "btree" ("org_id", "quote_id") WHERE ("quote_id" IS NOT NULL);


--
-- Name: organization_entitlement_events_org_created_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "organization_entitlement_events_org_created_idx" ON "public"."organization_entitlement_events" USING "btree" ("org_id", "created_at" DESC);


--
-- Name: organization_entitlement_events_revision_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "organization_entitlement_events_revision_idx" ON "public"."organization_entitlement_events" USING "btree" ("org_id", "source_revision" DESC);


--
-- Name: organization_entitlement_events_source_event_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE UNIQUE INDEX "organization_entitlement_events_source_event_idx" ON "public"."organization_entitlement_events" USING "btree" ("source_event_id") WHERE ("source_event_id" IS NOT NULL);


--
-- Name: organization_entitlements_effective_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "organization_entitlements_effective_idx" ON "public"."organization_entitlements" USING "btree" ("effective_until", "source_revision");


--
-- Name: organization_entitlements_plan_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "organization_entitlements_plan_idx" ON "public"."organization_entitlements" USING "btree" ("plan_id");


--
-- Name: organization_entitlements_status_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "organization_entitlements_status_idx" ON "public"."organization_entitlements" USING "btree" ("status");


--
-- Name: organization_usage_counters_full_reconcile_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "organization_usage_counters_full_reconcile_idx" ON "public"."organization_usage_counters" USING "btree" ("full_reconciled_at", "reconciliation_claimed_at", "org_id") WHERE ("metric" = 'storage.logical_bytes'::"text");


--
-- Name: organization_usage_events_org_created_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "organization_usage_events_org_created_idx" ON "public"."organization_usage_events" USING "btree" ("org_id", "metric", "created_at" DESC);


--
-- Name: project_create_operations_initialization_claim_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "project_create_operations_initialization_claim_idx" ON "public"."project_create_operations" USING "btree" ("status", "initialization_available_at", "created_at") WHERE ("status" = 'initializing'::"text");


--
-- Name: project_create_operations_org_created_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "project_create_operations_org_created_idx" ON "public"."project_create_operations" USING "btree" ("org_id", "created_at" DESC);


--
-- Name: project_create_operations_project_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "project_create_operations_project_idx" ON "public"."project_create_operations" USING "btree" ("project_id");


--
-- Name: project_deletion_jobs_claim_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "project_deletion_jobs_claim_idx" ON "public"."project_deletion_jobs" USING "btree" ("status", "available_at", "created_at");


--
-- Name: project_write_leases_active_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "project_write_leases_active_idx" ON "public"."project_write_leases" USING "btree" ("project_id", "expires_at");


--
-- Name: projects_ready_org_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "projects_ready_org_idx" ON "public"."projects" USING "btree" ("org_id", "created_at" DESC) WHERE ("lifecycle_status" = 'ready'::"text");


--
-- Name: runtime_billing_runs_org_created_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "runtime_billing_runs_org_created_idx" ON "public"."runtime_billing_runs" USING "btree" ("org_id", "created_at" DESC);


--
-- Name: runtime_billing_runs_recovery_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "runtime_billing_runs_recovery_idx" ON "public"."runtime_billing_runs" USING "btree" ("status", "heartbeat_at", "expires_at") WHERE ("status" = ANY (ARRAY['reserved'::"text", 'running'::"text", 'settling'::"text", 'failed'::"text"]));


--
-- Name: runtime_billing_runs_retry_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "runtime_billing_runs_retry_idx" ON "public"."runtime_billing_runs" USING "btree" ("status", "updated_at") WHERE ("status" = ANY (ARRAY['settling'::"text", 'failed'::"text"]));


--
-- Name: sandbox_execution_sessions_project_idx; Type: INDEX; Schema: public; Owner: postgres
--

CREATE INDEX "sandbox_execution_sessions_project_idx" ON "public"."sandbox_execution_sessions" USING "btree" ("project_id") WHERE ("project_id" IS NOT NULL);


--
-- Name: uq_access_surfaces_builtin_target_kind; Type: INDEX; Schema: public; Owner: postgres
--

CREATE UNIQUE INDEX "uq_access_surfaces_builtin_target_kind" ON "public"."access_surfaces" USING "btree" ("project_id", "scope_id", "kind") NULLS NOT DISTINCT WHERE ("kind" = ANY (ARRAY['git_remote'::"text", 'cli'::"text", 'filesystem'::"text"]));


--
-- Name: uq_access_surfaces_id_project_org; Type: INDEX; Schema: public; Owner: postgres
--

CREATE UNIQUE INDEX "uq_access_surfaces_id_project_org" ON "public"."access_surfaces" USING "btree" ("id", "project_id", "org_id");


--
-- Name: uq_org_members_org_user; Type: INDEX; Schema: public; Owner: postgres
--

CREATE UNIQUE INDEX "uq_org_members_org_user" ON "public"."org_members" USING "btree" ("org_id", "user_id");


--
-- Name: uq_projects_id_org; Type: INDEX; Schema: public; Owner: postgres
--

CREATE UNIQUE INDEX "uq_projects_id_org" ON "public"."projects" USING "btree" ("id", "org_id");


--
-- Name: uq_repository_scopes_id_project; Type: INDEX; Schema: public; Owner: postgres
--

CREATE UNIQUE INDEX "uq_repository_scopes_id_project" ON "public"."repository_scopes" USING "btree" ("id", "project_id");


--
-- Name: github_sync_log github_sync_log_sync_version_columns; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "github_sync_log_sync_version_columns" BEFORE INSERT OR UPDATE OF "version_commit_id", "mut_commit_id" ON "public"."github_sync_log" FOR EACH ROW EXECUTE FUNCTION "public"."sync_github_version_columns"();


--
-- Name: project_deletion_jobs project_deletion_jobs_prepare_storage; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "project_deletion_jobs_prepare_storage" BEFORE INSERT ON "public"."project_deletion_jobs" FOR EACH ROW EXECUTE FUNCTION "public"."_prepare_project_deletion_job_storage"();


--
-- Name: projects projects_sync_version_columns; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "projects_sync_version_columns" BEFORE INSERT OR UPDATE OF "version_root_hash", "mut_root_hash" ON "public"."projects" FOR EACH ROW EXECUTE FUNCTION "public"."sync_project_version_columns"();


--
-- Name: access_surface_policies trg_access_surface_policies_updated_at; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "trg_access_surface_policies_updated_at" BEFORE UPDATE ON "public"."access_surface_policies" FOR EACH ROW EXECUTE FUNCTION "public"."_context_entrypoint_bump_updated_at"();


--
-- Name: access_surfaces trg_access_surfaces_updated_at; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "trg_access_surfaces_updated_at" BEFORE UPDATE ON "public"."access_surfaces" FOR EACH ROW EXECUTE FUNCTION "public"."_context_entrypoint_bump_updated_at"();


--
-- Name: organization_entitlements trg_confirm_correlated_billing_operations; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "trg_confirm_correlated_billing_operations" AFTER INSERT OR UPDATE OF "source_revision", "source_quote_id", "plan_id", "seat_quantity" ON "public"."organization_entitlements" FOR EACH ROW EXECUTE FUNCTION "public"."_confirm_correlated_billing_operations"();


--
-- Name: connections trg_connections_updated_at; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "trg_connections_updated_at" BEFORE UPDATE ON "public"."connections" FOR EACH ROW EXECUTE FUNCTION "public"."_context_entrypoint_bump_updated_at"();


--
-- Name: connectors trg_connectors_updated_at; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "trg_connectors_updated_at" BEFORE UPDATE ON "public"."connectors" FOR EACH ROW EXECUTE FUNCTION "public"."_connectors_bump_updated_at"();


--
-- Name: github_integrations trg_github_integrations_updated_at; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "trg_github_integrations_updated_at" BEFORE UPDATE ON "public"."github_integrations" FOR EACH ROW EXECUTE FUNCTION "public"."_github_integrations_bump_updated_at"();


--
-- Name: import_jobs trg_import_jobs_updated_at; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "trg_import_jobs_updated_at" BEFORE UPDATE ON "public"."import_jobs" FOR EACH ROW EXECUTE FUNCTION "public"."_import_jobs_bump_updated_at"();


--
-- Name: version_commits trg_normalize_scope_path; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "trg_normalize_scope_path" BEFORE INSERT OR UPDATE ON "public"."version_commits" FOR EACH ROW EXECUTE FUNCTION "public"."normalize_scope_path"();


--
-- Name: version_scope_state trg_normalize_scope_path; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "trg_normalize_scope_path" BEFORE INSERT OR UPDATE ON "public"."version_scope_state" FOR EACH ROW EXECUTE FUNCTION "public"."normalize_scope_path"();


--
-- Name: organization_billing_operations trg_organization_billing_operations_updated_at; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "trg_organization_billing_operations_updated_at" BEFORE UPDATE ON "public"."organization_billing_operations" FOR EACH ROW EXECUTE FUNCTION "public"."_billing_row_bump_updated_at"();


--
-- Name: organization_entitlements trg_organization_entitlements_updated_at; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "trg_organization_entitlements_updated_at" BEFORE UPDATE ON "public"."organization_entitlements" FOR EACH ROW EXECUTE FUNCTION "public"."_organization_entitlements_bump_updated_at"();


--
-- Name: organization_usage_counters trg_organization_usage_counters_updated_at; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "trg_organization_usage_counters_updated_at" BEFORE UPDATE ON "public"."organization_usage_counters" FOR EACH ROW EXECUTE FUNCTION "public"."_billing_row_bump_updated_at"();


--
-- Name: project_members trg_project_members_creator_admin_guard; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "trg_project_members_creator_admin_guard" BEFORE INSERT OR DELETE OR UPDATE ON "public"."project_members" FOR EACH ROW EXECUTE FUNCTION "public"."_enforce_project_creator_admin_member"();


--
-- Name: project_members trg_project_members_updated_at; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "trg_project_members_updated_at" BEFORE UPDATE ON "public"."project_members" FOR EACH ROW EXECUTE FUNCTION "public"."_project_members_bump_updated_at"();


--
-- Name: projects trg_projects_creator_admin_guard; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE CONSTRAINT TRIGGER "trg_projects_creator_admin_guard" AFTER INSERT OR UPDATE OF "id", "org_id", "created_by" ON "public"."projects" DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION "public"."_assert_project_creator_admin_at_commit"();


--
-- Name: repository_scopes trg_repository_scopes_updated_at; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "trg_repository_scopes_updated_at" BEFORE UPDATE ON "public"."repository_scopes" FOR EACH ROW EXECUTE FUNCTION "public"."_repository_scopes_bump_updated_at"();


--
-- Name: organization_entitlements trg_reset_entitlement_quote_on_revision_advance; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "trg_reset_entitlement_quote_on_revision_advance" BEFORE UPDATE OF "source_revision" ON "public"."organization_entitlements" FOR EACH ROW EXECUTE FUNCTION "public"."_reset_entitlement_quote_on_revision_advance"();


--
-- Name: runtime_billing_runs trg_runtime_billing_runs_updated_at; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "trg_runtime_billing_runs_updated_at" BEFORE UPDATE ON "public"."runtime_billing_runs" FOR EACH ROW EXECUTE FUNCTION "public"."_billing_row_bump_updated_at"();


--
-- Name: scope_sandbox_sessions trg_scope_sandbox_sessions_updated_at; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "trg_scope_sandbox_sessions_updated_at" BEFORE UPDATE ON "public"."scope_sandbox_sessions" FOR EACH ROW EXECUTE FUNCTION "public"."_scope_sandbox_sessions_bump_updated_at"();


--
-- Name: sync_runs trg_sync_runs_updated_at; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "trg_sync_runs_updated_at" BEFORE UPDATE ON "public"."sync_runs" FOR EACH ROW EXECUTE FUNCTION "public"."_context_entrypoint_bump_updated_at"();


--
-- Name: upload_items trg_upload_items_updated_at; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "trg_upload_items_updated_at" BEFORE UPDATE ON "public"."upload_items" FOR EACH ROW EXECUTE FUNCTION "public"."_context_entrypoint_bump_updated_at"();


--
-- Name: upload_jobs trg_upload_jobs_updated_at; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "trg_upload_jobs_updated_at" BEFORE UPDATE ON "public"."upload_jobs" FOR EACH ROW EXECUTE FUNCTION "public"."_context_entrypoint_bump_updated_at"();


--
-- Name: access_surface_credentials trg_validate_access_surface_credential; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "trg_validate_access_surface_credential" BEFORE INSERT OR UPDATE OF "org_id", "project_id", "access_surface_id", "user_id", "credential_type", "grant_mode", "credential_lifecycle", "status", "expires_at" ON "public"."access_surface_credentials" FOR EACH ROW EXECUTE FUNCTION "public"."_validate_access_surface_credential"();


--
-- Name: access_tools trg_validate_access_tool_project_boundary; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "trg_validate_access_tool_project_boundary" BEFORE INSERT OR UPDATE OF "access_point_id", "tool_id" ON "public"."access_tools" FOR EACH ROW EXECUTE FUNCTION "public"."_validate_access_tool_project_boundary"();


--
-- Name: version_refs trg_version_refs_updated_at; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "trg_version_refs_updated_at" BEFORE UPDATE ON "public"."version_refs" FOR EACH ROW EXECUTE FUNCTION "public"."_version_refs_bump_updated_at"();


--
-- Name: uploads uploads_remember_storage_principal; Type: TRIGGER; Schema: public; Owner: postgres
--

CREATE TRIGGER "uploads_remember_storage_principal" AFTER INSERT OR UPDATE OF "project_id", "created_by" ON "public"."uploads" FOR EACH ROW EXECUTE FUNCTION "public"."_remember_upload_storage_principal"();


--
-- Name: access_logs access_logs_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."access_logs"
    ADD CONSTRAINT "access_logs_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE SET NULL;


--
-- Name: access_logs access_logs_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."access_logs"
    ADD CONSTRAINT "access_logs_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE SET NULL;


--
-- Name: access_surface_credentials access_surface_credentials_access_surface_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."access_surface_credentials"
    ADD CONSTRAINT "access_surface_credentials_access_surface_id_fkey" FOREIGN KEY ("access_surface_id") REFERENCES "public"."access_surfaces"("id") ON DELETE CASCADE;


--
-- Name: access_surface_credentials access_surface_credentials_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."access_surface_credentials"
    ADD CONSTRAINT "access_surface_credentials_created_by_fkey" FOREIGN KEY ("created_by") REFERENCES "auth"."users"("id") ON DELETE SET NULL;


--
-- Name: access_surface_credentials access_surface_credentials_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."access_surface_credentials"
    ADD CONSTRAINT "access_surface_credentials_org_id_fkey" FOREIGN KEY ("org_id") REFERENCES "public"."organizations"("id") ON DELETE CASCADE;


--
-- Name: access_surface_credentials access_surface_credentials_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."access_surface_credentials"
    ADD CONSTRAINT "access_surface_credentials_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE CASCADE;


--
-- Name: access_surface_credentials access_surface_credentials_surface_tenant_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."access_surface_credentials"
    ADD CONSTRAINT "access_surface_credentials_surface_tenant_fkey" FOREIGN KEY ("access_surface_id", "project_id", "org_id") REFERENCES "public"."access_surfaces"("id", "project_id", "org_id") ON DELETE CASCADE;


--
-- Name: access_surface_credentials access_surface_credentials_user_org_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."access_surface_credentials"
    ADD CONSTRAINT "access_surface_credentials_user_org_fkey" FOREIGN KEY ("org_id", "user_id") REFERENCES "public"."org_members"("org_id", "user_id") ON DELETE CASCADE;


--
-- Name: access_surface_policies access_surface_policies_access_surface_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."access_surface_policies"
    ADD CONSTRAINT "access_surface_policies_access_surface_id_fkey" FOREIGN KEY ("access_surface_id") REFERENCES "public"."access_surfaces"("id") ON DELETE CASCADE;


--
-- Name: access_surfaces access_surfaces_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."access_surfaces"
    ADD CONSTRAINT "access_surfaces_created_by_fkey" FOREIGN KEY ("created_by") REFERENCES "auth"."users"("id") ON DELETE SET NULL;


--
-- Name: access_surfaces access_surfaces_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."access_surfaces"
    ADD CONSTRAINT "access_surfaces_org_id_fkey" FOREIGN KEY ("org_id") REFERENCES "public"."organizations"("id") ON DELETE SET NULL;


--
-- Name: access_surfaces access_surfaces_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."access_surfaces"
    ADD CONSTRAINT "access_surfaces_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE CASCADE;


--
-- Name: access_surfaces access_surfaces_project_org_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."access_surfaces"
    ADD CONSTRAINT "access_surfaces_project_org_fkey" FOREIGN KEY ("project_id", "org_id") REFERENCES "public"."projects"("id", "org_id") ON DELETE CASCADE;


--
-- Name: access_surfaces access_surfaces_scope_project_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."access_surfaces"
    ADD CONSTRAINT "access_surfaces_scope_project_fkey" FOREIGN KEY ("scope_id", "project_id") REFERENCES "public"."repository_scopes"("id", "project_id") ON DELETE CASCADE;


--
-- Name: access_tools access_tools_surface_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."access_tools"
    ADD CONSTRAINT "access_tools_surface_fkey" FOREIGN KEY ("access_point_id") REFERENCES "public"."access_surfaces"("id") ON DELETE CASCADE;


--
-- Name: agent_logs agent_logs_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."agent_logs"
    ADD CONSTRAINT "agent_logs_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE SET NULL;


--
-- Name: access_tools agent_tool_tool_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."access_tools"
    ADD CONSTRAINT "agent_tool_tool_id_fkey" FOREIGN KEY ("tool_id") REFERENCES "public"."tools"("id") ON DELETE CASCADE;


--
-- Name: api_keys api_keys_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."api_keys"
    ADD CONSTRAINT "api_keys_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;


--
-- Name: bookmarks bookmarks_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."bookmarks"
    ADD CONSTRAINT "bookmarks_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE CASCADE;


--
-- Name: chat_messages chat_messages_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."chat_messages"
    ADD CONSTRAINT "chat_messages_session_id_fkey" FOREIGN KEY ("session_id") REFERENCES "public"."chat_sessions"("id") ON DELETE CASCADE;


--
-- Name: chat_sessions chat_sessions_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."chat_sessions"
    ADD CONSTRAINT "chat_sessions_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;


--
-- Name: connections connections_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."connections"
    ADD CONSTRAINT "connections_created_by_fkey" FOREIGN KEY ("created_by") REFERENCES "auth"."users"("id") ON DELETE SET NULL;


--
-- Name: connections connections_last_sync_run_fk; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."connections"
    ADD CONSTRAINT "connections_last_sync_run_fk" FOREIGN KEY ("last_sync_run_id") REFERENCES "public"."sync_runs"("id") ON DELETE SET NULL;


--
-- Name: connections connections_oauth_connection_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."connections"
    ADD CONSTRAINT "connections_oauth_connection_id_fkey" FOREIGN KEY ("oauth_connection_id") REFERENCES "public"."oauth_connections"("id") ON DELETE SET NULL;


--
-- Name: connections connections_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."connections"
    ADD CONSTRAINT "connections_org_id_fkey" FOREIGN KEY ("org_id") REFERENCES "public"."organizations"("id") ON DELETE SET NULL;


--
-- Name: connections connections_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."connections"
    ADD CONSTRAINT "connections_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE CASCADE;


--
-- Name: connections connections_scope_project_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."connections"
    ADD CONSTRAINT "connections_scope_project_fkey" FOREIGN KEY ("scope_id", "project_id") REFERENCES "public"."repository_scopes"("id", "project_id") ON DELETE SET NULL ("scope_id");


--
-- Name: connectors connectors_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."connectors"
    ADD CONSTRAINT "connectors_created_by_fkey" FOREIGN KEY ("created_by") REFERENCES "auth"."users"("id") ON DELETE SET NULL;


--
-- Name: connectors connectors_oauth_connection_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."connectors"
    ADD CONSTRAINT "connectors_oauth_connection_id_fkey" FOREIGN KEY ("oauth_connection_id") REFERENCES "public"."oauth_connections"("id") ON DELETE SET NULL;


--
-- Name: connectors connectors_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."connectors"
    ADD CONSTRAINT "connectors_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE CASCADE;


--
-- Name: connectors connectors_scope_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."connectors"
    ADD CONSTRAINT "connectors_scope_id_fkey" FOREIGN KEY ("scope_id") REFERENCES "public"."repository_scopes"("id") ON DELETE CASCADE;


--
-- Name: context_publishes context_publish_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."context_publishes"
    ADD CONSTRAINT "context_publish_user_id_fkey" FOREIGN KEY ("created_by") REFERENCES "auth"."users"("id") ON DELETE CASCADE;


--
-- Name: etl_rules etl_rule_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."etl_rules"
    ADD CONSTRAINT "etl_rule_org_id_fkey" FOREIGN KEY ("org_id") REFERENCES "public"."organizations"("id") ON DELETE CASCADE;


--
-- Name: etl_rules etl_rule_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."etl_rules"
    ADD CONSTRAINT "etl_rule_user_id_fkey" FOREIGN KEY ("created_by") REFERENCES "auth"."users"("id") ON DELETE CASCADE;


--
-- Name: fs_path_index fs_path_index_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."fs_path_index"
    ADD CONSTRAINT "fs_path_index_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE CASCADE;


--
-- Name: github_integrations github_integrations_oauth_connection_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."github_integrations"
    ADD CONSTRAINT "github_integrations_oauth_connection_id_fkey" FOREIGN KEY ("oauth_connection_id") REFERENCES "public"."oauth_connections"("id") ON DELETE SET NULL;


--
-- Name: github_integrations github_integrations_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."github_integrations"
    ADD CONSTRAINT "github_integrations_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE CASCADE;


--
-- Name: github_sync_log github_sync_log_integration_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."github_sync_log"
    ADD CONSTRAINT "github_sync_log_integration_id_fkey" FOREIGN KEY ("integration_id") REFERENCES "public"."github_integrations"("id") ON DELETE CASCADE;


--
-- Name: import_jobs import_jobs_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."import_jobs"
    ADD CONSTRAINT "import_jobs_created_by_fkey" FOREIGN KEY ("created_by") REFERENCES "auth"."users"("id") ON DELETE CASCADE;


--
-- Name: import_jobs import_jobs_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."import_jobs"
    ADD CONSTRAINT "import_jobs_org_id_fkey" FOREIGN KEY ("org_id") REFERENCES "public"."organizations"("id") ON DELETE SET NULL;


--
-- Name: import_jobs import_jobs_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."import_jobs"
    ADD CONSTRAINT "import_jobs_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE CASCADE;


--
-- Name: local_shadow_snapshots local_shadow_snapshots_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."local_shadow_snapshots"
    ADD CONSTRAINT "local_shadow_snapshots_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE CASCADE;


--
-- Name: version_commits mut_commits_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."version_commits"
    ADD CONSTRAINT "mut_commits_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE CASCADE;


--
-- Name: version_conflicts mut_conflicts_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."version_conflicts"
    ADD CONSTRAINT "mut_conflicts_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE CASCADE;


--
-- Name: version_conflicts mut_conflicts_transaction_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."version_conflicts"
    ADD CONSTRAINT "mut_conflicts_transaction_id_fkey" FOREIGN KEY ("transaction_id") REFERENCES "public"."version_transactions"("id") ON DELETE CASCADE;


--
-- Name: version_scope_state mut_scope_state_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."version_scope_state"
    ADD CONSTRAINT "mut_scope_state_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE CASCADE;


--
-- Name: version_view_commits mut_version_index_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."version_view_commits"
    ADD CONSTRAINT "mut_version_index_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE CASCADE;


--
-- Name: version_outbox mut_version_outbox_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."version_outbox"
    ADD CONSTRAINT "mut_version_outbox_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE CASCADE;


--
-- Name: oauth_connections oauth_connection_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."oauth_connections"
    ADD CONSTRAINT "oauth_connection_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;


--
-- Name: oauth_states oauth_states_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."oauth_states"
    ADD CONSTRAINT "oauth_states_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;


--
-- Name: org_invitations org_invitations_invited_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."org_invitations"
    ADD CONSTRAINT "org_invitations_invited_by_fkey" FOREIGN KEY ("invited_by") REFERENCES "auth"."users"("id");


--
-- Name: org_invitations org_invitations_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."org_invitations"
    ADD CONSTRAINT "org_invitations_org_id_fkey" FOREIGN KEY ("org_id") REFERENCES "public"."organizations"("id") ON DELETE CASCADE;


--
-- Name: org_members org_members_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."org_members"
    ADD CONSTRAINT "org_members_org_id_fkey" FOREIGN KEY ("org_id") REFERENCES "public"."organizations"("id") ON DELETE CASCADE;


--
-- Name: org_members org_members_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."org_members"
    ADD CONSTRAINT "org_members_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "public"."profiles"("user_id") ON DELETE CASCADE;


--
-- Name: organization_billing_operations organization_billing_operations_actor_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."organization_billing_operations"
    ADD CONSTRAINT "organization_billing_operations_actor_user_id_fkey" FOREIGN KEY ("actor_user_id") REFERENCES "public"."profiles"("user_id") ON DELETE SET NULL;


--
-- Name: organization_billing_operations organization_billing_operations_invitation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."organization_billing_operations"
    ADD CONSTRAINT "organization_billing_operations_invitation_id_fkey" FOREIGN KEY ("invitation_id") REFERENCES "public"."org_invitations"("id") ON DELETE SET NULL;


--
-- Name: organization_billing_operations organization_billing_operations_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."organization_billing_operations"
    ADD CONSTRAINT "organization_billing_operations_org_id_fkey" FOREIGN KEY ("org_id") REFERENCES "public"."organizations"("id") ON DELETE CASCADE;


--
-- Name: organization_billing_operations organization_billing_operations_subject_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."organization_billing_operations"
    ADD CONSTRAINT "organization_billing_operations_subject_user_id_fkey" FOREIGN KEY ("subject_user_id") REFERENCES "public"."profiles"("user_id") ON DELETE SET NULL;


--
-- Name: organization_entitlement_events organization_entitlement_events_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."organization_entitlement_events"
    ADD CONSTRAINT "organization_entitlement_events_org_id_fkey" FOREIGN KEY ("org_id") REFERENCES "public"."organizations"("id") ON DELETE CASCADE;


--
-- Name: organization_entitlements organization_entitlements_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."organization_entitlements"
    ADD CONSTRAINT "organization_entitlements_org_id_fkey" FOREIGN KEY ("org_id") REFERENCES "public"."organizations"("id") ON DELETE CASCADE;


--
-- Name: organization_usage_counters organization_usage_counters_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."organization_usage_counters"
    ADD CONSTRAINT "organization_usage_counters_org_id_fkey" FOREIGN KEY ("org_id") REFERENCES "public"."organizations"("id") ON DELETE CASCADE;


--
-- Name: organization_usage_events organization_usage_events_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."organization_usage_events"
    ADD CONSTRAINT "organization_usage_events_org_id_fkey" FOREIGN KEY ("org_id") REFERENCES "public"."organizations"("id") ON DELETE CASCADE;


--
-- Name: organizations organizations_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."organizations"
    ADD CONSTRAINT "organizations_created_by_fkey" FOREIGN KEY ("created_by") REFERENCES "auth"."users"("id");


--
-- Name: profiles profiles_default_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."profiles"
    ADD CONSTRAINT "profiles_default_org_id_fkey" FOREIGN KEY ("default_org_id") REFERENCES "public"."organizations"("id") ON DELETE SET NULL;


--
-- Name: profiles profiles_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."profiles"
    ADD CONSTRAINT "profiles_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;


--
-- Name: project_members project_members_granted_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."project_members"
    ADD CONSTRAINT "project_members_granted_by_fkey" FOREIGN KEY ("granted_by") REFERENCES "auth"."users"("id") ON DELETE SET NULL;


--
-- Name: project_members project_members_org_user_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."project_members"
    ADD CONSTRAINT "project_members_org_user_fkey" FOREIGN KEY ("org_id", "user_id") REFERENCES "public"."org_members"("org_id", "user_id") ON DELETE CASCADE;


--
-- Name: project_members project_members_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."project_members"
    ADD CONSTRAINT "project_members_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE CASCADE;


--
-- Name: project_members project_members_project_org_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."project_members"
    ADD CONSTRAINT "project_members_project_org_fkey" FOREIGN KEY ("project_id", "org_id") REFERENCES "public"."projects"("id", "org_id") ON DELETE CASCADE;


--
-- Name: project_members project_members_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."project_members"
    ADD CONSTRAINT "project_members_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;


--
-- Name: projects project_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."projects"
    ADD CONSTRAINT "project_org_id_fkey" FOREIGN KEY ("org_id") REFERENCES "public"."organizations"("id") ON DELETE CASCADE;


--
-- Name: project_storage_principals project_storage_principals_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."project_storage_principals"
    ADD CONSTRAINT "project_storage_principals_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE CASCADE;


--
-- Name: projects project_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."projects"
    ADD CONSTRAINT "project_user_id_fkey" FOREIGN KEY ("created_by") REFERENCES "auth"."users"("id") ON DELETE SET NULL;


--
-- Name: project_write_leases project_write_leases_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."project_write_leases"
    ADD CONSTRAINT "project_write_leases_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE CASCADE;


--
-- Name: repository_scopes repository_scopes_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."repository_scopes"
    ADD CONSTRAINT "repository_scopes_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE CASCADE;


--
-- Name: runtime_billing_runs runtime_billing_runs_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."runtime_billing_runs"
    ADD CONSTRAINT "runtime_billing_runs_org_id_fkey" FOREIGN KEY ("org_id") REFERENCES "public"."organizations"("id") ON DELETE CASCADE;


--
-- Name: runtime_billing_runs runtime_billing_runs_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."runtime_billing_runs"
    ADD CONSTRAINT "runtime_billing_runs_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE SET NULL;


--
-- Name: sandbox_endpoints sandbox_endpoints_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."sandbox_endpoints"
    ADD CONSTRAINT "sandbox_endpoints_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE CASCADE;


--
-- Name: sandbox_execution_sessions sandbox_execution_sessions_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."sandbox_execution_sessions"
    ADD CONSTRAINT "sandbox_execution_sessions_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE CASCADE;


--
-- Name: scope_sandbox_sessions scope_sandbox_sessions_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."scope_sandbox_sessions"
    ADD CONSTRAINT "scope_sandbox_sessions_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE CASCADE;


--
-- Name: scope_sandbox_sessions scope_sandbox_sessions_scope_project_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."scope_sandbox_sessions"
    ADD CONSTRAINT "scope_sandbox_sessions_scope_project_fkey" FOREIGN KEY ("scope_id", "project_id") REFERENCES "public"."repository_scopes"("id", "project_id") ON DELETE CASCADE;


--
-- Name: scope_sync_events scope_sync_events_scope_project_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."scope_sync_events"
    ADD CONSTRAINT "scope_sync_events_scope_project_fkey" FOREIGN KEY ("scope_id", "project_id") REFERENCES "public"."repository_scopes"("id", "project_id") ON DELETE CASCADE;


--
-- Name: scope_sync_settings scope_sync_settings_scope_project_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."scope_sync_settings"
    ADD CONSTRAINT "scope_sync_settings_scope_project_fkey" FOREIGN KEY ("scope_id", "project_id") REFERENCES "public"."repository_scopes"("id", "project_id") ON DELETE CASCADE;


--
-- Name: subscriptions subscriptions_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."subscriptions"
    ADD CONSTRAINT "subscriptions_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "auth"."users"("id") ON DELETE CASCADE;


--
-- Name: sync_runs sync_runs_connection_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."sync_runs"
    ADD CONSTRAINT "sync_runs_connection_id_fkey" FOREIGN KEY ("connection_id") REFERENCES "public"."connections"("id") ON DELETE CASCADE;


--
-- Name: sync_runs sync_runs_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."sync_runs"
    ADD CONSTRAINT "sync_runs_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE CASCADE;


--
-- Name: sync_runs sync_runs_triggered_by_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."sync_runs"
    ADD CONSTRAINT "sync_runs_triggered_by_user_id_fkey" FOREIGN KEY ("triggered_by_user_id") REFERENCES "auth"."users"("id") ON DELETE SET NULL;


--
-- Name: tables tables_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."tables"
    ADD CONSTRAINT "tables_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE CASCADE;


--
-- Name: tools tool_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."tools"
    ADD CONSTRAINT "tool_org_id_fkey" FOREIGN KEY ("org_id") REFERENCES "public"."organizations"("id") ON DELETE CASCADE;


--
-- Name: tools tool_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."tools"
    ADD CONSTRAINT "tool_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE CASCADE;


--
-- Name: tools tool_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."tools"
    ADD CONSTRAINT "tool_user_id_fkey" FOREIGN KEY ("created_by") REFERENCES "auth"."users"("id") ON DELETE CASCADE;


--
-- Name: upload_items upload_items_upload_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."upload_items"
    ADD CONSTRAINT "upload_items_upload_job_id_fkey" FOREIGN KEY ("upload_job_id") REFERENCES "public"."upload_jobs"("id") ON DELETE CASCADE;


--
-- Name: upload_jobs upload_jobs_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."upload_jobs"
    ADD CONSTRAINT "upload_jobs_created_by_fkey" FOREIGN KEY ("created_by") REFERENCES "auth"."users"("id") ON DELETE SET NULL;


--
-- Name: upload_jobs upload_jobs_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."upload_jobs"
    ADD CONSTRAINT "upload_jobs_org_id_fkey" FOREIGN KEY ("org_id") REFERENCES "public"."organizations"("id") ON DELETE SET NULL;


--
-- Name: upload_jobs upload_jobs_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."upload_jobs"
    ADD CONSTRAINT "upload_jobs_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE CASCADE;


--
-- Name: uploads uploads_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."uploads"
    ADD CONSTRAINT "uploads_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE CASCADE;


--
-- Name: uploads uploads_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."uploads"
    ADD CONSTRAINT "uploads_user_id_fkey" FOREIGN KEY ("created_by") REFERENCES "auth"."users"("id") ON DELETE CASCADE;


--
-- Name: version_object_gc_candidates version_object_gc_candidates_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."version_object_gc_candidates"
    ADD CONSTRAINT "version_object_gc_candidates_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE CASCADE;


--
-- Name: version_object_gc_runs version_object_gc_runs_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."version_object_gc_runs"
    ADD CONSTRAINT "version_object_gc_runs_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE CASCADE;


--
-- Name: version_project_root_integrity_incidents version_project_root_integrity_incidents_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."version_project_root_integrity_incidents"
    ADD CONSTRAINT "version_project_root_integrity_incidents_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE CASCADE;


--
-- Name: version_refs version_refs_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."version_refs"
    ADD CONSTRAINT "version_refs_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE CASCADE;


--
-- Name: version_transactions version_transactions_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: postgres
--

ALTER TABLE ONLY "public"."version_transactions"
    ADD CONSTRAINT "version_transactions_project_id_fkey" FOREIGN KEY ("project_id") REFERENCES "public"."projects"("id") ON DELETE CASCADE;


--
-- Name: access_logs; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."access_logs" ENABLE ROW LEVEL SECURITY;

--
-- Name: access_logs access_logs_authenticated_project_member; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "access_logs_authenticated_project_member" ON "public"."access_logs" FOR SELECT TO "authenticated" USING ((EXISTS ( SELECT 1
   FROM (("public"."projects" "p"
     LEFT JOIN "public"."org_members" "om" ON ((("om"."org_id" = "p"."org_id") AND ("om"."user_id" = "auth"."uid"()))))
     LEFT JOIN "public"."project_members" "pm" ON ((("pm"."project_id" = "p"."id") AND ("pm"."user_id" = "auth"."uid"()))))
  WHERE (("p"."id" = "access_logs"."project_id") AND (((COALESCE("p"."visibility", 'org'::"text") = 'org'::"text") AND ("om"."user_id" IS NOT NULL)) OR ("om"."role" = 'owner'::"text") OR ("pm"."user_id" IS NOT NULL))))));


--
-- Name: access_surface_credentials; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."access_surface_credentials" ENABLE ROW LEVEL SECURITY;

--
-- Name: access_surface_credentials access_surface_credentials_service_role_all; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "access_surface_credentials_service_role_all" ON "public"."access_surface_credentials" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: access_surface_policies; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."access_surface_policies" ENABLE ROW LEVEL SECURITY;

--
-- Name: access_surface_policies access_surface_policies_service_role_all; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "access_surface_policies_service_role_all" ON "public"."access_surface_policies" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: access_surfaces; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."access_surfaces" ENABLE ROW LEVEL SECURITY;

--
-- Name: access_surfaces access_surfaces_service_role_all; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "access_surfaces_service_role_all" ON "public"."access_surfaces" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: access_tools; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."access_tools" ENABLE ROW LEVEL SECURITY;

--
-- Name: agent_execution_logs; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."agent_execution_logs" ENABLE ROW LEVEL SECURITY;

--
-- Name: agent_logs; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."agent_logs" ENABLE ROW LEVEL SECURITY;

--
-- Name: agent_profiles; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."agent_profiles" ENABLE ROW LEVEL SECURITY;

--
-- Name: api_keys; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."api_keys" ENABLE ROW LEVEL SECURITY;

--
-- Name: api_keys api_keys_select; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "api_keys_select" ON "public"."api_keys" FOR SELECT USING (("user_id" = "auth"."uid"()));


--
-- Name: chat_messages; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."chat_messages" ENABLE ROW LEVEL SECURITY;

--
-- Name: chat_messages chat_messages_service_role; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "chat_messages_service_role" ON "public"."chat_messages" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: chat_sessions; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."chat_sessions" ENABLE ROW LEVEL SECURITY;

--
-- Name: chat_sessions chat_sessions_service_role; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "chat_sessions_service_role" ON "public"."chat_sessions" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: chunks; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."chunks" ENABLE ROW LEVEL SECURITY;

--
-- Name: connections; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."connections" ENABLE ROW LEVEL SECURITY;

--
-- Name: connections connections_service_role_all; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "connections_service_role_all" ON "public"."connections" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: connector_runs connector_runs_service_role_all; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "connector_runs_service_role_all" ON "public"."connector_runs" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: connectors; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."connectors" ENABLE ROW LEVEL SECURITY;

--
-- Name: connectors connectors_service_role_all; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "connectors_service_role_all" ON "public"."connectors" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: context_publishes; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."context_publishes" ENABLE ROW LEVEL SECURITY;

--
-- Name: etl_rules; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."etl_rules" ENABLE ROW LEVEL SECURITY;

--
-- Name: fs_path_index; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."fs_path_index" ENABLE ROW LEVEL SECURITY;

--
-- Name: fs_path_index fs_path_index_service_role_all; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "fs_path_index_service_role_all" ON "public"."fs_path_index" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: git_credential_issue_operations; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."git_credential_issue_operations" ENABLE ROW LEVEL SECURITY;

--
-- Name: github_integrations; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."github_integrations" ENABLE ROW LEVEL SECURITY;

--
-- Name: github_integrations github_integrations_service_role_all; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "github_integrations_service_role_all" ON "public"."github_integrations" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: github_sync_log; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."github_sync_log" ENABLE ROW LEVEL SECURITY;

--
-- Name: github_sync_log github_sync_log_service_role_all; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "github_sync_log_service_role_all" ON "public"."github_sync_log" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: import_jobs; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."import_jobs" ENABLE ROW LEVEL SECURITY;

--
-- Name: import_jobs import_jobs_service_role_all; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "import_jobs_service_role_all" ON "public"."import_jobs" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: local_shadow_snapshots; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."local_shadow_snapshots" ENABLE ROW LEVEL SECURITY;

--
-- Name: migration_log; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."migration_log" ENABLE ROW LEVEL SECURITY;

--
-- Name: migration_log migration_log_service_role_all; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "migration_log_service_role_all" ON "public"."migration_log" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: version_conflicts mut_conflicts_service_role_all; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "mut_conflicts_service_role_all" ON "public"."version_conflicts" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: version_view_commits mut_version_index_service_role_all; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "mut_version_index_service_role_all" ON "public"."version_view_commits" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: version_outbox mut_version_outbox_service_role_all; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "mut_version_outbox_service_role_all" ON "public"."version_outbox" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: oauth_connections; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."oauth_connections" ENABLE ROW LEVEL SECURITY;

--
-- Name: oauth_states; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."oauth_states" ENABLE ROW LEVEL SECURITY;

--
-- Name: oauth_states oauth_states_service_role_all; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "oauth_states_service_role_all" ON "public"."oauth_states" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: org_invitations; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."org_invitations" ENABLE ROW LEVEL SECURITY;

--
-- Name: org_members; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."org_members" ENABLE ROW LEVEL SECURITY;

--
-- Name: organization_billing_operations; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."organization_billing_operations" ENABLE ROW LEVEL SECURITY;

--
-- Name: organization_billing_operations organization_billing_operations_service_all; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "organization_billing_operations_service_all" ON "public"."organization_billing_operations" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: organization_entitlement_events; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."organization_entitlement_events" ENABLE ROW LEVEL SECURITY;

--
-- Name: organization_entitlement_events organization_entitlement_events_service_role_all; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "organization_entitlement_events_service_role_all" ON "public"."organization_entitlement_events" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: organization_entitlements; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."organization_entitlements" ENABLE ROW LEVEL SECURITY;

--
-- Name: organization_entitlements organization_entitlements_member_select; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "organization_entitlements_member_select" ON "public"."organization_entitlements" FOR SELECT TO "authenticated" USING ((EXISTS ( SELECT 1
   FROM "public"."org_members" "m"
  WHERE (("m"."org_id" = "organization_entitlements"."org_id") AND ("m"."user_id" = "auth"."uid"())))));


--
-- Name: organization_entitlements organization_entitlements_service_role_all; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "organization_entitlements_service_role_all" ON "public"."organization_entitlements" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: organization_usage_counters; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."organization_usage_counters" ENABLE ROW LEVEL SECURITY;

--
-- Name: organization_usage_counters organization_usage_counters_service_all; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "organization_usage_counters_service_all" ON "public"."organization_usage_counters" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: organization_usage_events; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."organization_usage_events" ENABLE ROW LEVEL SECURITY;

--
-- Name: organization_usage_events organization_usage_events_service_all; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "organization_usage_events_service_all" ON "public"."organization_usage_events" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: organizations; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."organizations" ENABLE ROW LEVEL SECURITY;

--
-- Name: profiles; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."profiles" ENABLE ROW LEVEL SECURITY;

--
-- Name: profiles profiles_select; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "profiles_select" ON "public"."profiles" FOR SELECT USING (("user_id" = "auth"."uid"()));


--
-- Name: project_create_operations; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."project_create_operations" ENABLE ROW LEVEL SECURITY;

--
-- Name: project_deletion_jobs; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."project_deletion_jobs" ENABLE ROW LEVEL SECURITY;

--
-- Name: project_members; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."project_members" ENABLE ROW LEVEL SECURITY;

--
-- Name: project_storage_inventory_batches; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."project_storage_inventory_batches" ENABLE ROW LEVEL SECURITY;

--
-- Name: project_storage_inventory_state; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."project_storage_inventory_state" ENABLE ROW LEVEL SECURITY;

--
-- Name: project_storage_orphan_prefixes; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."project_storage_orphan_prefixes" ENABLE ROW LEVEL SECURITY;

--
-- Name: project_storage_principals; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."project_storage_principals" ENABLE ROW LEVEL SECURITY;

--
-- Name: project_storage_principals project_storage_principals_service_role_read; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "project_storage_principals_service_role_read" ON "public"."project_storage_principals" FOR SELECT TO "service_role" USING (true);


--
-- Name: project_write_leases; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."project_write_leases" ENABLE ROW LEVEL SECURITY;

--
-- Name: projects; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."projects" ENABLE ROW LEVEL SECURITY;

--
-- Name: repository_scopes; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."repository_scopes" ENABLE ROW LEVEL SECURITY;

--
-- Name: repository_scopes repository_scopes_service_role_all; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "repository_scopes_service_role_all" ON "public"."repository_scopes" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: runtime_billing_runs; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."runtime_billing_runs" ENABLE ROW LEVEL SECURITY;

--
-- Name: runtime_billing_runs runtime_billing_runs_service_all; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "runtime_billing_runs_service_all" ON "public"."runtime_billing_runs" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: sandbox_endpoints; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."sandbox_endpoints" ENABLE ROW LEVEL SECURITY;

--
-- Name: sandbox_execution_sessions; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."sandbox_execution_sessions" ENABLE ROW LEVEL SECURITY;

--
-- Name: scope_sandbox_sessions; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."scope_sandbox_sessions" ENABLE ROW LEVEL SECURITY;

--
-- Name: access_logs service_role_all_access_logs; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "service_role_all_access_logs" ON "public"."access_logs" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: agent_execution_logs service_role_all_agent_execution_log; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "service_role_all_agent_execution_log" ON "public"."agent_execution_logs" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: agent_logs service_role_all_agent_logs; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "service_role_all_agent_logs" ON "public"."agent_logs" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: agent_profiles service_role_all_agent_profiles; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "service_role_all_agent_profiles" ON "public"."agent_profiles" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: access_tools service_role_all_agent_tool; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "service_role_all_agent_tool" ON "public"."access_tools" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: chat_messages service_role_all_chat_messages; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "service_role_all_chat_messages" ON "public"."chat_messages" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: chat_sessions service_role_all_chat_sessions; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "service_role_all_chat_sessions" ON "public"."chat_sessions" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: chunks service_role_all_chunks; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "service_role_all_chunks" ON "public"."chunks" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: context_publishes service_role_all_context_publish; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "service_role_all_context_publish" ON "public"."context_publishes" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: etl_rules service_role_all_etl_rule; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "service_role_all_etl_rule" ON "public"."etl_rules" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: version_commits service_role_all_mut_commits; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "service_role_all_mut_commits" ON "public"."version_commits" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: version_scope_state service_role_all_mut_scope_state; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "service_role_all_mut_scope_state" ON "public"."version_scope_state" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: oauth_connections service_role_all_oauth_connection; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "service_role_all_oauth_connection" ON "public"."oauth_connections" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: org_invitations service_role_all_org_invitations; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "service_role_all_org_invitations" ON "public"."org_invitations" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: org_members service_role_all_org_members; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "service_role_all_org_members" ON "public"."org_members" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: organizations service_role_all_organizations; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "service_role_all_organizations" ON "public"."organizations" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: profiles service_role_all_profiles; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "service_role_all_profiles" ON "public"."profiles" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: projects service_role_all_project; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "service_role_all_project" ON "public"."projects" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: project_members service_role_all_project_members; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "service_role_all_project_members" ON "public"."project_members" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: sandbox_endpoints service_role_all_sandbox_endpoints; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "service_role_all_sandbox_endpoints" ON "public"."sandbox_endpoints" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: sync_changelog service_role_all_sync_changelog; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "service_role_all_sync_changelog" ON "public"."sync_changelog" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: sync_state service_role_all_sync_state; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "service_role_all_sync_state" ON "public"."sync_state" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: tools service_role_all_tool; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "service_role_all_tool" ON "public"."tools" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: uploads service_role_all_uploads; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "service_role_all_uploads" ON "public"."uploads" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: local_shadow_snapshots shadow_snapshots_service_role_all; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "shadow_snapshots_service_role_all" ON "public"."local_shadow_snapshots" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: subscriptions; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."subscriptions" ENABLE ROW LEVEL SECURITY;

--
-- Name: subscriptions subscriptions_select; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "subscriptions_select" ON "public"."subscriptions" FOR SELECT USING (("user_id" = "auth"."uid"()));


--
-- Name: sync_changelog; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."sync_changelog" ENABLE ROW LEVEL SECURITY;

--
-- Name: sync_runs; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."sync_runs" ENABLE ROW LEVEL SECURITY;

--
-- Name: sync_runs sync_runs_service_role_all; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "sync_runs_service_role_all" ON "public"."sync_runs" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: sync_state; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."sync_state" ENABLE ROW LEVEL SECURITY;

--
-- Name: tools; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."tools" ENABLE ROW LEVEL SECURITY;

--
-- Name: upload_items; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."upload_items" ENABLE ROW LEVEL SECURITY;

--
-- Name: upload_items upload_items_service_role_all; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "upload_items_service_role_all" ON "public"."upload_items" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: upload_jobs; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."upload_jobs" ENABLE ROW LEVEL SECURITY;

--
-- Name: upload_jobs upload_jobs_service_role_all; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "upload_jobs_service_role_all" ON "public"."upload_jobs" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: uploads; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."uploads" ENABLE ROW LEVEL SECURITY;

--
-- Name: version_commits; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."version_commits" ENABLE ROW LEVEL SECURITY;

--
-- Name: version_conflicts; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."version_conflicts" ENABLE ROW LEVEL SECURITY;

--
-- Name: version_object_gc_candidates; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."version_object_gc_candidates" ENABLE ROW LEVEL SECURITY;

--
-- Name: version_object_gc_candidates version_object_gc_candidates_service_role; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "version_object_gc_candidates_service_role" ON "public"."version_object_gc_candidates" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: version_object_gc_runs; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."version_object_gc_runs" ENABLE ROW LEVEL SECURITY;

--
-- Name: version_object_gc_runs version_object_gc_runs_service_role; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "version_object_gc_runs_service_role" ON "public"."version_object_gc_runs" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: version_object_locations; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."version_object_locations" ENABLE ROW LEVEL SECURITY;

--
-- Name: version_outbox; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."version_outbox" ENABLE ROW LEVEL SECURITY;

--
-- Name: version_project_root_integrity_incidents; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."version_project_root_integrity_incidents" ENABLE ROW LEVEL SECURITY;

--
-- Name: version_project_root_integrity_incidents version_project_root_integrity_incidents_service_role; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "version_project_root_integrity_incidents_service_role" ON "public"."version_project_root_integrity_incidents" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: version_refs; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."version_refs" ENABLE ROW LEVEL SECURITY;

--
-- Name: version_refs version_refs_service_role_all; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "version_refs_service_role_all" ON "public"."version_refs" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: version_scope_state; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."version_scope_state" ENABLE ROW LEVEL SECURITY;

--
-- Name: version_text_index; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."version_text_index" ENABLE ROW LEVEL SECURITY;

--
-- Name: version_text_index version_text_index_service_role_all; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "version_text_index_service_role_all" ON "public"."version_text_index" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: version_text_index_state; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."version_text_index_state" ENABLE ROW LEVEL SECURITY;

--
-- Name: version_text_index_state version_text_index_state_service_role_all; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "version_text_index_state_service_role_all" ON "public"."version_text_index_state" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: version_transactions; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."version_transactions" ENABLE ROW LEVEL SECURITY;

--
-- Name: version_transactions version_transactions_service_role_all; Type: POLICY; Schema: public; Owner: postgres
--

CREATE POLICY "version_transactions_service_role_all" ON "public"."version_transactions" TO "service_role" USING (true) WITH CHECK (true);


--
-- Name: version_view_commits; Type: ROW SECURITY; Schema: public; Owner: postgres
--

ALTER TABLE "public"."version_view_commits" ENABLE ROW LEVEL SECURITY;

--
-- Name: SCHEMA "public"; Type: ACL; Schema: -; Owner: pg_database_owner
--

GRANT USAGE ON SCHEMA "public" TO "postgres";
GRANT USAGE ON SCHEMA "public" TO "anon";
GRANT USAGE ON SCHEMA "public" TO "authenticated";
GRANT USAGE ON SCHEMA "public" TO "service_role";


--
-- Name: FUNCTION "_assert_project_creator_admin_at_commit"(); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."_assert_project_creator_admin_at_commit"() FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."_assert_project_creator_admin_at_commit"() TO "service_role";


--
-- Name: FUNCTION "_billing_row_bump_updated_at"(); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION "public"."_billing_row_bump_updated_at"() TO "anon";
GRANT ALL ON FUNCTION "public"."_billing_row_bump_updated_at"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."_billing_row_bump_updated_at"() TO "service_role";


--
-- Name: FUNCTION "_confirm_correlated_billing_operations"(); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."_confirm_correlated_billing_operations"() FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."_confirm_correlated_billing_operations"() TO "service_role";


--
-- Name: FUNCTION "_connectors_bump_updated_at"(); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION "public"."_connectors_bump_updated_at"() TO "anon";
GRANT ALL ON FUNCTION "public"."_connectors_bump_updated_at"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."_connectors_bump_updated_at"() TO "service_role";


--
-- Name: FUNCTION "_context_entrypoint_bump_updated_at"(); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION "public"."_context_entrypoint_bump_updated_at"() TO "anon";
GRANT ALL ON FUNCTION "public"."_context_entrypoint_bump_updated_at"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."_context_entrypoint_bump_updated_at"() TO "service_role";


--
-- Name: FUNCTION "_enforce_project_creator_admin_member"(); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."_enforce_project_creator_admin_member"() FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."_enforce_project_creator_admin_member"() TO "service_role";


--
-- Name: FUNCTION "_github_integrations_bump_updated_at"(); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION "public"."_github_integrations_bump_updated_at"() TO "anon";
GRANT ALL ON FUNCTION "public"."_github_integrations_bump_updated_at"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."_github_integrations_bump_updated_at"() TO "service_role";


--
-- Name: FUNCTION "_import_jobs_bump_updated_at"(); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION "public"."_import_jobs_bump_updated_at"() TO "anon";
GRANT ALL ON FUNCTION "public"."_import_jobs_bump_updated_at"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."_import_jobs_bump_updated_at"() TO "service_role";


--
-- Name: FUNCTION "_organization_entitlements_bump_updated_at"(); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION "public"."_organization_entitlements_bump_updated_at"() TO "anon";
GRANT ALL ON FUNCTION "public"."_organization_entitlements_bump_updated_at"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."_organization_entitlements_bump_updated_at"() TO "service_role";


--
-- Name: FUNCTION "_prepare_project_deletion_job_storage"(); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."_prepare_project_deletion_job_storage"() FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."_prepare_project_deletion_job_storage"() TO "service_role";


--
-- Name: FUNCTION "_project_deletion_external_ingest_snapshot_valid"("p_project_id" "text", "p_external_ingest_resources" "jsonb"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."_project_deletion_external_ingest_snapshot_valid"("p_project_id" "text", "p_external_ingest_resources" "jsonb") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."_project_deletion_external_ingest_snapshot_valid"("p_project_id" "text", "p_external_ingest_resources" "jsonb") TO "service_role";


--
-- Name: FUNCTION "_project_deletion_object_prefixes"("p_project_id" "text", "p_storage_principals" "jsonb"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."_project_deletion_object_prefixes"("p_project_id" "text", "p_storage_principals" "jsonb") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."_project_deletion_object_prefixes"("p_project_id" "text", "p_storage_principals" "jsonb") TO "service_role";


--
-- Name: FUNCTION "_project_deletion_principals_valid"("p_project_id" "text", "p_requested_by" "uuid", "p_storage_principals" "jsonb"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."_project_deletion_principals_valid"("p_project_id" "text", "p_requested_by" "uuid", "p_storage_principals" "jsonb") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."_project_deletion_principals_valid"("p_project_id" "text", "p_requested_by" "uuid", "p_storage_principals" "jsonb") TO "service_role";


--
-- Name: FUNCTION "_project_deletion_sandbox_resources"("p_project_id" "text"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."_project_deletion_sandbox_resources"("p_project_id" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."_project_deletion_sandbox_resources"("p_project_id" "text") TO "service_role";


--
-- Name: FUNCTION "_project_deletion_sandbox_resources_valid"("p_resources" "jsonb"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."_project_deletion_sandbox_resources_valid"("p_resources" "jsonb") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."_project_deletion_sandbox_resources_valid"("p_resources" "jsonb") TO "service_role";


--
-- Name: FUNCTION "_project_deletion_search_prefixes"("p_project_id" "text"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."_project_deletion_search_prefixes"("p_project_id" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."_project_deletion_search_prefixes"("p_project_id" "text") TO "service_role";


--
-- Name: FUNCTION "_project_deletion_storage_principals"("p_project_id" "text", "p_requested_by" "uuid"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."_project_deletion_storage_principals"("p_project_id" "text", "p_requested_by" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."_project_deletion_storage_principals"("p_project_id" "text", "p_requested_by" "uuid") TO "service_role";


--
-- Name: FUNCTION "_project_initialization_has_cascade_dependents"("p_project_id" "text"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."_project_initialization_has_cascade_dependents"("p_project_id" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."_project_initialization_has_cascade_dependents"("p_project_id" "text") TO "service_role";


--
-- Name: FUNCTION "_project_members_bump_updated_at"(); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION "public"."_project_members_bump_updated_at"() TO "anon";
GRANT ALL ON FUNCTION "public"."_project_members_bump_updated_at"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."_project_members_bump_updated_at"() TO "service_role";


--
-- Name: FUNCTION "_remember_upload_storage_principal"(); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."_remember_upload_storage_principal"() FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."_remember_upload_storage_principal"() TO "service_role";


--
-- Name: FUNCTION "_repository_scopes_bump_updated_at"(); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION "public"."_repository_scopes_bump_updated_at"() TO "anon";
GRANT ALL ON FUNCTION "public"."_repository_scopes_bump_updated_at"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."_repository_scopes_bump_updated_at"() TO "service_role";


--
-- Name: FUNCTION "_reset_entitlement_quote_on_revision_advance"(); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."_reset_entitlement_quote_on_revision_advance"() FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."_reset_entitlement_quote_on_revision_advance"() TO "service_role";


--
-- Name: FUNCTION "_scope_sandbox_sessions_bump_updated_at"(); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION "public"."_scope_sandbox_sessions_bump_updated_at"() TO "anon";
GRANT ALL ON FUNCTION "public"."_scope_sandbox_sessions_bump_updated_at"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."_scope_sandbox_sessions_bump_updated_at"() TO "service_role";


--
-- Name: FUNCTION "_untitled_project_slot"("p_name" "text"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."_untitled_project_slot"("p_name" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."_untitled_project_slot"("p_name" "text") TO "service_role";


--
-- Name: FUNCTION "_validate_access_surface_credential"(); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION "public"."_validate_access_surface_credential"() TO "anon";
GRANT ALL ON FUNCTION "public"."_validate_access_surface_credential"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."_validate_access_surface_credential"() TO "service_role";


--
-- Name: FUNCTION "_validate_access_tool_project_boundary"(); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION "public"."_validate_access_tool_project_boundary"() TO "anon";
GRANT ALL ON FUNCTION "public"."_validate_access_tool_project_boundary"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."_validate_access_tool_project_boundary"() TO "service_role";


--
-- Name: FUNCTION "_version_refs_bump_updated_at"(); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION "public"."_version_refs_bump_updated_at"() TO "anon";
GRANT ALL ON FUNCTION "public"."_version_refs_bump_updated_at"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."_version_refs_bump_updated_at"() TO "service_role";


--
-- Name: FUNCTION "abandon_project_initialization"("p_project_id" "text", "p_operation_key" "text", "p_actor_user_id" "uuid", "p_quiescence_seconds" integer, "p_worker_id" "text"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."abandon_project_initialization"("p_project_id" "text", "p_operation_key" "text", "p_actor_user_id" "uuid", "p_quiescence_seconds" integer, "p_worker_id" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."abandon_project_initialization"("p_project_id" "text", "p_operation_key" "text", "p_actor_user_id" "uuid", "p_quiescence_seconds" integer, "p_worker_id" "text") TO "service_role";


--
-- Name: FUNCTION "abort_deferred_project_publication"("p_project_id" "text", "p_operation_key" "text", "p_actor_user_id" "uuid", "p_quiescence_seconds" integer, "p_worker_id" "text"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."abort_deferred_project_publication"("p_project_id" "text", "p_operation_key" "text", "p_actor_user_id" "uuid", "p_quiescence_seconds" integer, "p_worker_id" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."abort_deferred_project_publication"("p_project_id" "text", "p_operation_key" "text", "p_actor_user_id" "uuid", "p_quiescence_seconds" integer, "p_worker_id" "text") TO "service_role";


--
-- Name: FUNCTION "acquire_project_write_lease"("p_project_id" "text", "p_lease_id" "uuid", "p_holder_id" "text", "p_operation" "text", "p_ttl_seconds" integer, "p_initialization_operation_key" "text", "p_initialization_actor" "uuid", "p_initialization_worker" "text"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."acquire_project_write_lease"("p_project_id" "text", "p_lease_id" "uuid", "p_holder_id" "text", "p_operation" "text", "p_ttl_seconds" integer, "p_initialization_operation_key" "text", "p_initialization_actor" "uuid", "p_initialization_worker" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."acquire_project_write_lease"("p_project_id" "text", "p_lease_id" "uuid", "p_holder_id" "text", "p_operation" "text", "p_ttl_seconds" integer, "p_initialization_operation_key" "text", "p_initialization_actor" "uuid", "p_initialization_worker" "text") TO "service_role";


--
-- Name: TABLE "project_members"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."project_members" TO "anon";
GRANT ALL ON TABLE "public"."project_members" TO "authenticated";
GRANT ALL ON TABLE "public"."project_members" TO "service_role";


--
-- Name: FUNCTION "add_project_member_authorized"("p_project_id" "text", "p_target_user_id" "uuid", "p_role" "text", "p_actor_user_id" "uuid"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."add_project_member_authorized"("p_project_id" "text", "p_target_user_id" "uuid", "p_role" "text", "p_actor_user_id" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."add_project_member_authorized"("p_project_id" "text", "p_target_user_id" "uuid", "p_role" "text", "p_actor_user_id" "uuid") TO "service_role";


--
-- Name: FUNCTION "adjust_organization_usage_counter"("p_org_id" "text", "p_metric" "text", "p_delta" bigint, "p_idempotency_key" "text", "p_source" "text", "p_metadata" "jsonb"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."adjust_organization_usage_counter"("p_org_id" "text", "p_metric" "text", "p_delta" bigint, "p_idempotency_key" "text", "p_source" "text", "p_metadata" "jsonb") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."adjust_organization_usage_counter"("p_org_id" "text", "p_metric" "text", "p_delta" bigint, "p_idempotency_key" "text", "p_source" "text", "p_metadata" "jsonb") TO "service_role";


--
-- Name: FUNCTION "analytics_access_summary"("p_project_id" "text", "p_start_time" timestamp with time zone); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."analytics_access_summary"("p_project_id" "text", "p_start_time" timestamp with time zone) FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."analytics_access_summary"("p_project_id" "text", "p_start_time" timestamp with time zone) TO "service_role";


--
-- Name: FUNCTION "analytics_access_timeseries"("p_project_id" "text", "p_start_time" timestamp with time zone, "p_interval" "text", "p_agent_id" "text", "p_node_name" "text"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."analytics_access_timeseries"("p_project_id" "text", "p_start_time" timestamp with time zone, "p_interval" "text", "p_agent_id" "text", "p_node_name" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."analytics_access_timeseries"("p_project_id" "text", "p_start_time" timestamp with time zone, "p_interval" "text", "p_agent_id" "text", "p_node_name" "text") TO "service_role";


--
-- Name: FUNCTION "cas_update_root_hash"("p_project_id" "text", "p_old_hash" "text", "p_new_hash" "text"); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION "public"."cas_update_root_hash"("p_project_id" "text", "p_old_hash" "text", "p_new_hash" "text") TO "anon";
GRANT ALL ON FUNCTION "public"."cas_update_root_hash"("p_project_id" "text", "p_old_hash" "text", "p_new_hash" "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."cas_update_root_hash"("p_project_id" "text", "p_old_hash" "text", "p_new_hash" "text") TO "service_role";


--
-- Name: FUNCTION "cas_update_scope_state"("p_project_id" "text", "p_scope_path" "text", "p_old_hash" "text", "p_new_hash" "text", "p_head_commit_id" "text"); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION "public"."cas_update_scope_state"("p_project_id" "text", "p_scope_path" "text", "p_old_hash" "text", "p_new_hash" "text", "p_head_commit_id" "text") TO "anon";
GRANT ALL ON FUNCTION "public"."cas_update_scope_state"("p_project_id" "text", "p_scope_path" "text", "p_old_hash" "text", "p_new_hash" "text", "p_head_commit_id" "text") TO "authenticated";
GRANT ALL ON FUNCTION "public"."cas_update_scope_state"("p_project_id" "text", "p_scope_path" "text", "p_old_hash" "text", "p_new_hash" "text", "p_head_commit_id" "text") TO "service_role";


--
-- Name: TABLE "organization_billing_operations"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."organization_billing_operations" TO "anon";
GRANT ALL ON TABLE "public"."organization_billing_operations" TO "authenticated";
GRANT ALL ON TABLE "public"."organization_billing_operations" TO "service_role";


--
-- Name: FUNCTION "claim_entitlement_provisioning_batch"("p_limit" integer, "p_lease_seconds" integer); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."claim_entitlement_provisioning_batch"("p_limit" integer, "p_lease_seconds" integer) FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."claim_entitlement_provisioning_batch"("p_limit" integer, "p_lease_seconds" integer) TO "service_role";


--
-- Name: FUNCTION "claim_mut_version_outbox_batch"("p_limit" integer); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."claim_mut_version_outbox_batch"("p_limit" integer) FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."claim_mut_version_outbox_batch"("p_limit" integer) TO "service_role";


--
-- Name: FUNCTION "claim_project_deletion_jobs"("p_worker_id" "text", "p_limit" integer, "p_lease_seconds" integer); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."claim_project_deletion_jobs"("p_worker_id" "text", "p_limit" integer, "p_lease_seconds" integer) FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."claim_project_deletion_jobs"("p_worker_id" "text", "p_limit" integer, "p_lease_seconds" integer) TO "service_role";


--
-- Name: FUNCTION "claim_project_initialization_operations"("p_worker_id" "text", "p_limit" integer, "p_lease_seconds" integer); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."claim_project_initialization_operations"("p_worker_id" "text", "p_limit" integer, "p_lease_seconds" integer) FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."claim_project_initialization_operations"("p_worker_id" "text", "p_limit" integer, "p_lease_seconds" integer) TO "service_role";


--
-- Name: FUNCTION "claim_seat_proposal_batch"("p_limit" integer, "p_lease_seconds" integer); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."claim_seat_proposal_batch"("p_limit" integer, "p_lease_seconds" integer) FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."claim_seat_proposal_batch"("p_limit" integer, "p_lease_seconds" integer) TO "service_role";


--
-- Name: FUNCTION "claim_storage_reconciliation_batch"("p_limit" integer, "p_min_age_seconds" integer, "p_claim_lease_seconds" integer); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."claim_storage_reconciliation_batch"("p_limit" integer, "p_min_age_seconds" integer, "p_claim_lease_seconds" integer) FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."claim_storage_reconciliation_batch"("p_limit" integer, "p_min_age_seconds" integer, "p_claim_lease_seconds" integer) TO "service_role";


--
-- Name: FUNCTION "claim_version_outbox_batch"("p_limit" integer); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."claim_version_outbox_batch"("p_limit" integer) FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."claim_version_outbox_batch"("p_limit" integer) TO "service_role";


--
-- Name: FUNCTION "complete_mut_version_outbox"("p_id" bigint); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."complete_mut_version_outbox"("p_id" bigint) FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."complete_mut_version_outbox"("p_id" bigint) TO "service_role";


--
-- Name: FUNCTION "complete_project_deletion_job"("p_job_id" "text", "p_worker_id" "text"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."complete_project_deletion_job"("p_job_id" "text", "p_worker_id" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."complete_project_deletion_job"("p_job_id" "text", "p_worker_id" "text") TO "service_role";


--
-- Name: FUNCTION "complete_project_initialization"("p_operation_key" "text", "p_project_id" "text", "p_actor_user_id" "uuid"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."complete_project_initialization"("p_operation_key" "text", "p_project_id" "text", "p_actor_user_id" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."complete_project_initialization"("p_operation_key" "text", "p_project_id" "text", "p_actor_user_id" "uuid") TO "service_role";


--
-- Name: FUNCTION "complete_project_storage_inventory"(); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."complete_project_storage_inventory"() FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."complete_project_storage_inventory"() TO "service_role";


--
-- Name: FUNCTION "complete_version_outbox"("p_id" bigint); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."complete_version_outbox"("p_id" bigint) FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."complete_version_outbox"("p_id" bigint) TO "service_role";


--
-- Name: FUNCTION "count_billable_organization_members"("p_org_id" "text"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."count_billable_organization_members"("p_org_id" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."count_billable_organization_members"("p_org_id" "text") TO "service_role";


--
-- Name: FUNCTION "count_children_batch"("p_parent_ids" "text"[]); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION "public"."count_children_batch"("p_parent_ids" "text"[]) TO "anon";
GRANT ALL ON FUNCTION "public"."count_children_batch"("p_parent_ids" "text"[]) TO "authenticated";
GRANT ALL ON FUNCTION "public"."count_children_batch"("p_parent_ids" "text"[]) TO "service_role";


--
-- Name: FUNCTION "create_project_idempotent"("p_operation_key" "text", "p_payload_hash" "text", "p_project_id" "text", "p_name" "text", "p_description" "text", "p_org_id" "text", "p_created_by" "uuid", "p_share_token" "text", "p_publication_mode" "text", "p_project_limit" integer, "p_request_hash" "text", "p_result_metadata" "jsonb"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."create_project_idempotent"("p_operation_key" "text", "p_payload_hash" "text", "p_project_id" "text", "p_name" "text", "p_description" "text", "p_org_id" "text", "p_created_by" "uuid", "p_share_token" "text", "p_publication_mode" "text", "p_project_limit" integer, "p_request_hash" "text", "p_result_metadata" "jsonb") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."create_project_idempotent"("p_operation_key" "text", "p_payload_hash" "text", "p_project_id" "text", "p_name" "text", "p_description" "text", "p_org_id" "text", "p_created_by" "uuid", "p_share_token" "text", "p_publication_mode" "text", "p_project_limit" integer, "p_request_hash" "text", "p_result_metadata" "jsonb") TO "service_role";


--
-- Name: FUNCTION "dead_letter_project_initialization_operation"("p_operation_key" "text", "p_actor_user_id" "uuid", "p_worker_id" "text", "p_error" "text"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."dead_letter_project_initialization_operation"("p_operation_key" "text", "p_actor_user_id" "uuid", "p_worker_id" "text", "p_error" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."dead_letter_project_initialization_operation"("p_operation_key" "text", "p_actor_user_id" "uuid", "p_worker_id" "text", "p_error" "text") TO "service_role";


--
-- Name: FUNCTION "delete_empty_organization_control_plane"("p_org_id" "text", "p_actor_user_id" "uuid"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."delete_empty_organization_control_plane"("p_org_id" "text", "p_actor_user_id" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."delete_empty_organization_control_plane"("p_org_id" "text", "p_actor_user_id" "uuid") TO "service_role";


--
-- Name: FUNCTION "delete_project_control_plane"("p_project_id" "text", "p_actor_user_id" "uuid", "p_quiescence_seconds" integer); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."delete_project_control_plane"("p_project_id" "text", "p_actor_user_id" "uuid", "p_quiescence_seconds" integer) FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."delete_project_control_plane"("p_project_id" "text", "p_actor_user_id" "uuid", "p_quiescence_seconds" integer) TO "service_role";


--
-- Name: FUNCTION "drain_project_deletion_job"("p_job_id" "text", "p_worker_id" "text"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."drain_project_deletion_job"("p_job_id" "text", "p_worker_id" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."drain_project_deletion_job"("p_job_id" "text", "p_worker_id" "text") TO "service_role";


--
-- Name: FUNCTION "enqueue_missing_entitlement_provisioning"("p_limit" integer); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."enqueue_missing_entitlement_provisioning"("p_limit" integer) FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."enqueue_missing_entitlement_provisioning"("p_limit" integer) TO "service_role";


--
-- Name: TABLE "access_surfaces"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."access_surfaces" TO "anon";
GRANT ALL ON TABLE "public"."access_surfaces" TO "authenticated";
GRANT ALL ON TABLE "public"."access_surfaces" TO "service_role";


--
-- Name: FUNCTION "ensure_repository_target_access_surfaces"("p_project_id" "text", "p_scope_id" "text", "p_created_by" "uuid", "p_git_surface_id" "text", "p_cli_surface_id" "text"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."ensure_repository_target_access_surfaces"("p_project_id" "text", "p_scope_id" "text", "p_created_by" "uuid", "p_git_surface_id" "text", "p_cli_surface_id" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."ensure_repository_target_access_surfaces"("p_project_id" "text", "p_scope_id" "text", "p_created_by" "uuid", "p_git_surface_id" "text", "p_cli_surface_id" "text") TO "service_role";


--
-- Name: FUNCTION "fail_mut_version_outbox"("p_id" bigint, "p_error" "text"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."fail_mut_version_outbox"("p_id" bigint, "p_error" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."fail_mut_version_outbox"("p_id" bigint, "p_error" "text") TO "service_role";


--
-- Name: FUNCTION "fail_project_deletion_job"("p_job_id" "text", "p_worker_id" "text", "p_error" "text", "p_retry_after_seconds" integer); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."fail_project_deletion_job"("p_job_id" "text", "p_worker_id" "text", "p_error" "text", "p_retry_after_seconds" integer) FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."fail_project_deletion_job"("p_job_id" "text", "p_worker_id" "text", "p_error" "text", "p_retry_after_seconds" integer) TO "service_role";


--
-- Name: FUNCTION "fail_project_initialization_operation"("p_operation_key" "text", "p_actor_user_id" "uuid", "p_worker_id" "text", "p_error" "text", "p_retry_after_seconds" integer); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."fail_project_initialization_operation"("p_operation_key" "text", "p_actor_user_id" "uuid", "p_worker_id" "text", "p_error" "text", "p_retry_after_seconds" integer) FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."fail_project_initialization_operation"("p_operation_key" "text", "p_actor_user_id" "uuid", "p_worker_id" "text", "p_error" "text", "p_retry_after_seconds" integer) TO "service_role";


--
-- Name: FUNCTION "fail_version_outbox"("p_id" bigint, "p_error" "text"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."fail_version_outbox"("p_id" bigint, "p_error" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."fail_version_outbox"("p_id" bigint, "p_error" "text") TO "service_role";


--
-- Name: FUNCTION "finalize_project_storage_inventory_scan"("p_observed_object_count" bigint, "p_observed_multipart_count" bigint, "p_inventory_digest" "text"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."finalize_project_storage_inventory_scan"("p_observed_object_count" bigint, "p_observed_multipart_count" bigint, "p_inventory_digest" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."finalize_project_storage_inventory_scan"("p_observed_object_count" bigint, "p_observed_multipart_count" bigint, "p_inventory_digest" "text") TO "service_role";


--
-- Name: FUNCTION "get_mut_project_write_state"("p_project_id" "text", "p_user_id" "text"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."get_mut_project_write_state"("p_project_id" "text", "p_user_id" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."get_mut_project_write_state"("p_project_id" "text", "p_user_id" "text") TO "service_role";


--
-- Name: FUNCTION "get_project_create_operation_replay"("p_operation_key" "text", "p_actor_user_id" "uuid", "p_request_hash" "text"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."get_project_create_operation_replay"("p_operation_key" "text", "p_actor_user_id" "uuid", "p_request_hash" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."get_project_create_operation_replay"("p_operation_key" "text", "p_actor_user_id" "uuid", "p_request_hash" "text") TO "service_role";


--
-- Name: FUNCTION "get_version_project_history_refs"("p_project_id" "text"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."get_version_project_history_refs"("p_project_id" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."get_version_project_history_refs"("p_project_id" "text") TO "service_role";


--
-- Name: FUNCTION "get_version_project_write_state"("p_project_id" "text", "p_user_id" "text"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."get_version_project_write_state"("p_project_id" "text", "p_user_id" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."get_version_project_write_state"("p_project_id" "text", "p_user_id" "text") TO "service_role";


--
-- Name: FUNCTION "handle_new_user"(); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION "public"."handle_new_user"() TO "anon";
GRANT ALL ON FUNCTION "public"."handle_new_user"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."handle_new_user"() TO "service_role";


--
-- Name: FUNCTION "is_billable_organization_member"("p_org_id" "text", "p_user_id" "uuid"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."is_billable_organization_member"("p_org_id" "text", "p_user_id" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."is_billable_organization_member"("p_org_id" "text", "p_user_id" "uuid") TO "service_role";


--
-- Name: FUNCTION "issue_user_git_http_credential"("p_credential_id" "text", "p_access_surface_id" "text", "p_org_id" "text", "p_project_id" "text", "p_scope_id" "text", "p_user_id" "uuid", "p_grant_mode" "text", "p_key_prefix" "text", "p_key_last4" "text", "p_key_hash" "text", "p_hash_alg" "text"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."issue_user_git_http_credential"("p_credential_id" "text", "p_access_surface_id" "text", "p_org_id" "text", "p_project_id" "text", "p_scope_id" "text", "p_user_id" "uuid", "p_grant_mode" "text", "p_key_prefix" "text", "p_key_last4" "text", "p_key_hash" "text", "p_hash_alg" "text") FROM PUBLIC;


--
-- Name: FUNCTION "issue_user_git_http_credential_idempotent"("p_operation_key" "text", "p_payload_hash" "text", "p_credential_id" "text", "p_access_surface_id" "text", "p_org_id" "text", "p_project_id" "text", "p_scope_id" "text", "p_user_id" "uuid", "p_grant_mode" "text", "p_key_prefix" "text", "p_key_last4" "text", "p_key_hash" "text", "p_hash_alg" "text"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."issue_user_git_http_credential_idempotent"("p_operation_key" "text", "p_payload_hash" "text", "p_credential_id" "text", "p_access_surface_id" "text", "p_org_id" "text", "p_project_id" "text", "p_scope_id" "text", "p_user_id" "uuid", "p_grant_mode" "text", "p_key_prefix" "text", "p_key_last4" "text", "p_key_hash" "text", "p_hash_alg" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."issue_user_git_http_credential_idempotent"("p_operation_key" "text", "p_payload_hash" "text", "p_credential_id" "text", "p_access_surface_id" "text", "p_org_id" "text", "p_project_id" "text", "p_scope_id" "text", "p_user_id" "uuid", "p_grant_mode" "text", "p_key_prefix" "text", "p_key_last4" "text", "p_key_hash" "text", "p_hash_alg" "text") TO "service_role";


--
-- Name: FUNCTION "join_project_via_share_token"("p_share_token" "text", "p_user_id" "uuid"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."join_project_via_share_token"("p_share_token" "text", "p_user_id" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."join_project_via_share_token"("p_share_token" "text", "p_user_id" "uuid") TO "service_role";


--
-- Name: FUNCTION "list_project_deletion_host_tombstones"(); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."list_project_deletion_host_tombstones"() FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."list_project_deletion_host_tombstones"() TO "service_role";


--
-- Name: FUNCTION "mark_project_storage_orphan_cleaned"("p_project_id" "text", "p_principal" "text"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."mark_project_storage_orphan_cleaned"("p_project_id" "text", "p_principal" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."mark_project_storage_orphan_cleaned"("p_project_id" "text", "p_principal" "text") TO "service_role";


--
-- Name: FUNCTION "normalize_scope_path"(); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION "public"."normalize_scope_path"() TO "anon";
GRANT ALL ON FUNCTION "public"."normalize_scope_path"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."normalize_scope_path"() TO "service_role";


--
-- Name: FUNCTION "oauth_states_purge_expired"(); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION "public"."oauth_states_purge_expired"() TO "anon";
GRANT ALL ON FUNCTION "public"."oauth_states_purge_expired"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."oauth_states_purge_expired"() TO "service_role";


--
-- Name: FUNCTION "persist_project_deletion_external_ingest_snapshot"("p_job_id" "text", "p_worker_id" "text", "p_external_ingest_resources" "jsonb"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."persist_project_deletion_external_ingest_snapshot"("p_job_id" "text", "p_worker_id" "text", "p_external_ingest_resources" "jsonb") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."persist_project_deletion_external_ingest_snapshot"("p_job_id" "text", "p_worker_id" "text", "p_external_ingest_resources" "jsonb") TO "service_role";


--
-- Name: FUNCTION "project_creator_authorization_preflight"(); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."project_creator_authorization_preflight"() FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."project_creator_authorization_preflight"() TO "service_role";


--
-- Name: FUNCTION "project_storage_inventory_status"(); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."project_storage_inventory_status"() FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."project_storage_inventory_status"() TO "service_role";


--
-- Name: FUNCTION "publish_mut_project_update"("p_project_id" "text", "p_old_root_hash" "text", "p_new_root_hash" "text", "p_head_commit_id" "text", "p_who" "text", "p_message" "text", "p_event_type" "text", "p_changes" "jsonb", "p_conflicts" "jsonb", "p_created_at" "text", "p_audit_agent_id" "text", "p_audit_detail" "jsonb", "p_source_channel" "text", "p_policy" "text", "p_base_commit_id" "text", "p_client_commit_id" "text", "p_proposed_tree_id" "text", "p_intent_type" "text", "p_scope_path" "text", "p_scope_hash" "text", "p_scope_head_commit_id" "text", "p_expected_scope_head_commit_id" "text"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."publish_mut_project_update"("p_project_id" "text", "p_old_root_hash" "text", "p_new_root_hash" "text", "p_head_commit_id" "text", "p_who" "text", "p_message" "text", "p_event_type" "text", "p_changes" "jsonb", "p_conflicts" "jsonb", "p_created_at" "text", "p_audit_agent_id" "text", "p_audit_detail" "jsonb", "p_source_channel" "text", "p_policy" "text", "p_base_commit_id" "text", "p_client_commit_id" "text", "p_proposed_tree_id" "text", "p_intent_type" "text", "p_scope_path" "text", "p_scope_hash" "text", "p_scope_head_commit_id" "text", "p_expected_scope_head_commit_id" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."publish_mut_project_update"("p_project_id" "text", "p_old_root_hash" "text", "p_new_root_hash" "text", "p_head_commit_id" "text", "p_who" "text", "p_message" "text", "p_event_type" "text", "p_changes" "jsonb", "p_conflicts" "jsonb", "p_created_at" "text", "p_audit_agent_id" "text", "p_audit_detail" "jsonb", "p_source_channel" "text", "p_policy" "text", "p_base_commit_id" "text", "p_client_commit_id" "text", "p_proposed_tree_id" "text", "p_intent_type" "text", "p_scope_path" "text", "p_scope_hash" "text", "p_scope_head_commit_id" "text", "p_expected_scope_head_commit_id" "text") TO "service_role";


--
-- Name: FUNCTION "publish_organization_entitlement"("p_org_id" "text", "p_schema_version" "text", "p_plan_id" "text", "p_status" "text", "p_source" "text", "p_entitlements" "jsonb", "p_seat_quantity" integer, "p_catalog_version" "text", "p_source_revision" bigint, "p_effective_at" timestamp with time zone, "p_effective_until" timestamp with time zone, "p_current_period_end" timestamp with time zone, "p_payload_hash" "text", "p_source_event_id" "text", "p_event_type" "text"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."publish_organization_entitlement"("p_org_id" "text", "p_schema_version" "text", "p_plan_id" "text", "p_status" "text", "p_source" "text", "p_entitlements" "jsonb", "p_seat_quantity" integer, "p_catalog_version" "text", "p_source_revision" bigint, "p_effective_at" timestamp with time zone, "p_effective_until" timestamp with time zone, "p_current_period_end" timestamp with time zone, "p_payload_hash" "text", "p_source_event_id" "text", "p_event_type" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."publish_organization_entitlement"("p_org_id" "text", "p_schema_version" "text", "p_plan_id" "text", "p_status" "text", "p_source" "text", "p_entitlements" "jsonb", "p_seat_quantity" integer, "p_catalog_version" "text", "p_source_revision" bigint, "p_effective_at" timestamp with time zone, "p_effective_until" timestamp with time zone, "p_current_period_end" timestamp with time zone, "p_payload_hash" "text", "p_source_event_id" "text", "p_event_type" "text") TO "service_role";


--
-- Name: FUNCTION "publish_organization_entitlement_v2"("p_org_id" "text", "p_schema_version" "text", "p_plan_id" "text", "p_status" "text", "p_source" "text", "p_entitlements" "jsonb", "p_seat_quantity" integer, "p_catalog_version" "text", "p_source_revision" bigint, "p_effective_at" timestamp with time zone, "p_effective_until" timestamp with time zone, "p_current_period_end" timestamp with time zone, "p_payload_hash" "text", "p_source_event_id" "text", "p_source_quote_id" "text", "p_event_type" "text"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."publish_organization_entitlement_v2"("p_org_id" "text", "p_schema_version" "text", "p_plan_id" "text", "p_status" "text", "p_source" "text", "p_entitlements" "jsonb", "p_seat_quantity" integer, "p_catalog_version" "text", "p_source_revision" bigint, "p_effective_at" timestamp with time zone, "p_effective_until" timestamp with time zone, "p_current_period_end" timestamp with time zone, "p_payload_hash" "text", "p_source_event_id" "text", "p_source_quote_id" "text", "p_event_type" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."publish_organization_entitlement_v2"("p_org_id" "text", "p_schema_version" "text", "p_plan_id" "text", "p_status" "text", "p_source" "text", "p_entitlements" "jsonb", "p_seat_quantity" integer, "p_catalog_version" "text", "p_source_revision" bigint, "p_effective_at" timestamp with time zone, "p_effective_until" timestamp with time zone, "p_current_period_end" timestamp with time zone, "p_payload_hash" "text", "p_source_event_id" "text", "p_source_quote_id" "text", "p_event_type" "text") TO "service_role";


--
-- Name: FUNCTION "publish_version_project_update"("p_project_id" "text", "p_old_root_hash" "text", "p_new_root_hash" "text", "p_head_commit_id" "text", "p_who" "text", "p_message" "text", "p_event_type" "text", "p_changes" "jsonb", "p_conflicts" "jsonb", "p_created_at" "text", "p_audit_agent_id" "text", "p_audit_detail" "jsonb", "p_source_channel" "text", "p_policy" "text", "p_base_commit_id" "text", "p_client_commit_id" "text", "p_proposed_tree_id" "text", "p_intent_type" "text", "p_scope_path" "text", "p_scope_hash" "text", "p_scope_head_commit_id" "text", "p_expected_scope_head_commit_id" "text"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."publish_version_project_update"("p_project_id" "text", "p_old_root_hash" "text", "p_new_root_hash" "text", "p_head_commit_id" "text", "p_who" "text", "p_message" "text", "p_event_type" "text", "p_changes" "jsonb", "p_conflicts" "jsonb", "p_created_at" "text", "p_audit_agent_id" "text", "p_audit_detail" "jsonb", "p_source_channel" "text", "p_policy" "text", "p_base_commit_id" "text", "p_client_commit_id" "text", "p_proposed_tree_id" "text", "p_intent_type" "text", "p_scope_path" "text", "p_scope_hash" "text", "p_scope_head_commit_id" "text", "p_expected_scope_head_commit_id" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."publish_version_project_update"("p_project_id" "text", "p_old_root_hash" "text", "p_new_root_hash" "text", "p_head_commit_id" "text", "p_who" "text", "p_message" "text", "p_event_type" "text", "p_changes" "jsonb", "p_conflicts" "jsonb", "p_created_at" "text", "p_audit_agent_id" "text", "p_audit_detail" "jsonb", "p_source_channel" "text", "p_policy" "text", "p_base_commit_id" "text", "p_client_commit_id" "text", "p_proposed_tree_id" "text", "p_intent_type" "text", "p_scope_path" "text", "p_scope_hash" "text", "p_scope_head_commit_id" "text", "p_expected_scope_head_commit_id" "text") TO "service_role";


--
-- Name: FUNCTION "publish_version_project_update_with_usage"("p_project_id" "text", "p_old_root_hash" "text", "p_new_root_hash" "text", "p_head_commit_id" "text", "p_who" "text", "p_message" "text", "p_event_type" "text", "p_changes" "jsonb", "p_conflicts" "jsonb", "p_created_at" "text", "p_audit_agent_id" "text", "p_audit_detail" "jsonb", "p_source_channel" "text", "p_policy" "text", "p_base_commit_id" "text", "p_client_commit_id" "text", "p_proposed_tree_id" "text", "p_intent_type" "text", "p_scope_path" "text", "p_scope_hash" "text", "p_scope_head_commit_id" "text", "p_expected_scope_head_commit_id" "text", "p_org_id" "text", "p_storage_old_value" bigint, "p_storage_delta" bigint, "p_storage_limit" bigint, "p_storage_enforce" boolean, "p_entitlement_source_revision" bigint); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."publish_version_project_update_with_usage"("p_project_id" "text", "p_old_root_hash" "text", "p_new_root_hash" "text", "p_head_commit_id" "text", "p_who" "text", "p_message" "text", "p_event_type" "text", "p_changes" "jsonb", "p_conflicts" "jsonb", "p_created_at" "text", "p_audit_agent_id" "text", "p_audit_detail" "jsonb", "p_source_channel" "text", "p_policy" "text", "p_base_commit_id" "text", "p_client_commit_id" "text", "p_proposed_tree_id" "text", "p_intent_type" "text", "p_scope_path" "text", "p_scope_hash" "text", "p_scope_head_commit_id" "text", "p_expected_scope_head_commit_id" "text", "p_org_id" "text", "p_storage_old_value" bigint, "p_storage_delta" bigint, "p_storage_limit" bigint, "p_storage_enforce" boolean, "p_entitlement_source_revision" bigint) FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."publish_version_project_update_with_usage"("p_project_id" "text", "p_old_root_hash" "text", "p_new_root_hash" "text", "p_head_commit_id" "text", "p_who" "text", "p_message" "text", "p_event_type" "text", "p_changes" "jsonb", "p_conflicts" "jsonb", "p_created_at" "text", "p_audit_agent_id" "text", "p_audit_detail" "jsonb", "p_source_channel" "text", "p_policy" "text", "p_base_commit_id" "text", "p_client_commit_id" "text", "p_proposed_tree_id" "text", "p_intent_type" "text", "p_scope_path" "text", "p_scope_hash" "text", "p_scope_head_commit_id" "text", "p_expected_scope_head_commit_id" "text", "p_org_id" "text", "p_storage_old_value" bigint, "p_storage_delta" bigint, "p_storage_limit" bigint, "p_storage_enforce" boolean, "p_entitlement_source_revision" bigint) TO "service_role";


--
-- Name: FUNCTION "reconcile_billing_operation_from_entitlement"("p_org_id" "text", "p_operation_id" "text"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."reconcile_billing_operation_from_entitlement"("p_org_id" "text", "p_operation_id" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."reconcile_billing_operation_from_entitlement"("p_org_id" "text", "p_operation_id" "text") TO "service_role";


--
-- Name: FUNCTION "reconcile_organization_usage_counter"("p_org_id" "text", "p_metric" "text", "p_value" bigint, "p_limit" bigint, "p_idempotency_key" "text", "p_source" "text", "p_metadata" "jsonb"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."reconcile_organization_usage_counter"("p_org_id" "text", "p_metric" "text", "p_value" bigint, "p_limit" bigint, "p_idempotency_key" "text", "p_source" "text", "p_metadata" "jsonb") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."reconcile_organization_usage_counter"("p_org_id" "text", "p_metric" "text", "p_value" bigint, "p_limit" bigint, "p_idempotency_key" "text", "p_source" "text", "p_metadata" "jsonb") TO "service_role";


--
-- Name: FUNCTION "record_project_storage_inventory_batch"("p_batch_key" "text", "p_principals" "jsonb", "p_checkpoint" "jsonb", "p_observed_object_count" integer, "p_observed_multipart_count" integer); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."record_project_storage_inventory_batch"("p_batch_key" "text", "p_principals" "jsonb", "p_checkpoint" "jsonb", "p_observed_object_count" integer, "p_observed_multipart_count" integer) FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."record_project_storage_inventory_batch"("p_batch_key" "text", "p_principals" "jsonb", "p_checkpoint" "jsonb", "p_observed_object_count" integer, "p_observed_multipart_count" integer) TO "service_role";


--
-- Name: FUNCTION "register_version_object_gc_candidates"("p_project_id" "text", "p_object_ids" "text"[], "p_now" timestamp with time zone); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."register_version_object_gc_candidates"("p_project_id" "text", "p_object_ids" "text"[], "p_now" timestamp with time zone) FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."register_version_object_gc_candidates"("p_project_id" "text", "p_object_ids" "text"[], "p_now" timestamp with time zone) TO "service_role";


--
-- Name: FUNCTION "release_project_write_lease"("p_lease_id" "uuid", "p_holder_id" "text"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."release_project_write_lease"("p_lease_id" "uuid", "p_holder_id" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."release_project_write_lease"("p_lease_id" "uuid", "p_holder_id" "text") TO "service_role";


--
-- Name: FUNCTION "remove_project_member_authorized"("p_project_id" "text", "p_target_user_id" "uuid", "p_actor_user_id" "uuid"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."remove_project_member_authorized"("p_project_id" "text", "p_target_user_id" "uuid", "p_actor_user_id" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."remove_project_member_authorized"("p_project_id" "text", "p_target_user_id" "uuid", "p_actor_user_id" "uuid") TO "service_role";


--
-- Name: FUNCTION "renew_project_write_lease"("p_lease_id" "uuid", "p_holder_id" "text", "p_ttl_seconds" integer); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."renew_project_write_lease"("p_lease_id" "uuid", "p_holder_id" "text", "p_ttl_seconds" integer) FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."renew_project_write_lease"("p_lease_id" "uuid", "p_holder_id" "text", "p_ttl_seconds" integer) TO "service_role";


--
-- Name: FUNCTION "replace_mcp_surface_policy"("p_surface_id" "text", "p_accesses" "jsonb", "p_tools_policy" "jsonb", "p_bindings" "jsonb"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."replace_mcp_surface_policy"("p_surface_id" "text", "p_accesses" "jsonb", "p_tools_policy" "jsonb", "p_bindings" "jsonb") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."replace_mcp_surface_policy"("p_surface_id" "text", "p_accesses" "jsonb", "p_tools_policy" "jsonb", "p_bindings" "jsonb") TO "service_role";


--
-- Name: FUNCTION "repository_target_integrity_report"(); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."repository_target_integrity_report"() FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."repository_target_integrity_report"() TO "service_role";


--
-- Name: FUNCTION "reserve_billable_member_activation"("p_org_id" "text", "p_subject_user_id" "uuid", "p_actor_user_id" "uuid", "p_invitation_id" "text", "p_role" "text", "p_idempotency_key" "text"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."reserve_billable_member_activation"("p_org_id" "text", "p_subject_user_id" "uuid", "p_actor_user_id" "uuid", "p_invitation_id" "text", "p_role" "text", "p_idempotency_key" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."reserve_billable_member_activation"("p_org_id" "text", "p_subject_user_id" "uuid", "p_actor_user_id" "uuid", "p_invitation_id" "text", "p_role" "text", "p_idempotency_key" "text") TO "service_role";


--
-- Name: FUNCTION "resolve_git_runtime_credential"("p_key_hash" "text"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."resolve_git_runtime_credential"("p_key_hash" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."resolve_git_runtime_credential"("p_key_hash" "text") TO "service_role";


--
-- Name: FUNCTION "resolve_project_role"("p_project_id" "text", "p_user_id" "uuid"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."resolve_project_role"("p_project_id" "text", "p_user_id" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."resolve_project_role"("p_project_id" "text", "p_user_id" "uuid") TO "service_role";


--
-- Name: FUNCTION "revoke_user_git_http_credential"("p_credential_id" "text", "p_project_id" "text", "p_user_id" "uuid"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."revoke_user_git_http_credential"("p_credential_id" "text", "p_project_id" "text", "p_user_id" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."revoke_user_git_http_credential"("p_credential_id" "text", "p_project_id" "text", "p_user_id" "uuid") TO "service_role";


--
-- Name: FUNCTION "rotate_access_surface_bearer_token"("p_access_surface_id" "text", "p_org_id" "text", "p_project_id" "text", "p_key_prefix" "text", "p_key_last4" "text", "p_key_hash" "text", "p_hash_alg" "text", "p_created_by" "uuid", "p_expires_at" timestamp with time zone); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."rotate_access_surface_bearer_token"("p_access_surface_id" "text", "p_org_id" "text", "p_project_id" "text", "p_key_prefix" "text", "p_key_last4" "text", "p_key_hash" "text", "p_hash_alg" "text", "p_created_by" "uuid", "p_expires_at" timestamp with time zone) FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."rotate_access_surface_bearer_token"("p_access_surface_id" "text", "p_org_id" "text", "p_project_id" "text", "p_key_prefix" "text", "p_key_last4" "text", "p_key_hash" "text", "p_hash_alg" "text", "p_created_by" "uuid", "p_expires_at" timestamp with time zone) TO "service_role";


--
-- Name: FUNCTION "rotate_access_surface_git_http_token"("p_access_surface_id" "text", "p_org_id" "text", "p_project_id" "text", "p_grant_mode" "text", "p_key_prefix" "text", "p_key_last4" "text", "p_key_hash" "text", "p_hash_alg" "text", "p_created_by" "uuid", "p_expires_at" timestamp with time zone); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."rotate_access_surface_git_http_token"("p_access_surface_id" "text", "p_org_id" "text", "p_project_id" "text", "p_grant_mode" "text", "p_key_prefix" "text", "p_key_last4" "text", "p_key_hash" "text", "p_hash_alg" "text", "p_created_by" "uuid", "p_expires_at" timestamp with time zone) FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."rotate_access_surface_git_http_token"("p_access_surface_id" "text", "p_org_id" "text", "p_project_id" "text", "p_grant_mode" "text", "p_key_prefix" "text", "p_key_last4" "text", "p_key_hash" "text", "p_hash_alg" "text", "p_created_by" "uuid", "p_expires_at" timestamp with time zone) TO "service_role";


--
-- Name: FUNCTION "schedule_project_deletion_verification"("p_job_id" "text", "p_worker_id" "text", "p_verify_after_seconds" integer); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."schedule_project_deletion_verification"("p_job_id" "text", "p_worker_id" "text", "p_verify_after_seconds" integer) FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."schedule_project_deletion_verification"("p_job_id" "text", "p_worker_id" "text", "p_verify_after_seconds" integer) TO "service_role";


--
-- Name: FUNCTION "sync_github_version_columns"(); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION "public"."sync_github_version_columns"() TO "anon";
GRANT ALL ON FUNCTION "public"."sync_github_version_columns"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."sync_github_version_columns"() TO "service_role";


--
-- Name: FUNCTION "sync_project_version_columns"(); Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON FUNCTION "public"."sync_project_version_columns"() TO "anon";
GRANT ALL ON FUNCTION "public"."sync_project_version_columns"() TO "authenticated";
GRANT ALL ON FUNCTION "public"."sync_project_version_columns"() TO "service_role";


--
-- Name: FUNCTION "sync_version_object_gc_candidates"("p_project_id" "text", "p_object_ids" "text"[], "p_now" timestamp with time zone, "p_quarantine_seconds" integer); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."sync_version_object_gc_candidates"("p_project_id" "text", "p_object_ids" "text"[], "p_now" timestamp with time zone, "p_quarantine_seconds" integer) FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."sync_version_object_gc_candidates"("p_project_id" "text", "p_object_ids" "text"[], "p_now" timestamp with time zone, "p_quarantine_seconds" integer) TO "service_role";


--
-- Name: FUNCTION "transfer_organization_ownership"("p_org_id" "text", "p_current_owner" "uuid", "p_new_owner" "uuid"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."transfer_organization_ownership"("p_org_id" "text", "p_current_owner" "uuid", "p_new_owner" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."transfer_organization_ownership"("p_org_id" "text", "p_current_owner" "uuid", "p_new_owner" "uuid") TO "service_role";


--
-- Name: FUNCTION "unified_authorization_preflight"(); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."unified_authorization_preflight"() FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."unified_authorization_preflight"() TO "service_role";


--
-- Name: FUNCTION "update_project_member_role_authorized"("p_project_id" "text", "p_target_user_id" "uuid", "p_role" "text", "p_actor_user_id" "uuid"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."update_project_member_role_authorized"("p_project_id" "text", "p_target_user_id" "uuid", "p_role" "text", "p_actor_user_id" "uuid") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."update_project_member_role_authorized"("p_project_id" "text", "p_target_user_id" "uuid", "p_role" "text", "p_actor_user_id" "uuid") TO "service_role";


--
-- Name: FUNCTION "verify_project_storage_inventory"("p_observed_object_count" bigint, "p_observed_multipart_count" bigint, "p_inventory_digest" "text"); Type: ACL; Schema: public; Owner: postgres
--

REVOKE ALL ON FUNCTION "public"."verify_project_storage_inventory"("p_observed_object_count" bigint, "p_observed_multipart_count" bigint, "p_inventory_digest" "text") FROM PUBLIC;
GRANT ALL ON FUNCTION "public"."verify_project_storage_inventory"("p_observed_object_count" bigint, "p_observed_multipart_count" bigint, "p_inventory_digest" "text") TO "service_role";


--
-- Name: TABLE "access_logs"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."access_logs" TO "anon";
GRANT ALL ON TABLE "public"."access_logs" TO "authenticated";
GRANT ALL ON TABLE "public"."access_logs" TO "service_role";


--
-- Name: SEQUENCE "access_logs_id_seq"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON SEQUENCE "public"."access_logs_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."access_logs_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."access_logs_id_seq" TO "service_role";


--
-- Name: TABLE "access_surface_credentials"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."access_surface_credentials" TO "anon";
GRANT ALL ON TABLE "public"."access_surface_credentials" TO "authenticated";
GRANT ALL ON TABLE "public"."access_surface_credentials" TO "service_role";


--
-- Name: TABLE "access_surface_policies"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."access_surface_policies" TO "anon";
GRANT ALL ON TABLE "public"."access_surface_policies" TO "authenticated";
GRANT ALL ON TABLE "public"."access_surface_policies" TO "service_role";


--
-- Name: TABLE "access_tools"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."access_tools" TO "anon";
GRANT ALL ON TABLE "public"."access_tools" TO "authenticated";
GRANT ALL ON TABLE "public"."access_tools" TO "service_role";


--
-- Name: TABLE "agent_execution_logs"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."agent_execution_logs" TO "anon";
GRANT ALL ON TABLE "public"."agent_execution_logs" TO "authenticated";
GRANT ALL ON TABLE "public"."agent_execution_logs" TO "service_role";


--
-- Name: SEQUENCE "agent_execution_log_id_seq"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON SEQUENCE "public"."agent_execution_log_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."agent_execution_log_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."agent_execution_log_id_seq" TO "service_role";


--
-- Name: TABLE "agent_logs"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."agent_logs" TO "anon";
GRANT ALL ON TABLE "public"."agent_logs" TO "authenticated";
GRANT ALL ON TABLE "public"."agent_logs" TO "service_role";


--
-- Name: SEQUENCE "agent_logs_id_seq"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON SEQUENCE "public"."agent_logs_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."agent_logs_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."agent_logs_id_seq" TO "service_role";


--
-- Name: TABLE "agent_profiles"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."agent_profiles" TO "anon";
GRANT ALL ON TABLE "public"."agent_profiles" TO "authenticated";
GRANT ALL ON TABLE "public"."agent_profiles" TO "service_role";


--
-- Name: TABLE "api_keys"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."api_keys" TO "anon";
GRANT ALL ON TABLE "public"."api_keys" TO "authenticated";
GRANT ALL ON TABLE "public"."api_keys" TO "service_role";


--
-- Name: TABLE "audit_logs"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."audit_logs" TO "anon";
GRANT ALL ON TABLE "public"."audit_logs" TO "authenticated";
GRANT ALL ON TABLE "public"."audit_logs" TO "service_role";


--
-- Name: SEQUENCE "audit_logs_id_seq"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON SEQUENCE "public"."audit_logs_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."audit_logs_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."audit_logs_id_seq" TO "service_role";


--
-- Name: TABLE "bookmarks"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."bookmarks" TO "anon";
GRANT ALL ON TABLE "public"."bookmarks" TO "authenticated";
GRANT ALL ON TABLE "public"."bookmarks" TO "service_role";


--
-- Name: TABLE "chat_messages"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."chat_messages" TO "anon";
GRANT ALL ON TABLE "public"."chat_messages" TO "authenticated";
GRANT ALL ON TABLE "public"."chat_messages" TO "service_role";


--
-- Name: TABLE "chat_sessions"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."chat_sessions" TO "anon";
GRANT ALL ON TABLE "public"."chat_sessions" TO "authenticated";
GRANT ALL ON TABLE "public"."chat_sessions" TO "service_role";


--
-- Name: TABLE "chunks"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."chunks" TO "anon";
GRANT ALL ON TABLE "public"."chunks" TO "authenticated";
GRANT ALL ON TABLE "public"."chunks" TO "service_role";


--
-- Name: SEQUENCE "chunks_id_seq"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON SEQUENCE "public"."chunks_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."chunks_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."chunks_id_seq" TO "service_role";


--
-- Name: TABLE "connections"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."connections" TO "anon";
GRANT ALL ON TABLE "public"."connections" TO "authenticated";
GRANT ALL ON TABLE "public"."connections" TO "service_role";


--
-- Name: TABLE "connector_runs"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."connector_runs" TO "anon";
GRANT ALL ON TABLE "public"."connector_runs" TO "authenticated";
GRANT ALL ON TABLE "public"."connector_runs" TO "service_role";


--
-- Name: TABLE "connectors"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."connectors" TO "anon";
GRANT ALL ON TABLE "public"."connectors" TO "authenticated";
GRANT ALL ON TABLE "public"."connectors" TO "service_role";


--
-- Name: TABLE "import_jobs"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."import_jobs" TO "anon";
GRANT ALL ON TABLE "public"."import_jobs" TO "authenticated";
GRANT ALL ON TABLE "public"."import_jobs" TO "service_role";


--
-- Name: TABLE "sync_runs"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."sync_runs" TO "anon";
GRANT ALL ON TABLE "public"."sync_runs" TO "authenticated";
GRANT ALL ON TABLE "public"."sync_runs" TO "service_role";


--
-- Name: TABLE "upload_jobs"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."upload_jobs" TO "anon";
GRANT ALL ON TABLE "public"."upload_jobs" TO "authenticated";
GRANT ALL ON TABLE "public"."upload_jobs" TO "service_role";


--
-- Name: TABLE "context_activity_items"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."context_activity_items" TO "anon";
GRANT ALL ON TABLE "public"."context_activity_items" TO "authenticated";
GRANT ALL ON TABLE "public"."context_activity_items" TO "service_role";


--
-- Name: TABLE "context_publishes"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."context_publishes" TO "anon";
GRANT ALL ON TABLE "public"."context_publishes" TO "authenticated";
GRANT ALL ON TABLE "public"."context_publishes" TO "service_role";


--
-- Name: SEQUENCE "context_publish_id_seq"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON SEQUENCE "public"."context_publish_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."context_publish_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."context_publish_id_seq" TO "service_role";


--
-- Name: TABLE "etl_rules"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."etl_rules" TO "anon";
GRANT ALL ON TABLE "public"."etl_rules" TO "authenticated";
GRANT ALL ON TABLE "public"."etl_rules" TO "service_role";


--
-- Name: SEQUENCE "etl_rule_id_seq"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON SEQUENCE "public"."etl_rule_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."etl_rule_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."etl_rule_id_seq" TO "service_role";


--
-- Name: TABLE "fs_path_index"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."fs_path_index" TO "anon";
GRANT ALL ON TABLE "public"."fs_path_index" TO "authenticated";
GRANT ALL ON TABLE "public"."fs_path_index" TO "service_role";


--
-- Name: SEQUENCE "fs_path_index_id_seq"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON SEQUENCE "public"."fs_path_index_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."fs_path_index_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."fs_path_index_id_seq" TO "service_role";


--
-- Name: TABLE "github_integrations"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."github_integrations" TO "anon";
GRANT ALL ON TABLE "public"."github_integrations" TO "authenticated";
GRANT ALL ON TABLE "public"."github_integrations" TO "service_role";


--
-- Name: TABLE "github_sync_log"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."github_sync_log" TO "anon";
GRANT ALL ON TABLE "public"."github_sync_log" TO "authenticated";
GRANT ALL ON TABLE "public"."github_sync_log" TO "service_role";


--
-- Name: TABLE "local_shadow_snapshots"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."local_shadow_snapshots" TO "anon";
GRANT ALL ON TABLE "public"."local_shadow_snapshots" TO "authenticated";
GRANT ALL ON TABLE "public"."local_shadow_snapshots" TO "service_role";


--
-- Name: TABLE "migration_log"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."migration_log" TO "anon";
GRANT ALL ON TABLE "public"."migration_log" TO "authenticated";
GRANT ALL ON TABLE "public"."migration_log" TO "service_role";


--
-- Name: TABLE "version_commits"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."version_commits" TO "anon";
GRANT ALL ON TABLE "public"."version_commits" TO "authenticated";
GRANT ALL ON TABLE "public"."version_commits" TO "service_role";


--
-- Name: TABLE "mut_commits"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."mut_commits" TO "service_role";


--
-- Name: SEQUENCE "mut_commits_id_seq"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON SEQUENCE "public"."mut_commits_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."mut_commits_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."mut_commits_id_seq" TO "service_role";


--
-- Name: TABLE "version_conflicts"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."version_conflicts" TO "anon";
GRANT ALL ON TABLE "public"."version_conflicts" TO "authenticated";
GRANT ALL ON TABLE "public"."version_conflicts" TO "service_role";


--
-- Name: TABLE "mut_conflicts"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."mut_conflicts" TO "service_role";


--
-- Name: SEQUENCE "mut_conflicts_id_seq"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON SEQUENCE "public"."mut_conflicts_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."mut_conflicts_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."mut_conflicts_id_seq" TO "service_role";


--
-- Name: TABLE "version_object_locations"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."version_object_locations" TO "service_role";


--
-- Name: TABLE "mut_object_locations"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."mut_object_locations" TO "service_role";


--
-- Name: TABLE "version_scope_state"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."version_scope_state" TO "anon";
GRANT ALL ON TABLE "public"."version_scope_state" TO "authenticated";
GRANT ALL ON TABLE "public"."version_scope_state" TO "service_role";


--
-- Name: TABLE "mut_scope_state"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."mut_scope_state" TO "service_role";


--
-- Name: SEQUENCE "mut_scope_state_id_seq"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON SEQUENCE "public"."mut_scope_state_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."mut_scope_state_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."mut_scope_state_id_seq" TO "service_role";


--
-- Name: TABLE "version_view_commits"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."version_view_commits" TO "anon";
GRANT ALL ON TABLE "public"."version_view_commits" TO "authenticated";
GRANT ALL ON TABLE "public"."version_view_commits" TO "service_role";


--
-- Name: TABLE "mut_version_index"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."mut_version_index" TO "service_role";


--
-- Name: SEQUENCE "mut_version_index_id_seq"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON SEQUENCE "public"."mut_version_index_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."mut_version_index_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."mut_version_index_id_seq" TO "service_role";


--
-- Name: TABLE "version_outbox"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."version_outbox" TO "anon";
GRANT ALL ON TABLE "public"."version_outbox" TO "authenticated";
GRANT ALL ON TABLE "public"."version_outbox" TO "service_role";


--
-- Name: TABLE "mut_version_outbox"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."mut_version_outbox" TO "service_role";


--
-- Name: SEQUENCE "mut_version_outbox_id_seq"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON SEQUENCE "public"."mut_version_outbox_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."mut_version_outbox_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."mut_version_outbox_id_seq" TO "service_role";


--
-- Name: TABLE "oauth_connections"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."oauth_connections" TO "anon";
GRANT ALL ON TABLE "public"."oauth_connections" TO "authenticated";
GRANT ALL ON TABLE "public"."oauth_connections" TO "service_role";


--
-- Name: SEQUENCE "oauth_connection_id_seq"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON SEQUENCE "public"."oauth_connection_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."oauth_connection_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."oauth_connection_id_seq" TO "service_role";


--
-- Name: TABLE "oauth_states"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."oauth_states" TO "anon";
GRANT ALL ON TABLE "public"."oauth_states" TO "authenticated";
GRANT ALL ON TABLE "public"."oauth_states" TO "service_role";


--
-- Name: TABLE "org_invitations"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."org_invitations" TO "anon";
GRANT ALL ON TABLE "public"."org_invitations" TO "authenticated";
GRANT ALL ON TABLE "public"."org_invitations" TO "service_role";


--
-- Name: TABLE "org_members"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."org_members" TO "anon";
GRANT ALL ON TABLE "public"."org_members" TO "authenticated";
GRANT ALL ON TABLE "public"."org_members" TO "service_role";


--
-- Name: TABLE "organization_entitlement_events"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."organization_entitlement_events" TO "anon";
GRANT ALL ON TABLE "public"."organization_entitlement_events" TO "authenticated";
GRANT ALL ON TABLE "public"."organization_entitlement_events" TO "service_role";


--
-- Name: TABLE "organization_entitlements"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."organization_entitlements" TO "anon";
GRANT ALL ON TABLE "public"."organization_entitlements" TO "authenticated";
GRANT ALL ON TABLE "public"."organization_entitlements" TO "service_role";


--
-- Name: TABLE "organization_usage_counters"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."organization_usage_counters" TO "anon";
GRANT ALL ON TABLE "public"."organization_usage_counters" TO "authenticated";
GRANT ALL ON TABLE "public"."organization_usage_counters" TO "service_role";


--
-- Name: TABLE "organization_usage_events"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."organization_usage_events" TO "anon";
GRANT ALL ON TABLE "public"."organization_usage_events" TO "authenticated";
GRANT ALL ON TABLE "public"."organization_usage_events" TO "service_role";


--
-- Name: TABLE "organizations"; Type: ACL; Schema: public; Owner: postgres
--

GRANT SELECT,INSERT,REFERENCES,TRIGGER,TRUNCATE,MAINTAIN,UPDATE ON TABLE "public"."organizations" TO "anon";
GRANT SELECT,INSERT,REFERENCES,TRIGGER,TRUNCATE,MAINTAIN,UPDATE ON TABLE "public"."organizations" TO "authenticated";
GRANT SELECT,INSERT,REFERENCES,TRIGGER,TRUNCATE,MAINTAIN,UPDATE ON TABLE "public"."organizations" TO "service_role";


--
-- Name: TABLE "profiles"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."profiles" TO "anon";
GRANT ALL ON TABLE "public"."profiles" TO "authenticated";
GRANT ALL ON TABLE "public"."profiles" TO "service_role";


--
-- Name: TABLE "project_storage_inventory_state"; Type: ACL; Schema: public; Owner: postgres
--

GRANT SELECT ON TABLE "public"."project_storage_inventory_state" TO "service_role";


--
-- Name: TABLE "project_storage_orphan_prefixes"; Type: ACL; Schema: public; Owner: postgres
--

GRANT SELECT ON TABLE "public"."project_storage_orphan_prefixes" TO "service_role";


--
-- Name: TABLE "project_storage_principals"; Type: ACL; Schema: public; Owner: postgres
--

GRANT SELECT ON TABLE "public"."project_storage_principals" TO "service_role";


--
-- Name: TABLE "projects"; Type: ACL; Schema: public; Owner: postgres
--

GRANT SELECT,INSERT,REFERENCES,TRIGGER,TRUNCATE,MAINTAIN,UPDATE ON TABLE "public"."projects" TO "anon";
GRANT SELECT,INSERT,REFERENCES,TRIGGER,TRUNCATE,MAINTAIN,UPDATE ON TABLE "public"."projects" TO "authenticated";
GRANT SELECT,REFERENCES,TRIGGER,TRUNCATE,MAINTAIN ON TABLE "public"."projects" TO "service_role";


--
-- Name: COLUMN "projects"."name"; Type: ACL; Schema: public; Owner: postgres
--

GRANT UPDATE("name") ON TABLE "public"."projects" TO "service_role";


--
-- Name: COLUMN "projects"."description"; Type: ACL; Schema: public; Owner: postgres
--

GRANT UPDATE("description") ON TABLE "public"."projects" TO "service_role";


--
-- Name: COLUMN "projects"."updated_at"; Type: ACL; Schema: public; Owner: postgres
--

GRANT UPDATE("updated_at") ON TABLE "public"."projects" TO "service_role";


--
-- Name: COLUMN "projects"."visibility"; Type: ACL; Schema: public; Owner: postgres
--

GRANT UPDATE("visibility") ON TABLE "public"."projects" TO "service_role";


--
-- Name: COLUMN "projects"."mut_root_hash"; Type: ACL; Schema: public; Owner: postgres
--

GRANT UPDATE("mut_root_hash") ON TABLE "public"."projects" TO "service_role";


--
-- Name: COLUMN "projects"."prompt_template"; Type: ACL; Schema: public; Owner: postgres
--

GRANT UPDATE("prompt_template") ON TABLE "public"."projects" TO "service_role";


--
-- Name: COLUMN "projects"."bound_git_branch"; Type: ACL; Schema: public; Owner: postgres
--

GRANT UPDATE("bound_git_branch") ON TABLE "public"."projects" TO "service_role";


--
-- Name: COLUMN "projects"."share_token"; Type: ACL; Schema: public; Owner: postgres
--

GRANT UPDATE("share_token") ON TABLE "public"."projects" TO "service_role";


--
-- Name: COLUMN "projects"."version_root_hash"; Type: ACL; Schema: public; Owner: postgres
--

GRANT UPDATE("version_root_hash") ON TABLE "public"."projects" TO "service_role";


--
-- Name: TABLE "repository_scopes"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."repository_scopes" TO "anon";
GRANT ALL ON TABLE "public"."repository_scopes" TO "authenticated";
GRANT ALL ON TABLE "public"."repository_scopes" TO "service_role";


--
-- Name: TABLE "runtime_billing_runs"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."runtime_billing_runs" TO "anon";
GRANT ALL ON TABLE "public"."runtime_billing_runs" TO "authenticated";
GRANT ALL ON TABLE "public"."runtime_billing_runs" TO "service_role";


--
-- Name: TABLE "sandbox_endpoints"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."sandbox_endpoints" TO "anon";
GRANT ALL ON TABLE "public"."sandbox_endpoints" TO "authenticated";
GRANT ALL ON TABLE "public"."sandbox_endpoints" TO "service_role";


--
-- Name: TABLE "sandbox_execution_sessions"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."sandbox_execution_sessions" TO "service_role";


--
-- Name: TABLE "scope_sandbox_sessions"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."scope_sandbox_sessions" TO "service_role";


--
-- Name: TABLE "scope_sync_events"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."scope_sync_events" TO "anon";
GRANT ALL ON TABLE "public"."scope_sync_events" TO "authenticated";
GRANT ALL ON TABLE "public"."scope_sync_events" TO "service_role";


--
-- Name: SEQUENCE "scope_sync_events_id_seq"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON SEQUENCE "public"."scope_sync_events_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."scope_sync_events_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."scope_sync_events_id_seq" TO "service_role";


--
-- Name: TABLE "scope_sync_settings"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."scope_sync_settings" TO "anon";
GRANT ALL ON TABLE "public"."scope_sync_settings" TO "authenticated";
GRANT ALL ON TABLE "public"."scope_sync_settings" TO "service_role";


--
-- Name: TABLE "subscriptions"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."subscriptions" TO "anon";
GRANT ALL ON TABLE "public"."subscriptions" TO "authenticated";
GRANT ALL ON TABLE "public"."subscriptions" TO "service_role";


--
-- Name: TABLE "sync_changelog"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."sync_changelog" TO "anon";
GRANT ALL ON TABLE "public"."sync_changelog" TO "authenticated";
GRANT ALL ON TABLE "public"."sync_changelog" TO "service_role";


--
-- Name: SEQUENCE "sync_changelog_id_seq"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON SEQUENCE "public"."sync_changelog_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."sync_changelog_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."sync_changelog_id_seq" TO "service_role";


--
-- Name: TABLE "sync_state"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."sync_state" TO "anon";
GRANT ALL ON TABLE "public"."sync_state" TO "authenticated";
GRANT ALL ON TABLE "public"."sync_state" TO "service_role";


--
-- Name: TABLE "tables"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."tables" TO "anon";
GRANT ALL ON TABLE "public"."tables" TO "authenticated";
GRANT ALL ON TABLE "public"."tables" TO "service_role";


--
-- Name: TABLE "tools"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."tools" TO "anon";
GRANT ALL ON TABLE "public"."tools" TO "authenticated";
GRANT ALL ON TABLE "public"."tools" TO "service_role";


--
-- Name: TABLE "upload_items"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."upload_items" TO "anon";
GRANT ALL ON TABLE "public"."upload_items" TO "authenticated";
GRANT ALL ON TABLE "public"."upload_items" TO "service_role";


--
-- Name: TABLE "uploads"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."uploads" TO "anon";
GRANT ALL ON TABLE "public"."uploads" TO "authenticated";
GRANT ALL ON TABLE "public"."uploads" TO "service_role";


--
-- Name: TABLE "version_transactions"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."version_transactions" TO "anon";
GRANT ALL ON TABLE "public"."version_transactions" TO "authenticated";
GRANT ALL ON TABLE "public"."version_transactions" TO "service_role";


--
-- Name: TABLE "version_activity_feed"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."version_activity_feed" TO "anon";
GRANT ALL ON TABLE "public"."version_activity_feed" TO "authenticated";
GRANT ALL ON TABLE "public"."version_activity_feed" TO "service_role";


--
-- Name: TABLE "version_object_gc_candidates"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."version_object_gc_candidates" TO "service_role";


--
-- Name: TABLE "version_object_gc_runs"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."version_object_gc_runs" TO "service_role";


--
-- Name: SEQUENCE "version_object_gc_runs_id_seq"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON SEQUENCE "public"."version_object_gc_runs_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."version_object_gc_runs_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."version_object_gc_runs_id_seq" TO "service_role";


--
-- Name: TABLE "version_project_root_integrity_incidents"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."version_project_root_integrity_incidents" TO "service_role";


--
-- Name: TABLE "version_refs"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."version_refs" TO "anon";
GRANT ALL ON TABLE "public"."version_refs" TO "authenticated";
GRANT ALL ON TABLE "public"."version_refs" TO "service_role";


--
-- Name: TABLE "version_text_index"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."version_text_index" TO "anon";
GRANT ALL ON TABLE "public"."version_text_index" TO "authenticated";
GRANT ALL ON TABLE "public"."version_text_index" TO "service_role";


--
-- Name: SEQUENCE "version_text_index_id_seq"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON SEQUENCE "public"."version_text_index_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."version_text_index_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."version_text_index_id_seq" TO "service_role";


--
-- Name: TABLE "version_text_index_state"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON TABLE "public"."version_text_index_state" TO "anon";
GRANT ALL ON TABLE "public"."version_text_index_state" TO "authenticated";
GRANT ALL ON TABLE "public"."version_text_index_state" TO "service_role";


--
-- Name: SEQUENCE "version_transactions_id_seq"; Type: ACL; Schema: public; Owner: postgres
--

GRANT ALL ON SEQUENCE "public"."version_transactions_id_seq" TO "anon";
GRANT ALL ON SEQUENCE "public"."version_transactions_id_seq" TO "authenticated";
GRANT ALL ON SEQUENCE "public"."version_transactions_id_seq" TO "service_role";


--
-- Name: DEFAULT PRIVILEGES FOR SEQUENCES; Type: DEFAULT ACL; Schema: public; Owner: postgres
--

ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON SEQUENCES TO "postgres";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON SEQUENCES TO "anon";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON SEQUENCES TO "authenticated";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON SEQUENCES TO "service_role";


--
-- Name: DEFAULT PRIVILEGES FOR SEQUENCES; Type: DEFAULT ACL; Schema: public; Owner: supabase_admin
--



--
-- Name: DEFAULT PRIVILEGES FOR FUNCTIONS; Type: DEFAULT ACL; Schema: public; Owner: postgres
--

ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON FUNCTIONS TO "postgres";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON FUNCTIONS TO "anon";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON FUNCTIONS TO "authenticated";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON FUNCTIONS TO "service_role";


--
-- Name: DEFAULT PRIVILEGES FOR FUNCTIONS; Type: DEFAULT ACL; Schema: public; Owner: supabase_admin
--



--
-- Name: DEFAULT PRIVILEGES FOR TABLES; Type: DEFAULT ACL; Schema: public; Owner: postgres
--

ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON TABLES TO "postgres";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON TABLES TO "anon";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON TABLES TO "authenticated";
ALTER DEFAULT PRIVILEGES FOR ROLE "postgres" IN SCHEMA "public" GRANT ALL ON TABLES TO "service_role";


--
-- Name: DEFAULT PRIVILEGES FOR TABLES; Type: DEFAULT ACL; Schema: public; Owner: supabase_admin
--



--
-- PostgreSQL database dump complete
--

-- Application-owned triggers on platform tables.
CREATE TRIGGER on_auth_user_created AFTER INSERT ON auth.users FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();

-- Reviewed fresh-install reference data.
-- Reviewed fresh-install reference data, NOT production data or demo seeds.
-- The inventory starts incomplete. This must not fabricate an S3 inventory
-- completion receipt or enable deletion before its normal admission checks.
INSERT INTO public.project_storage_inventory_state (singleton) VALUES (true);
