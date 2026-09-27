-- Repair an installation whose migration history records the original
-- Project-storage deletion migration, but whose inventory control-plane
-- relations were never materialized.  This is deliberately a forward,
-- idempotent schema repair: history is evidence, not a substitute for the
-- objects required by later migrations and the deletion admission fence.

CREATE TABLE IF NOT EXISTS public.project_storage_principals (
    project_id text NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,
    principal text NOT NULL,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, principal),
    CONSTRAINT project_storage_principals_segment_check CHECK (
        project_id ~ '^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$'
        AND principal ~ '^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$'
    )
);

CREATE TABLE IF NOT EXISTS public.project_storage_inventory_state (
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

CREATE TABLE IF NOT EXISTS public.project_storage_inventory_batches (
    batch_key text PRIMARY KEY,
    checkpoint jsonb NOT NULL,
    observed_object_count integer NOT NULL CHECK (observed_object_count >= 0),
    observed_multipart_count integer NOT NULL CHECK (observed_multipart_count >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT project_storage_inventory_batch_key_check CHECK (
        batch_key ~ '^[0-9a-f]{64}$'
    )
);

CREATE TABLE IF NOT EXISTS public.project_storage_orphan_prefixes (
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

INSERT INTO public.project_storage_inventory_state (singleton)
VALUES (true) ON CONFLICT (singleton) DO NOTHING;

ALTER TABLE public.project_storage_principals ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.project_storage_inventory_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.project_storage_inventory_batches ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.project_storage_orphan_prefixes ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS project_storage_principals_service_role_read
    ON public.project_storage_principals;
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

CREATE OR REPLACE FUNCTION public.record_project_storage_inventory_batch(
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

CREATE OR REPLACE FUNCTION public.mark_project_storage_orphan_cleaned(
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

CREATE OR REPLACE FUNCTION public.finalize_project_storage_inventory_scan(
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

CREATE OR REPLACE FUNCTION public.verify_project_storage_inventory(
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

CREATE OR REPLACE FUNCTION public.complete_project_storage_inventory()
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
