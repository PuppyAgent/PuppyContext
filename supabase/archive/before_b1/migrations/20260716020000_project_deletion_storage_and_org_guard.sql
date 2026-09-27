-- Close the two remaining destructive-control-plane gaps:
--
-- 1. Organization deletion must never cascade Projects around the durable
--    Project deletion journal.  Organization deletion is therefore allowed
--    only after every Project has been deleted through
--    delete_project_control_plane and its tombstone has been published.
-- 2. A Project deletion job must snapshot every physical object namespace
--    while the Project/Uploads rows still exist.  The cleanup worker accepts
--    only the exact, persisted allowlist assembled here.

BEGIN;

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '15min';

-- Legacy ETL keys put the actor before the Project in the physical path.  An
-- Upload row can be deleted independently of its S3 artifacts, so querying
-- only currently-live Uploads during Project deletion would lose ownership
-- history.  This compact ledger remembers every principal that has ever owned
-- such a key for the life of the Project.
CREATE TABLE public.project_storage_principals (
    project_id text NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,
    principal text NOT NULL,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, principal),
    CONSTRAINT project_storage_principals_segment_check CHECK (
        project_id ~ '^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$'
        AND principal ~ '^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$'
    )
);

-- Deletion stays fail-closed until a one-time inventory has reconstructed
-- principals for legacy `users/{principal}/.../{project}/` objects whose
-- Upload rows may already be gone. Inventory batches are idempotent so the
-- scan can resume safely after any process or network failure.
CREATE TABLE public.project_storage_inventory_state (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    inventory_complete boolean NOT NULL DEFAULT false,
    checkpoint jsonb NOT NULL DEFAULT '{}'::jsonb,
    observed_object_count bigint NOT NULL DEFAULT 0 CHECK (observed_object_count >= 0),
    observed_multipart_count bigint NOT NULL DEFAULT 0 CHECK (observed_multipart_count >= 0),
    inventory_digest text CHECK (inventory_digest ~ '^[0-9a-f]{64}$'),
    verification_object_count bigint CHECK (verification_object_count >= 0),
    verification_multipart_count bigint CHECK (verification_multipart_count >= 0),
    verification_digest text CHECK (verification_digest ~ '^[0-9a-f]{64}$'),
    completed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO public.project_storage_inventory_state (singleton)
VALUES (true) ON CONFLICT (singleton) DO NOTHING;

CREATE TABLE public.project_storage_inventory_batches (
    batch_key text PRIMARY KEY,
    checkpoint jsonb NOT NULL,
    observed_object_count integer NOT NULL CHECK (observed_object_count >= 0),
    observed_multipart_count integer NOT NULL CHECK (observed_multipart_count >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT project_storage_inventory_batch_key_check CHECK (
        batch_key ~ '^[0-9a-f]{64}$'
    )
);

CREATE TABLE public.project_storage_orphan_prefixes (
    project_id text NOT NULL,
    principal text NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'cleaned')),
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    cleaned_at timestamptz,
    PRIMARY KEY (project_id, principal),
    CONSTRAINT project_storage_orphan_prefixes_segment_check CHECK (
        project_id ~ '^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$'
        AND principal ~ '^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$'
    )
);

ALTER TABLE public.project_storage_principals ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.project_storage_inventory_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.project_storage_inventory_batches ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.project_storage_orphan_prefixes ENABLE ROW LEVEL SECURITY;
CREATE POLICY project_storage_principals_service_role_read
    ON public.project_storage_principals FOR SELECT TO service_role
    USING (true);
REVOKE ALL ON public.project_storage_principals
    FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON public.project_storage_inventory_state
    FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON public.project_storage_inventory_batches
    FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON public.project_storage_orphan_prefixes
    FROM PUBLIC, anon, authenticated, service_role;
GRANT SELECT ON public.project_storage_principals TO service_role;
GRANT SELECT ON public.project_storage_inventory_state TO service_role;
GRANT SELECT ON public.project_storage_orphan_prefixes TO service_role;

INSERT INTO public.project_storage_principals (project_id, principal)
SELECT DISTINCT upload.project_id, COALESCE(upload.created_by::text, upload.project_id)
FROM public.uploads upload
ON CONFLICT (project_id, principal) DO NOTHING;

CREATE FUNCTION public.record_project_storage_inventory_batch(
    p_batch_key text,
    p_principals jsonb,
    p_checkpoint jsonb,
    p_observed_object_count integer,
    p_observed_multipart_count integer
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
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
$$;

CREATE FUNCTION public.mark_project_storage_orphan_cleaned(
    p_project_id text,
    p_principal text
)
RETURNS boolean
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
UPDATE public.project_storage_orphan_prefixes
SET status = 'cleaned', cleaned_at = now()
WHERE project_id = p_project_id
  AND principal = p_principal
  AND status = 'pending'
RETURNING true;
$$;

CREATE FUNCTION public.finalize_project_storage_inventory_scan(
    p_observed_object_count bigint,
    p_observed_multipart_count bigint,
    p_inventory_digest text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
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
$$;

CREATE FUNCTION public.verify_project_storage_inventory(
    p_observed_object_count bigint,
    p_observed_multipart_count bigint,
    p_inventory_digest text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
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
$$;

CREATE FUNCTION public.complete_project_storage_inventory()
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
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

REVOKE ALL ON FUNCTION public.record_project_storage_inventory_batch(
    text, jsonb, jsonb, integer, integer
) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.mark_project_storage_orphan_cleaned(text, text)
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.finalize_project_storage_inventory_scan(
    bigint, bigint, text
) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.verify_project_storage_inventory(bigint, bigint, text)
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.complete_project_storage_inventory()
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.record_project_storage_inventory_batch(
    text, jsonb, jsonb, integer, integer
) TO service_role;
GRANT EXECUTE ON FUNCTION public.mark_project_storage_orphan_cleaned(text, text)
    TO service_role;
GRANT EXECUTE ON FUNCTION public.finalize_project_storage_inventory_scan(
    bigint, bigint, text
) TO service_role;
GRANT EXECUTE ON FUNCTION public.verify_project_storage_inventory(bigint, bigint, text)
    TO service_role;
GRANT EXECUTE ON FUNCTION public.complete_project_storage_inventory()
    TO service_role;

-- Execution sandboxes historically recorded only an opaque provider handle,
-- which made Project deletion unable to discover and terminate them. New
-- sessions always persist ownership; legacy nullable rows remain reaper-owned.
ALTER TABLE public.sandbox_execution_sessions
    ADD COLUMN project_id text REFERENCES public.projects(id) ON DELETE CASCADE;
CREATE INDEX sandbox_execution_sessions_project_idx
    ON public.sandbox_execution_sessions(project_id)
    WHERE project_id IS NOT NULL;

CREATE FUNCTION public._remember_upload_storage_principal()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
    INSERT INTO public.project_storage_principals (project_id, principal)
    VALUES (NEW.project_id, COALESCE(NEW.created_by::text, NEW.project_id))
    ON CONFLICT (project_id, principal) DO NOTHING;
    RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION public._remember_upload_storage_principal()
    FROM PUBLIC, anon, authenticated;

CREATE TRIGGER uploads_remember_storage_principal
AFTER INSERT OR UPDATE OF project_id, created_by ON public.uploads
FOR EACH ROW
EXECUTE FUNCTION public._remember_upload_storage_principal();

CREATE FUNCTION public._project_deletion_storage_principals(
    p_project_id text,
    p_requested_by uuid
)
RETURNS jsonb
LANGUAGE sql
STABLE
SET search_path = pg_catalog, public, pg_temp
AS $$
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
$$;

CREATE FUNCTION public._project_deletion_object_prefixes(
    p_project_id text,
    p_storage_principals jsonb
)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
SET search_path = pg_catalog, public, pg_temp
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

CREATE FUNCTION public._project_deletion_search_prefixes(p_project_id text)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
SET search_path = pg_catalog, public, pg_temp
AS $$
    SELECT jsonb_build_array(
        'project_' || p_project_id || '_path_',
        'project_' || p_project_id || '_folder_'
    );
$$;

CREATE FUNCTION public._project_deletion_sandbox_resources(p_project_id text)
RETURNS jsonb
LANGUAGE sql
STABLE
SET search_path = pg_catalog, public, pg_temp
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

CREATE FUNCTION public._project_deletion_sandbox_resources_valid(
    p_resources jsonb
)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
SET search_path = pg_catalog, public, pg_temp
AS $$
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
$$;

CREATE FUNCTION public._project_deletion_principals_valid(
    p_project_id text,
    p_requested_by uuid,
    p_storage_principals jsonb
)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
SET search_path = pg_catalog, public, pg_temp
AS $$
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
$$;

REVOKE ALL ON FUNCTION public._project_deletion_storage_principals(text, uuid)
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public._project_deletion_object_prefixes(text, jsonb)
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public._project_deletion_principals_valid(text, uuid, jsonb)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public._project_deletion_storage_principals(text, uuid)
    TO service_role;
GRANT EXECUTE ON FUNCTION public._project_deletion_object_prefixes(text, jsonb)
    TO service_role;
GRANT EXECUTE ON FUNCTION public._project_deletion_principals_valid(text, uuid, jsonb)
    TO service_role;
REVOKE ALL ON FUNCTION public._project_deletion_search_prefixes(text)
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public._project_deletion_sandbox_resources(text)
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public._project_deletion_sandbox_resources_valid(jsonb)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public._project_deletion_search_prefixes(text)
    TO service_role;
GRANT EXECUTE ON FUNCTION public._project_deletion_sandbox_resources(text)
    TO service_role;
GRANT EXECUTE ON FUNCTION public._project_deletion_sandbox_resources_valid(jsonb)
    TO service_role;

ALTER TABLE public.project_deletion_jobs
    DROP CONSTRAINT project_deletion_jobs_prefixes_check;
ALTER TABLE public.project_deletion_jobs
    ADD COLUMN storage_principals jsonb,
    ADD COLUMN search_namespace_prefixes jsonb,
    ADD COLUMN sandbox_resources jsonb,
    ADD COLUMN external_ingest_resources jsonb,
    ADD COLUMN external_ingest_snapshot_at timestamptz;

-- The control-plane migration and this closure migration are deployed in one
-- release.  This backfill is nevertheless safe if a job was admitted between
-- them: surviving Project/Upload rows contribute their principals, while the
-- requester is always retained as a conservative exact prefix.
UPDATE public.project_deletion_jobs job
SET storage_principals = public._project_deletion_storage_principals(
        job.project_id, job.requested_by
    );
UPDATE public.project_deletion_jobs job
SET object_prefixes = public._project_deletion_object_prefixes(
        job.project_id, job.storage_principals
    ),
    search_namespace_prefixes = public._project_deletion_search_prefixes(
        job.project_id
    ),
    sandbox_resources = public._project_deletion_sandbox_resources(job.project_id);

ALTER TABLE public.project_deletion_jobs
    ALTER COLUMN storage_principals SET NOT NULL;
ALTER TABLE public.project_deletion_jobs
    ALTER COLUMN search_namespace_prefixes SET NOT NULL,
    ALTER COLUMN sandbox_resources SET NOT NULL;
ALTER TABLE public.project_deletion_jobs
    ADD CONSTRAINT project_deletion_jobs_principals_check CHECK (
        public._project_deletion_principals_valid(
            project_id, requested_by, storage_principals
        )
    );
ALTER TABLE public.project_deletion_jobs
    ADD CONSTRAINT project_deletion_jobs_prefixes_check CHECK (
        object_prefixes = public._project_deletion_object_prefixes(
            project_id, storage_principals
        )
    );
ALTER TABLE public.project_deletion_jobs
    ADD CONSTRAINT project_deletion_jobs_search_prefixes_check CHECK (
        search_namespace_prefixes = public._project_deletion_search_prefixes(project_id)
    ),
    ADD CONSTRAINT project_deletion_jobs_sandbox_resources_check CHECK (
        public._project_deletion_sandbox_resources_valid(sandbox_resources)
    );

CREATE FUNCTION public._project_deletion_external_ingest_snapshot_valid(
    p_project_id text,
    p_external_ingest_resources jsonb
)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
SET search_path = pg_catalog, public, pg_temp
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

REVOKE ALL ON FUNCTION public._project_deletion_external_ingest_snapshot_valid(
    text, jsonb
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public._project_deletion_external_ingest_snapshot_valid(
    text, jsonb
) TO service_role;

ALTER TABLE public.project_deletion_jobs
    ADD CONSTRAINT project_deletion_jobs_external_ingest_snapshot_check CHECK (
        (external_ingest_resources IS NULL
         AND external_ingest_snapshot_at IS NULL)
        OR (
            external_ingest_resources IS NOT NULL
            AND external_ingest_snapshot_at IS NOT NULL
            AND public._project_deletion_external_ingest_snapshot_valid(
                project_id, external_ingest_resources
            )
        )
    );

CREATE FUNCTION public._prepare_project_deletion_job_storage()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
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
    -- This trigger runs before the enclosing destructive transaction removes
    -- uploads/local_shadow_snapshots through FK cascades.  It turns relational
    -- ownership facts into a durable, self-contained cleanup manifest.
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

REVOKE ALL ON FUNCTION public._prepare_project_deletion_job_storage()
    FROM PUBLIC, anon, authenticated;

CREATE TRIGGER project_deletion_jobs_prepare_storage
BEFORE INSERT ON public.project_deletion_jobs
FOR EACH ROW
EXECUTE FUNCTION public._prepare_project_deletion_job_storage();

-- External provider handles are authoritative resources but not relational
-- foreign-key children. Persist their exact ownership manifest after the
-- write lease count reaches zero and before ETL task rows cascade. Local host
-- workspaces/Git views are deliberately excluded: they are fenced, ephemeral,
-- non-authoritative derived caches reconciled independently on each replica.
CREATE FUNCTION public.persist_project_deletion_external_ingest_snapshot(
    p_job_id text,
    p_worker_id text,
    p_external_ingest_resources jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
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

REVOKE ALL ON FUNCTION public.persist_project_deletion_external_ingest_snapshot(
    text, text, jsonb
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.persist_project_deletion_external_ingest_snapshot(
    text, text, jsonb
) TO service_role;

-- Replica-local caches are scrubbed from this durable tombstone feed. The RPC
-- exposes only opaque Project IDs and does not turn hosts/caches into global
-- authoritative resources or completion acknowledgements.
CREATE FUNCTION public.list_project_deletion_host_tombstones()
RETURNS TABLE(project_id text)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
    SELECT DISTINCT job.project_id
    FROM public.project_deletion_jobs job
    ORDER BY job.project_id
$$;

REVOKE ALL ON FUNCTION public.list_project_deletion_host_tombstones()
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.list_project_deletion_host_tombstones()
    TO service_role;

-- Refresh the durable storage manifest at the write-drain linearization
-- point, not merely when deletion is requested. A writer admitted before the
-- Project entered `deleting` may legitimately add a previously unseen legacy
-- upload principal before releasing its lease. Once the active lease count is
-- zero no further Project write can start, so this final snapshot is complete
-- before relational cascades remove the source ledger.
CREATE OR REPLACE FUNCTION public.drain_project_deletion_job(
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

-- Empty-Organization deletion is intentionally a separate, narrow control
-- plane.  It cannot recursively delete Projects because Project deletion has
-- asynchronous object cleanup semantics and an Organization row does not.
CREATE FUNCTION public.delete_empty_organization_control_plane(
    p_org_id text,
    p_actor_user_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
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

-- Eliminate the old service-role escape hatch.  SECURITY DEFINER owns the
-- destructive statement; application code can only request the guarded RPC.
REVOKE DELETE ON TABLE public.organizations
    FROM PUBLIC, anon, authenticated, service_role;
-- Project rows have the same single destructive entry point.  Read/update
-- privileges remain for the Version Engine and ordinary Project metadata, but
-- application roles cannot remove a row without first publishing its cleanup
-- tombstone in delete_project_control_plane.
REVOKE DELETE ON TABLE public.projects
    FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON FUNCTION public.delete_empty_organization_control_plane(text, uuid)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.delete_empty_organization_control_plane(text, uuid)
    TO service_role;

COMMIT;
