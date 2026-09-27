-- ===========================================================================
-- Durable Project initialization control plane
-- ===========================================================================
-- Project creation and user Git credential issuance are externally retried
-- operations.  Their idempotency records deliberately identify a human actor
-- and an operation key; they never identify a device, checkout, or folder.
--
-- Project deletion is a two-part operation: database authorization state is
-- removed transactionally, while Project-owned object prefixes are purged by a
-- retryable control-plane worker.  Deletion jobs and idempotency tombstones do
-- not reference projects so they survive the Project cascade.

BEGIN;

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '15min';

-- A Project row is not a published product resource until L5 has installed
-- its canonical root and the same transaction below marks it ready. Existing
-- rows are backfilled as ready, while every future insert starts unpublished;
-- no legacy insert path may accidentally bypass the lifecycle gate.
ALTER TABLE public.projects
    ADD COLUMN lifecycle_status text;
-- Rows created before this migration were already product-visible. Preserve
-- that fact, then make every future insertion explicitly unpublished by
-- default so no legacy insert can accidentally bypass the lifecycle gate.
UPDATE public.projects SET lifecycle_status = 'ready';
ALTER TABLE public.projects
    ALTER COLUMN lifecycle_status DROP DEFAULT,
    ALTER COLUMN lifecycle_status SET NOT NULL;
ALTER TABLE public.projects
    ADD CONSTRAINT projects_lifecycle_status_check
    CHECK (lifecycle_status IN ('initializing', 'ready', 'deleting'));
CREATE INDEX projects_ready_org_idx
    ON public.projects(org_id, created_at DESC)
    WHERE lifecycle_status = 'ready';

-- There is one creation aggregate and one publication protocol. The previous
-- service-role RPC cannot express an idempotency key or lifecycle completion,
-- so retaining it would leave a permanent half-created-Project escape hatch.
DROP FUNCTION IF EXISTS public.create_project_with_admin(
    text, text, text, text, uuid, text
);

-- Default-name allocation is part of the serialized create transaction.  A
-- frontend may optimistically propose a slot, but two stale/concurrent clients
-- cannot publish duplicate Untitled names because the Organization row is
-- locked before this helper is evaluated.
CREATE FUNCTION public._untitled_project_slot(p_name text)
RETURNS bigint
LANGUAGE plpgsql
IMMUTABLE
SET search_path = pg_catalog, public, pg_temp
AS $$
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
$$;

REVOKE ALL ON FUNCTION public._untitled_project_slot(text)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public._untitled_project_slot(text)
    TO service_role;

CREATE TABLE public.project_create_operations (
    actor_user_id uuid NOT NULL,
    operation_key text NOT NULL,
    payload_hash text NOT NULL,
    request_hash text NOT NULL,
    org_id text NOT NULL,
    project_id text NOT NULL,
    project_snapshot jsonb NOT NULL,
    result_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    publication_mode text NOT NULL,
    status text NOT NULL DEFAULT 'initializing',
    initialization_attempts integer NOT NULL DEFAULT 0,
    initialization_available_at timestamptz NOT NULL DEFAULT now(),
    initialization_deadline_at timestamptz NOT NULL DEFAULT now() + interval '24 hours',
    initialization_claimed_at timestamptz,
    initialization_claimed_by text,
    initialization_last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    ready_at timestamptz,
    replayed_at timestamptz,
    deleted_at timestamptz,
    dead_lettered_at timestamptz,
    PRIMARY KEY (actor_user_id, operation_key),
    CONSTRAINT project_create_operations_key_check
      CHECK (operation_key ~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'),
    CONSTRAINT project_create_operations_hash_check
      CHECK (payload_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT project_create_operations_request_hash_check
      CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT project_create_operations_result_metadata_check
      CHECK (jsonb_typeof(result_metadata) = 'object'),
    CONSTRAINT project_create_operations_status_check
      CHECK (status IN ('initializing', 'ready', 'deleted', 'dead_lettered')),
    CONSTRAINT project_create_operations_publication_mode_check
      CHECK (publication_mode IN ('empty', 'deferred'))
);

CREATE INDEX project_create_operations_project_idx
    ON public.project_create_operations(project_id);
CREATE INDEX project_create_operations_org_created_idx
    ON public.project_create_operations(org_id, created_at DESC);
CREATE INDEX project_create_operations_initialization_claim_idx
    ON public.project_create_operations(
        status, initialization_available_at, created_at
    ) WHERE status = 'initializing';

CREATE TABLE public.git_credential_issue_operations (
    actor_user_id uuid NOT NULL,
    operation_key text NOT NULL,
    payload_hash text NOT NULL,
    org_id text NOT NULL,
    project_id text NOT NULL,
    credential_id text NOT NULL,
    credential_hash text NOT NULL,
    status text NOT NULL DEFAULT 'active',
    created_at timestamptz NOT NULL DEFAULT now(),
    replayed_at timestamptz,
    revoked_at timestamptz,
    PRIMARY KEY (actor_user_id, operation_key),
    CONSTRAINT git_credential_issue_operations_key_check
      CHECK (operation_key ~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'),
    CONSTRAINT git_credential_issue_operations_hash_check
      CHECK (payload_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT git_credential_issue_operations_status_check
      CHECK (status IN ('active', 'revoked', 'deleted'))
);

CREATE INDEX git_credential_issue_operations_project_idx
    ON public.git_credential_issue_operations(project_id);

CREATE TABLE public.project_deletion_jobs (
    id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
    project_id text NOT NULL UNIQUE,
    org_id text NOT NULL,
    requested_by uuid NOT NULL,
    source text NOT NULL,
    source_operation_key text,
    object_prefixes jsonb NOT NULL,
    phase text NOT NULL DEFAULT 'drain',
    status text NOT NULL DEFAULT 'pending',
    attempts integer NOT NULL DEFAULT 0,
    -- Retained as the post-purge verification interval. Write admission itself
    -- is closed and drained through project_write_leases before purge starts.
    quiescence_seconds integer NOT NULL,
    available_at timestamptz NOT NULL,
    claimed_at timestamptz,
    claimed_by text,
    purged_at timestamptz,
    verification_cycles integer NOT NULL DEFAULT 0,
    completed_at timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT project_deletion_jobs_source_check
      CHECK (source IN ('project_delete', 'initialization_abandon', 'publication_abort')),
    CONSTRAINT project_deletion_jobs_status_check
      CHECK (status IN ('pending', 'running', 'failed', 'completed')),
    CONSTRAINT project_deletion_jobs_phase_check
      CHECK (phase IN ('drain', 'purge', 'verify')),
    CONSTRAINT project_deletion_jobs_quiescence_check
      CHECK (quiescence_seconds >= 1800),
    CONSTRAINT project_deletion_jobs_prefixes_check
      CHECK (
        object_prefixes = jsonb_build_array(
            'version/' || project_id || '/',
            'mut/' || project_id || '/',
            'projects/' || project_id || '/'
        )
      )
);

-- Every externally-triggered Project write holds a short renewable lease.
-- Deletion atomically closes admission by moving the Project to `deleting`,
-- then waits for leases to be released or expire before relational deletion
-- and object purge. This is the proof that no already-admitted writer can
-- repopulate a prefix after cleanup; a time-based quiet window alone is not.
CREATE TABLE public.project_write_leases (
    id uuid PRIMARY KEY,
    project_id text NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,
    holder_id text NOT NULL,
    operation text NOT NULL,
    acquired_at timestamptz NOT NULL DEFAULT now(),
    renewed_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    CONSTRAINT project_write_leases_holder_check CHECK (
        holder_id ~ '^[A-Za-z0-9][A-Za-z0-9:._-]{0,255}$'
    ),
    CONSTRAINT project_write_leases_operation_check CHECK (
        operation ~ '^[A-Za-z0-9][A-Za-z0-9:._/-]{0,255}$'
    )
);
CREATE INDEX project_write_leases_active_idx
    ON public.project_write_leases(project_id, expires_at);

CREATE INDEX project_deletion_jobs_claim_idx
    ON public.project_deletion_jobs(status, available_at, created_at);

ALTER TABLE public.project_create_operations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.git_credential_issue_operations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.project_deletion_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.project_write_leases ENABLE ROW LEVEL SECURITY;

-- These journals are private implementation state, not service-role data
-- APIs. Every mutation and worker claim crosses a SECURITY DEFINER function
-- below; otherwise a service caller could forge a tombstone, skip a lease, or
-- enqueue arbitrary object deletion. The table owner used by those functions
-- bypasses RLS without granting the transport principal direct access.
REVOKE ALL ON public.project_create_operations
    FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON public.git_credential_issue_operations
    FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON public.project_deletion_jobs
    FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON public.project_write_leases
    FROM PUBLIC, anon, authenticated, service_role;

-- Human authorization is a publication boundary as well as an ACL decision.
-- An initializing row is deliberately indistinguishable from a missing
-- Project to every ordinary SQL policy consumer.
CREATE OR REPLACE FUNCTION public.resolve_project_role(
    p_project_id text,
    p_user_id uuid
)
RETURNS TABLE(org_id text, effective_role text, grant_source text)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
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

REVOKE ALL ON FUNCTION public.resolve_project_role(text, uuid)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.resolve_project_role(text, uuid)
    TO service_role;

-- Share links are another ordinary discovery path and therefore observe the
-- same publication gate.  The bootstrap token cannot reveal or add members to
-- an initializing Project.
CREATE OR REPLACE FUNCTION public.join_project_via_share_token(
    p_share_token text,
    p_user_id uuid
)
RETURNS TABLE(
    project_id text,
    project_name text,
    role text,
    newly_joined boolean
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
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

REVOKE ALL ON FUNCTION public.join_project_via_share_token(text, uuid)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.join_project_via_share_token(text, uuid)
    TO service_role;

-- Runtime grants fail closed for *all* credential lifecycles, including
-- service/shared credentials which do not flow through resolve_project_role.
CREATE OR REPLACE FUNCTION public.resolve_git_runtime_credential(p_key_hash text)
RETURNS TABLE(
    credential_id text,
    org_id text,
    project_id text,
    access_surface_id text,
    target_kind text,
    scope_id text,
    path_prefix text,
    excludes jsonb,
    target_max_mode text,
    user_id uuid,
    effective_mode text
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
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

REVOKE ALL ON FUNCTION public.resolve_git_runtime_credential(text)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.resolve_git_runtime_credential(text)
    TO service_role;

-- One transaction owns idempotency resolution, tenant membership, quota
-- admission, and hidden Project/Admin preparation. Initial root refs remain a
-- separate, durable L5-owned step; completion below publishes the Project.
CREATE FUNCTION public.create_project_idempotent(
    p_operation_key text,
    p_payload_hash text,
    p_project_id text,
    p_name text,
    p_description text,
    p_org_id text,
    p_created_by uuid,
    p_share_token text,
    p_publication_mode text,
    p_project_limit integer DEFAULT NULL,
    p_request_hash text DEFAULT NULL,
    p_result_metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
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
$$;

REVOKE ALL ON FUNCTION public.create_project_idempotent(
    text, text, text, text, text, text, uuid, text, text, integer, text, jsonb
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.create_project_idempotent(
    text, text, text, text, text, text, uuid, text, text, integer, text, jsonb
) TO service_role;

-- Exact completed replay is intentionally independent of mutable workflow
-- sources (Registry aliases, release availability, landing ticket expiry and
-- preview-object retention). The actor/key plus the source-independent request
-- hash identifies the durable response; initializing work still resumes
-- through the normal workflow and never exposes its hidden Project snapshot.
CREATE FUNCTION public.get_project_create_operation_replay(
    p_operation_key text,
    p_actor_user_id uuid,
    p_request_hash text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
STABLE
SET search_path = pg_catalog, public, pg_temp
AS $$
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
$$;

REVOKE ALL ON FUNCTION public.get_project_create_operation_replay(text, uuid, text)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.get_project_create_operation_replay(text, uuid, text)
    TO service_role;

-- L5 calls this after initialize_project_tree has published the root ref.
-- Completion never writes a ref; it verifies the L5 fact and makes the
-- control-plane operation ready for an API response/replay.
CREATE FUNCTION public.complete_project_initialization(
    p_operation_key text,
    p_project_id text,
    p_actor_user_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
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

REVOKE ALL ON FUNCTION public.complete_project_initialization(text, text, uuid)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.complete_project_initialization(text, text, uuid)
    TO service_role;

CREATE FUNCTION public.claim_project_initialization_operations(
    p_worker_id text,
    p_limit integer DEFAULT 1,
    p_lease_seconds integer DEFAULT 300
)
RETURNS SETOF public.project_create_operations
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
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

CREATE FUNCTION public.fail_project_initialization_operation(
    p_operation_key text,
    p_actor_user_id uuid,
    p_worker_id text,
    p_error text,
    p_retry_after_seconds integer DEFAULT 60
)
RETURNS boolean
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
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

-- A worker that exhausts the retry/deadline budget must stop hot-looping even
-- when fail-closed abandonment detects unexpected user state.  The Project
-- remains hidden and an operator can inspect the durable error; the ordinary
-- safe path below deletes the empty aggregate and writes a cleanup tombstone.
CREATE FUNCTION public.dead_letter_project_initialization_operation(
    p_operation_key text,
    p_actor_user_id uuid,
    p_worker_id text,
    p_error text
)
RETURNS boolean
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
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

REVOKE ALL ON FUNCTION public.claim_project_initialization_operations(text, integer, integer)
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.fail_project_initialization_operation(
    text, uuid, text, text, integer
) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.dead_letter_project_initialization_operation(
    text, uuid, text, text
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.claim_project_initialization_operations(
    text, integer, integer
) TO service_role;
GRANT EXECUTE ON FUNCTION public.fail_project_initialization_operation(
    text, uuid, text, text, integer
) TO service_role;
GRANT EXECUTE ON FUNCTION public.dead_letter_project_initialization_operation(
    text, uuid, text, text
) TO service_role;

-- Raw Git secrets are supplied by the trusted Desktop main process and are
-- never persisted here.  The operation stores only the canonical payload hash
-- and the same keyed credential hash used by the runtime auth resolver.
CREATE FUNCTION public.issue_user_git_http_credential_idempotent(
    p_operation_key text,
    p_payload_hash text,
    p_credential_id text,
    p_access_surface_id text,
    p_org_id text,
    p_project_id text,
    p_scope_id text,
    p_user_id uuid,
    p_grant_mode text,
    p_key_prefix text,
    p_key_last4 text,
    p_key_hash text,
    p_hash_alg text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
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
$$;

REVOKE ALL ON FUNCTION public.issue_user_git_http_credential_idempotent(
    text, text, text, text, text, text, text, uuid, text, text, text, text, text
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.issue_user_git_http_credential_idempotent(
    text, text, text, text, text, text, text, uuid, text, text, text, text, text
) TO service_role;

-- The older function remains an internal SECURITY DEFINER implementation
-- detail for the idempotent wrapper above.  Service callers must not be able
-- to invoke it directly and bypass operation replay or the ready gate.
REVOKE ALL ON FUNCTION public.issue_user_git_http_credential(
    text, text, text, text, text, uuid, text, text, text, text, text
) FROM service_role;

-- Deferred/contentful creation is never user-visible before completion.  If
-- its owner reports failure, or the reconciler claims it after the durable
-- deadline, the whole unpublished aggregate can therefore be removed without
-- pretending that partially written content is a valid Project.
CREATE FUNCTION public.abort_deferred_project_publication(
    p_project_id text,
    p_operation_key text,
    p_actor_user_id uuid,
    p_quiescence_seconds integer DEFAULT 3600,
    p_worker_id text DEFAULT NULL
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
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

REVOKE ALL ON FUNCTION public.abort_deferred_project_publication(
    text, text, uuid, integer, text
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.abort_deferred_project_publication(
    text, text, uuid, integer, text
) TO service_role;

-- Keep Abandon fail-closed as the schema grows.  Every direct FK whose delete
-- action mutates a child row (CASCADE, SET NULL, or SET DEFAULT) is a user-
-- visible initialization side effect unless abandon_project_initialization
-- explicitly proves that exact table is still canonical bootstrap state.
CREATE FUNCTION public._project_initialization_has_cascade_dependents(
    p_project_id text
)
RETURNS boolean
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
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
$$;

REVOKE ALL ON FUNCTION public._project_initialization_has_cascade_dependents(text)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public._project_initialization_has_cascade_dependents(text)
    TO service_role;

-- Abandon is intentionally narrower than ordinary Project deletion.  It is
-- valid only for the Project created by this actor/operation while the
-- canonical roots are still empty and no user resource was published.
CREATE FUNCTION public.abandon_project_initialization(
    p_project_id text,
    p_operation_key text,
    p_actor_user_id uuid,
    p_quiescence_seconds integer DEFAULT 3600,
    p_worker_id text DEFAULT NULL
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
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

REVOKE ALL ON FUNCTION public.abandon_project_initialization(
    text, text, uuid, integer, text
)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.abandon_project_initialization(
    text, text, uuid, integer, text
)
    TO service_role;

CREATE FUNCTION public.acquire_project_write_lease(
    p_project_id text,
    p_lease_id uuid,
    p_holder_id text,
    p_operation text,
    p_ttl_seconds integer DEFAULT 120,
    p_initialization_operation_key text DEFAULT NULL,
    p_initialization_actor uuid DEFAULT NULL,
    p_initialization_worker text DEFAULT NULL
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
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
$$;

CREATE FUNCTION public.renew_project_write_lease(
    p_lease_id uuid,
    p_holder_id text,
    p_ttl_seconds integer DEFAULT 120
)
RETURNS boolean
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
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

CREATE FUNCTION public.release_project_write_lease(
    p_lease_id uuid,
    p_holder_id text
)
RETURNS boolean
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DELETE FROM public.project_write_leases
WHERE id = p_lease_id AND holder_id = p_holder_id
RETURNING true;
$$;

REVOKE ALL ON FUNCTION public.acquire_project_write_lease(
    text, uuid, text, text, integer, text, uuid, text
) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.renew_project_write_lease(uuid, text, integer)
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.release_project_write_lease(uuid, text)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.acquire_project_write_lease(
    text, uuid, text, text, integer, text, uuid, text
) TO service_role;
GRANT EXECUTE ON FUNCTION public.renew_project_write_lease(uuid, text, integer)
    TO service_role;
GRANT EXECUTE ON FUNCTION public.release_project_write_lease(uuid, text)
    TO service_role;

-- Ordinary Project deletion first closes write admission. Relational state is
-- removed only after the durable worker proves every admitted writer released
-- or lost its renewable lease.
CREATE FUNCTION public.delete_project_control_plane(
    p_project_id text,
    p_actor_user_id uuid,
    p_quiescence_seconds integer DEFAULT 3600
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
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

REVOKE ALL ON FUNCTION public.delete_project_control_plane(text, uuid, integer)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.delete_project_control_plane(text, uuid, integer)
    TO service_role;

CREATE FUNCTION public.drain_project_deletion_job(
    p_job_id text,
    p_worker_id text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    job public.project_deletion_jobs%ROWTYPE;
    active_count bigint;
    next_expiry timestamptz;
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

REVOKE ALL ON FUNCTION public.drain_project_deletion_job(text, text)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.drain_project_deletion_job(text, text)
    TO service_role;

CREATE FUNCTION public.claim_project_deletion_jobs(
    p_worker_id text,
    p_limit integer DEFAULT 10,
    p_lease_seconds integer DEFAULT 300
)
RETURNS SETOF public.project_deletion_jobs
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
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

CREATE FUNCTION public.complete_project_deletion_job(
    p_job_id text,
    p_worker_id text
)
RETURNS boolean
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
UPDATE public.project_deletion_jobs
SET status = 'completed', completed_at = now(), updated_at = now()
WHERE id = p_job_id
  AND phase = 'verify'
  AND status = 'running'
  AND claimed_by = p_worker_id
RETURNING true;
$$;

-- A purge never completes a job directly.  It schedules a second observation
-- after a quiet interval.  If that observation finds a late object, the worker
-- purges again and schedules another full verification window.
CREATE FUNCTION public.schedule_project_deletion_verification(
    p_job_id text,
    p_worker_id text,
    p_verify_after_seconds integer DEFAULT 60
)
RETURNS boolean
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
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

CREATE FUNCTION public.fail_project_deletion_job(
    p_job_id text,
    p_worker_id text,
    p_error text,
    p_retry_after_seconds integer DEFAULT 60
)
RETURNS boolean
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
UPDATE public.project_deletion_jobs
SET status = 'failed',
    available_at = now() + make_interval(secs => GREATEST(p_retry_after_seconds, 1)),
    last_error = left(p_error, 2000),
    updated_at = now()
WHERE id = p_job_id AND status = 'running' AND claimed_by = p_worker_id
RETURNING true;
$$;

REVOKE ALL ON FUNCTION public.claim_project_deletion_jobs(text, integer, integer)
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.complete_project_deletion_job(text, text)
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.schedule_project_deletion_verification(text, text, integer)
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.fail_project_deletion_job(text, text, text, integer)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.claim_project_deletion_jobs(text, integer, integer)
    TO service_role;
GRANT EXECUTE ON FUNCTION public.complete_project_deletion_job(text, text)
    TO service_role;
GRANT EXECUTE ON FUNCTION public.schedule_project_deletion_verification(text, text, integer)
    TO service_role;
GRANT EXECUTE ON FUNCTION public.fail_project_deletion_job(text, text, text, integer)
    TO service_role;

-- The service role is an application transport principal, not a schema owner.
-- Project creation, publication and deletion must cross the SECURITY DEFINER
-- control-plane functions above. Keep ordinary metadata/root updates working
-- through a fail-closed column allowlist while denying raw INSERT and every
-- direct lifecycle transition. New columns receive no implicit write access.
REVOKE INSERT, UPDATE ON public.projects FROM service_role;
GRANT UPDATE (
    name,
    description,
    visibility,
    bound_git_branch,
    prompt_template,
    share_token,
    mut_root_hash,
    version_root_hash,
    updated_at
) ON public.projects TO service_role;

COMMIT;
