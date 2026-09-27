-- ============================================================================
-- Unified PuppyPay -> PuppyOne billing control-plane projection
-- ============================================================================
-- This migration is intentionally additive.  PuppyPay remains the commercial
-- and financial authority; these tables are the product-side projection,
-- durable saga state, and usage facts needed for low-latency enforcement.

BEGIN;

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '15min';

-- --------------------------------------------------------------------------
-- Versioned entitlement projection
-- --------------------------------------------------------------------------

ALTER TABLE public.organization_entitlements
    ADD COLUMN IF NOT EXISTS schema_version text NOT NULL DEFAULT '1.0',
    ADD COLUMN IF NOT EXISTS catalog_version text NOT NULL DEFAULT 'legacy',
    ADD COLUMN IF NOT EXISTS source_revision bigint NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS seat_quantity integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS effective_at timestamptz,
    ADD COLUMN IF NOT EXISTS payload_hash text NOT NULL DEFAULT '';

ALTER TABLE public.organization_entitlements
    DROP CONSTRAINT IF EXISTS organization_entitlements_revision_nonnegative,
    DROP CONSTRAINT IF EXISTS organization_entitlements_seats_nonnegative,
    DROP CONSTRAINT IF EXISTS organization_entitlements_payload_hash_shape;

ALTER TABLE public.organization_entitlements
    ADD CONSTRAINT organization_entitlements_revision_nonnegative
      CHECK (source_revision >= 0),
    ADD CONSTRAINT organization_entitlements_seats_nonnegative
      CHECK (seat_quantity >= 0),
    ADD CONSTRAINT organization_entitlements_payload_hash_shape
      CHECK (payload_hash = '' OR payload_hash ~ '^[0-9a-f]{64}$');

-- Replace the legacy Stripe-era lifecycle vocabulary with the canonical
-- PuppyPay commercial state machine. Existing legacy values stay readable
-- during the expand/contract window.
ALTER TABLE public.organization_entitlements
    DROP CONSTRAINT IF EXISTS organization_entitlements_status_check;
ALTER TABLE public.organization_entitlements
    ADD CONSTRAINT organization_entitlements_status_check CHECK (status IN (
        'free', 'trialing', 'checkout_pending', 'active', 'change_pending',
        'cancel_scheduled', 'past_due', 'canceled', 'expired', 'grace',
        'revoked', 'disputed'
    ));

CREATE INDEX IF NOT EXISTS organization_entitlements_effective_idx
    ON public.organization_entitlements (effective_until, source_revision);

ALTER TABLE public.organization_entitlement_events
    ADD COLUMN IF NOT EXISTS schema_version text,
    ADD COLUMN IF NOT EXISTS catalog_version text,
    ADD COLUMN IF NOT EXISTS source_revision bigint,
    ADD COLUMN IF NOT EXISTS seat_quantity integer,
    ADD COLUMN IF NOT EXISTS payload_hash text,
    ADD COLUMN IF NOT EXISTS publication_outcome text;

CREATE INDEX IF NOT EXISTS organization_entitlement_events_revision_idx
    ON public.organization_entitlement_events (org_id, source_revision DESC);

-- Pending/same/newer decisions and the audit row are one database transaction.
-- The function returns a JSON envelope so PostgREST clients can distinguish an
-- applied update from an exact idempotent replay without parsing exceptions.
CREATE OR REPLACE FUNCTION public.publish_organization_entitlement(
    p_org_id text,
    p_schema_version text,
    p_plan_id text,
    p_status text,
    p_source text,
    p_entitlements jsonb,
    p_seat_quantity integer,
    p_catalog_version text,
    p_source_revision bigint,
    p_effective_at timestamptz,
    p_effective_until timestamptz,
    p_current_period_end timestamptz,
    p_payload_hash text,
    p_source_event_id text DEFAULT NULL,
    p_event_type text DEFAULT 'entitlement.published'
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
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
$$;

REVOKE ALL ON FUNCTION public.publish_organization_entitlement(
    text, text, text, text, text, jsonb, integer, text, bigint,
    timestamptz, timestamptz, timestamptz, text, text, text
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.publish_organization_entitlement(
    text, text, text, text, text, jsonb, integer, text, bigint,
    timestamptz, timestamptz, timestamptz, text, text, text
) TO service_role;

-- Billing-operation rows never grant membership. Only an activated
-- org_members row may read this projection.
DROP POLICY IF EXISTS organization_entitlements_member_select
    ON public.organization_entitlements;
CREATE POLICY organization_entitlements_member_select
    ON public.organization_entitlements
    FOR SELECT
    TO authenticated
    USING (
        EXISTS (
            SELECT 1
            FROM public.org_members m
            WHERE m.org_id = organization_entitlements.org_id
              AND m.user_id = auth.uid()
        )
    );

-- --------------------------------------------------------------------------
-- Durable product-side billing saga/outbox
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.organization_billing_operations (
    id                  text PRIMARY KEY DEFAULT (extensions.uuid_generate_v4())::text,
    org_id              text NOT NULL REFERENCES public.organizations(id) ON DELETE CASCADE,
    kind                text NOT NULL,
    status              text NOT NULL DEFAULT 'pending',
    idempotency_key     text NOT NULL,
    actor_user_id       uuid REFERENCES public.profiles(user_id) ON DELETE SET NULL,
    subject_user_id     uuid REFERENCES public.profiles(user_id) ON DELETE SET NULL,
    invitation_id       text REFERENCES public.org_invitations(id) ON DELETE SET NULL,
    target_plan_id      text,
    current_seat_quantity integer,
    target_seat_quantity integer,
    quote_id            text,
    confirmed_revision  bigint,
    request_payload     jsonb NOT NULL DEFAULT '{}'::jsonb,
    response_payload    jsonb NOT NULL DEFAULT '{}'::jsonb,
    attempts            integer NOT NULL DEFAULT 0,
    next_attempt_at     timestamptz NOT NULL DEFAULT now(),
    last_error          text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    completed_at        timestamptz,
    CONSTRAINT organization_billing_operations_kind_check
      CHECK (kind IN (
        'seat_increase', 'seat_decrease', 'plan_change',
        'member_activation', 'member_deactivation',
        'entitlement_provision'
      )),
    CONSTRAINT organization_billing_operations_status_check
      CHECK (status IN (
        'pending', 'quoted', 'awaiting_confirmation', 'submitted',
        'confirmed', 'failed', 'canceled'
      )),
    CONSTRAINT organization_billing_operations_seats_check
      CHECK (
        (current_seat_quantity IS NULL OR current_seat_quantity >= 0)
        AND (target_seat_quantity IS NULL OR target_seat_quantity >= 0)
      ),
    CONSTRAINT organization_billing_operations_request_object
      CHECK (jsonb_typeof(request_payload) = 'object'),
    CONSTRAINT organization_billing_operations_response_object
      CHECK (jsonb_typeof(response_payload) = 'object'),
    UNIQUE (org_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS organization_billing_operations_pending_idx
    ON public.organization_billing_operations (status, next_attempt_at)
    WHERE status IN ('pending', 'submitted', 'failed');
CREATE INDEX IF NOT EXISTS organization_billing_operations_org_created_idx
    ON public.organization_billing_operations (org_id, created_at DESC);

CREATE OR REPLACE FUNCTION public._billing_row_bump_updated_at()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_organization_billing_operations_updated_at
    ON public.organization_billing_operations;
CREATE TRIGGER trg_organization_billing_operations_updated_at
    BEFORE UPDATE ON public.organization_billing_operations
    FOR EACH ROW EXECUTE FUNCTION public._billing_row_bump_updated_at();

-- Hosted PuppyOne discovers both legacy and newly-created organizations that
-- do not yet have an authoritative PuppyPay snapshot. The application invokes
-- these service-role-only functions only when ENTITLEMENTS_MODE=db; community
-- deployments therefore keep the additive schema without contacting PuppyPay.
CREATE OR REPLACE FUNCTION public.enqueue_missing_entitlement_provisioning(
    p_limit integer DEFAULT 100
)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
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

CREATE OR REPLACE FUNCTION public.claim_entitlement_provisioning_batch(
    p_limit integer DEFAULT 25,
    p_lease_seconds integer DEFAULT 60
)
RETURNS SETOF public.organization_billing_operations
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
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

REVOKE ALL ON FUNCTION public.enqueue_missing_entitlement_provisioning(integer)
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.claim_entitlement_provisioning_batch(integer, integer)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.enqueue_missing_entitlement_provisioning(integer)
    TO service_role;
GRANT EXECUTE ON FUNCTION public.claim_entitlement_provisioning_batch(integer, integer)
    TO service_role;

-- Seat operations are a product-side outbox. Claiming only advances a lease;
-- it does not grant membership or mutate the paid subscription. A worker
-- submits an idempotent proposal to PuppyPay and stores the resulting Quote
-- for an organization owner to confirm explicitly.
CREATE OR REPLACE FUNCTION public.claim_seat_proposal_batch(
    p_limit integer DEFAULT 25,
    p_lease_seconds integer DEFAULT 60
)
RETURNS SETOF public.organization_billing_operations
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
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

REVOKE ALL ON FUNCTION public.claim_seat_proposal_batch(integer, integer)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.claim_seat_proposal_batch(integer, integer)
    TO service_role;

-- --------------------------------------------------------------------------
-- Durable runtime reservation/settlement linkage
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.runtime_billing_runs (
    run_id              text PRIMARY KEY,
    org_id              text NOT NULL REFERENCES public.organizations(id) ON DELETE CASCADE,
    project_id          text REFERENCES public.projects(id) ON DELETE SET NULL,
    runtime_kind        text NOT NULL,
    compute_profile     text NOT NULL DEFAULT 'standard',
    status              text NOT NULL DEFAULT 'pending_reservation',
    idempotency_key     text NOT NULL,
    reservation_id      text,
    estimated_units     bigint NOT NULL DEFAULT 0,
    actual_units        bigint,
    started_at          timestamptz,
    heartbeat_at        timestamptz,
    settled_at          timestamptz,
    expires_at          timestamptz,
    attempts            integer NOT NULL DEFAULT 0,
    last_error          text,
    metadata            jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT runtime_billing_runs_status_check
      CHECK (status IN (
        'pending_reservation', 'reserved', 'running', 'settling',
        'settled', 'canceled', 'denied', 'expired', 'failed',
        'reservation_failed', 'unmetered'
      )),
    CONSTRAINT runtime_billing_runs_kind_check
      CHECK (runtime_kind IN ('automation', 'sandbox', 'workspace', 'connector')),
    CONSTRAINT runtime_billing_runs_units_check
      CHECK (
        estimated_units >= 0
        AND (actual_units IS NULL OR actual_units >= 0)
      ),
    CONSTRAINT runtime_billing_runs_metadata_object
      CHECK (jsonb_typeof(metadata) = 'object'),
    UNIQUE (org_id, idempotency_key),
    UNIQUE (reservation_id)
);

CREATE INDEX IF NOT EXISTS runtime_billing_runs_recovery_idx
    ON public.runtime_billing_runs (status, heartbeat_at, expires_at)
    WHERE status IN ('reserved', 'running', 'settling', 'failed');
CREATE INDEX IF NOT EXISTS runtime_billing_runs_retry_idx
    ON public.runtime_billing_runs (status, updated_at)
    WHERE status IN ('settling', 'failed');
CREATE INDEX IF NOT EXISTS runtime_billing_runs_org_created_idx
    ON public.runtime_billing_runs (org_id, created_at DESC);

DROP TRIGGER IF EXISTS trg_runtime_billing_runs_updated_at
    ON public.runtime_billing_runs;
CREATE TRIGGER trg_runtime_billing_runs_updated_at
    BEFORE UPDATE ON public.runtime_billing_runs
    FOR EACH ROW EXECUTE FUNCTION public._billing_row_bump_updated_at();

-- --------------------------------------------------------------------------
-- Idempotent logical usage counters
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.organization_usage_counters (
    org_id              text NOT NULL REFERENCES public.organizations(id) ON DELETE CASCADE,
    metric              text NOT NULL,
    value               bigint NOT NULL DEFAULT 0,
    version             bigint NOT NULL DEFAULT 0,
    threshold_percent   integer NOT NULL DEFAULT 0,
    reconciled_at       timestamptz,
    full_reconciled_at  timestamptz,
    reconciliation_claimed_at timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, metric),
    CONSTRAINT organization_usage_counters_metric_check
      CHECK (metric IN ('storage.logical_bytes')),
    CONSTRAINT organization_usage_counters_value_check CHECK (value >= 0),
    CONSTRAINT organization_usage_counters_version_check CHECK (version >= 0),
    CONSTRAINT organization_usage_counters_threshold_check
      CHECK (threshold_percent IN (0, 80, 95, 100))
);

CREATE TABLE IF NOT EXISTS public.organization_usage_events (
    id                  text PRIMARY KEY DEFAULT (extensions.uuid_generate_v4())::text,
    org_id              text NOT NULL REFERENCES public.organizations(id) ON DELETE CASCADE,
    metric              text NOT NULL,
    idempotency_key     text NOT NULL,
    delta               bigint NOT NULL,
    value_after         bigint NOT NULL,
    source              text NOT NULL,
    metadata            jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT organization_usage_events_value_check CHECK (value_after >= 0),
    CONSTRAINT organization_usage_events_metric_check
      CHECK (metric IN ('storage.logical_bytes')),
    CONSTRAINT organization_usage_events_metadata_object
      CHECK (jsonb_typeof(metadata) = 'object'),
    UNIQUE (org_id, metric, idempotency_key)
);

CREATE INDEX IF NOT EXISTS organization_usage_events_org_created_idx
    ON public.organization_usage_events (org_id, metric, created_at DESC);

CREATE INDEX IF NOT EXISTS organization_usage_counters_full_reconcile_idx
    ON public.organization_usage_counters (
        full_reconciled_at, reconciliation_claimed_at, org_id
    )
    WHERE metric = 'storage.logical_bytes';

DROP TRIGGER IF EXISTS trg_organization_usage_counters_updated_at
    ON public.organization_usage_counters;
CREATE TRIGGER trg_organization_usage_counters_updated_at
    BEFORE UPDATE ON public.organization_usage_counters
    FOR EACH ROW EXECUTE FUNCTION public._billing_row_bump_updated_at();

CREATE OR REPLACE FUNCTION public.claim_storage_reconciliation_batch(
    p_limit integer DEFAULT 25,
    p_min_age_seconds integer DEFAULT 86400,
    p_claim_lease_seconds integer DEFAULT 900
)
RETURNS TABLE (org_id text)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
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

REVOKE ALL ON FUNCTION public.claim_storage_reconciliation_batch(integer, integer, integer)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.claim_storage_reconciliation_batch(integer, integer, integer)
    TO service_role;

CREATE OR REPLACE FUNCTION public.adjust_organization_usage_counter(
    p_org_id text,
    p_metric text,
    p_delta bigint,
    p_idempotency_key text,
    p_source text,
    p_metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
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

REVOKE ALL ON FUNCTION public.adjust_organization_usage_counter(
    text, text, bigint, text, text, jsonb
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.adjust_organization_usage_counter(
    text, text, bigint, text, text, jsonb
) TO service_role;

-- Reconciliation/full-tree measurements set an absolute logical value. This
-- is also used after the authoritative Version Engine CAS to initialize a
-- missing counter and to append the commit's auditable usage event. Periodic
-- full reconciliation remains responsible for correcting pre-existing drift.
CREATE OR REPLACE FUNCTION public.reconcile_organization_usage_counter(
    p_org_id text,
    p_metric text,
    p_value bigint,
    p_limit bigint,
    p_idempotency_key text,
    p_source text,
    p_metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
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

REVOKE ALL ON FUNCTION public.reconcile_organization_usage_counter(
    text, text, bigint, bigint, text, text, jsonb
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.reconcile_organization_usage_counter(
    text, text, bigint, bigint, text, text, jsonb
) TO service_role;

-- Version publication and organization storage accounting share one SQL
-- transaction. The advisory lock serializes growth across different projects
-- in the same organization; project root CAS still protects each repository.
CREATE OR REPLACE FUNCTION public.publish_version_project_update_with_usage(
    p_project_id text,
    p_old_root_hash text,
    p_new_root_hash text,
    p_head_commit_id text,
    p_who text,
    p_message text,
    p_event_type text,
    p_changes jsonb,
    p_conflicts jsonb,
    p_created_at text,
    p_audit_agent_id text,
    p_audit_detail jsonb,
    p_source_channel text DEFAULT '',
    p_policy text DEFAULT '',
    p_base_commit_id text DEFAULT '',
    p_client_commit_id text DEFAULT '',
    p_proposed_tree_id text DEFAULT '',
    p_intent_type text DEFAULT 'operation',
    p_scope_path text DEFAULT '',
    p_scope_hash text DEFAULT '',
    p_scope_head_commit_id text DEFAULT '',
    p_expected_scope_head_commit_id text DEFAULT NULL,
    p_org_id text DEFAULT NULL,
    p_storage_old_value bigint DEFAULT 0,
    p_storage_delta bigint DEFAULT 0,
    p_storage_limit bigint DEFAULT NULL,
    p_storage_enforce boolean DEFAULT false,
    p_entitlement_source_revision bigint DEFAULT NULL
)
RETURNS TABLE (published boolean, txn_id bigint)
LANGUAGE plpgsql
AS $$
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
$$;

REVOKE ALL ON FUNCTION public.publish_version_project_update_with_usage(
    text, text, text, text, text, text, text, jsonb, jsonb, text, text, jsonb,
    text, text, text, text, text, text, text, text, text, text,
    text, bigint, bigint, bigint, boolean, bigint
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.publish_version_project_update_with_usage(
    text, text, text, text, text, text, text, jsonb, jsonb, text, text, jsonb,
    text, text, text, text, text, text, text, text, text, text,
    text, bigint, bigint, bigint, boolean, bigint
) TO service_role;

-- A seat is derived from effective product capability, not a manually copied
-- billing boolean. Canonical organization owner/member roles carry hosted
-- product capability; a viewer becomes billable only when an explicit Project
-- admin/editor grant gives Cloud write or hosted runtime capability.
CREATE OR REPLACE FUNCTION public.is_billable_organization_member(
    p_org_id text,
    p_user_id uuid
) RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = ''
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

CREATE OR REPLACE FUNCTION public.count_billable_organization_members(
    p_org_id text
) RETURNS bigint
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = ''
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

-- Serialize seat admission before a membership/capability mutation. The
-- confirmed, incomplete operation is a durable short-lived seat reservation:
-- a crash can conservatively consume capacity, but can never grant an unpaid
-- seat. Once the membership mutation succeeds, application code stamps
-- completed_at; the count then comes from canonical capability facts instead.
CREATE OR REPLACE FUNCTION public.reserve_billable_member_activation(
    p_org_id text,
    p_subject_user_id uuid,
    p_actor_user_id uuid,
    p_invitation_id text,
    p_role text,
    p_idempotency_key text
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
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

REVOKE ALL ON FUNCTION public.is_billable_organization_member(text, uuid),
    public.count_billable_organization_members(text),
    public.reserve_billable_member_activation(
        text, uuid, uuid, text, text, text
    )
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.is_billable_organization_member(text, uuid),
    public.count_billable_organization_members(text),
    public.reserve_billable_member_activation(
        text, uuid, uuid, text, text, text
    )
    TO service_role;

-- Ownership transfer changes two membership rows atomically. Application code
-- cannot expose a zero-owner or two-owner intermediate state to concurrent
-- authorization checks.
CREATE OR REPLACE FUNCTION public.transfer_organization_ownership(
    p_org_id text,
    p_current_owner uuid,
    p_new_owner uuid
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
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

REVOKE ALL ON FUNCTION public.transfer_organization_ownership(text, uuid, uuid)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.transfer_organization_ownership(text, uuid, uuid)
    TO service_role;

-- --------------------------------------------------------------------------
-- RLS: billing operation and raw usage facts stay behind the application BFF
-- --------------------------------------------------------------------------

ALTER TABLE public.organization_billing_operations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.runtime_billing_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.organization_usage_counters ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.organization_usage_events ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS organization_billing_operations_service_all
    ON public.organization_billing_operations;
CREATE POLICY organization_billing_operations_service_all
    ON public.organization_billing_operations
    FOR ALL TO service_role USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS runtime_billing_runs_service_all
    ON public.runtime_billing_runs;
CREATE POLICY runtime_billing_runs_service_all
    ON public.runtime_billing_runs
    FOR ALL TO service_role USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS organization_usage_counters_service_all
    ON public.organization_usage_counters;
CREATE POLICY organization_usage_counters_service_all
    ON public.organization_usage_counters
    FOR ALL TO service_role USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS organization_usage_events_service_all
    ON public.organization_usage_events;
CREATE POLICY organization_usage_events_service_all
    ON public.organization_usage_events
    FOR ALL TO service_role USING (true) WITH CHECK (true);

GRANT SELECT, INSERT, UPDATE, DELETE
    ON public.organization_billing_operations,
       public.runtime_billing_runs,
       public.organization_usage_counters,
       public.organization_usage_events
    TO service_role;

NOTIFY pgrst, 'reload schema';

COMMIT;
