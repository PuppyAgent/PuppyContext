-- ============================================================================
-- Durable commercial-operation correlation and public lifecycle support
-- ============================================================================

BEGIN;

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '15min';

ALTER TABLE public.organization_entitlements
    ADD COLUMN IF NOT EXISTS source_quote_id text;

ALTER TABLE public.organization_entitlement_events
    ADD COLUMN IF NOT EXISTS source_quote_id text;

ALTER TABLE public.organization_entitlements
    DROP CONSTRAINT IF EXISTS organization_entitlements_source_quote_shape;
ALTER TABLE public.organization_entitlements
    ADD CONSTRAINT organization_entitlements_source_quote_shape
      CHECK (
        source_quote_id IS NULL
        OR (NULLIF(btrim(source_quote_id), '') IS NOT NULL AND length(source_quote_id) <= 255)
      );

ALTER TABLE public.organization_entitlement_events
    DROP CONSTRAINT IF EXISTS organization_entitlement_events_source_quote_shape;
ALTER TABLE public.organization_entitlement_events
    ADD CONSTRAINT organization_entitlement_events_source_quote_shape
      CHECK (
        source_quote_id IS NULL
        OR (NULLIF(btrim(source_quote_id), '') IS NOT NULL AND length(source_quote_id) <= 255)
      );

ALTER TABLE public.organization_billing_operations
    ADD COLUMN IF NOT EXISTS baseline_source_revision bigint;

ALTER TABLE public.organization_billing_operations
    DROP CONSTRAINT IF EXISTS organization_billing_operations_kind_check,
    DROP CONSTRAINT IF EXISTS organization_billing_operations_baseline_revision_check,
    DROP CONSTRAINT IF EXISTS organization_billing_operations_quote_shape;

ALTER TABLE public.organization_billing_operations
    ADD CONSTRAINT organization_billing_operations_kind_check
      CHECK (kind IN (
        'checkout', 'seat_increase', 'seat_decrease', 'plan_change',
        'member_activation', 'member_deactivation',
        'entitlement_provision'
      )),
    ADD CONSTRAINT organization_billing_operations_baseline_revision_check
      CHECK (baseline_source_revision IS NULL OR baseline_source_revision >= 0),
    ADD CONSTRAINT organization_billing_operations_quote_shape
      CHECK (
        quote_id IS NULL
        OR (NULLIF(btrim(quote_id), '') IS NOT NULL AND length(quote_id) <= 255)
      );

CREATE UNIQUE INDEX IF NOT EXISTS organization_billing_operations_quote_idx
    ON public.organization_billing_operations (org_id, quote_id)
    WHERE quote_id IS NOT NULL;

-- The v1 publication function intentionally knows nothing about Quote
-- correlation and therefore leaves additive columns untouched on conflict.
-- Clear a previous revision's correlation before every revision advance so an
-- old application instance can never carry a stale Quote into a newer
-- entitlement. The v2 wrapper writes the new correlation later in the same
-- transaction.
CREATE OR REPLACE FUNCTION public._reset_entitlement_quote_on_revision_advance()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
    IF NEW.source_revision IS DISTINCT FROM OLD.source_revision THEN
        NEW.source_quote_id := NULL;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_reset_entitlement_quote_on_revision_advance
    ON public.organization_entitlements;
CREATE TRIGGER trg_reset_entitlement_quote_on_revision_advance
    BEFORE UPDATE OF source_revision ON public.organization_entitlements
    FOR EACH ROW EXECUTE FUNCTION public._reset_entitlement_quote_on_revision_advance();

CREATE OR REPLACE FUNCTION public._confirm_correlated_billing_operations()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
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

DROP TRIGGER IF EXISTS trg_confirm_correlated_billing_operations
    ON public.organization_entitlements;
CREATE TRIGGER trg_confirm_correlated_billing_operations
    AFTER INSERT OR UPDATE OF source_revision, source_quote_id, plan_id, seat_quantity
    ON public.organization_entitlements
    FOR EACH ROW EXECUTE FUNCTION public._confirm_correlated_billing_operations();

-- Keep the v1 publication function available during rolling deploys. The v2
-- wrapper adds Quote correlation without changing the established monotonic
-- revision/hash transaction, and refreshes the returned snapshot after the
-- correlation write has fired the operation-confirmation trigger.
CREATE OR REPLACE FUNCTION public.publish_organization_entitlement_v2(
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
    p_source_quote_id text DEFAULT NULL,
    p_event_type text DEFAULT 'entitlement.published'
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
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
$$;

CREATE OR REPLACE FUNCTION public.reconcile_billing_operation_from_entitlement(
    p_org_id text,
    p_operation_id text
)
RETURNS SETOF public.organization_billing_operations
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
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

REVOKE ALL ON FUNCTION public._confirm_correlated_billing_operations()
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public._reset_entitlement_quote_on_revision_advance()
    FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.publish_organization_entitlement_v2(
    text, text, text, text, text, jsonb, integer, text, bigint,
    timestamptz, timestamptz, timestamptz, text, text, text, text
) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.reconcile_billing_operation_from_entitlement(text, text)
    FROM PUBLIC, anon, authenticated;

GRANT EXECUTE ON FUNCTION public.publish_organization_entitlement_v2(
    text, text, text, text, text, jsonb, integer, text, bigint,
    timestamptz, timestamptz, timestamptz, text, text, text, text
) TO service_role;
GRANT EXECUTE ON FUNCTION public.reconcile_billing_operation_from_entitlement(text, text)
    TO service_role;

COMMIT;
