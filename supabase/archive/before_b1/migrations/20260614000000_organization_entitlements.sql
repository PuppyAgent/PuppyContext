-- Central entitlement snapshots owned by PuppyOne.
--
-- PuppyPay is the billing source-of-truth, but product enforcement needs a
-- local, low-latency snapshot in the PuppyOne database. The product reads this
-- table on hot paths; PuppyPay writes it through /internal/billing/entitlements.

CREATE TABLE IF NOT EXISTS public.organization_entitlements (
    org_id              text PRIMARY KEY REFERENCES public.organizations(id) ON DELETE CASCADE,
    plan_id             text NOT NULL DEFAULT 'free',
    status              text NOT NULL DEFAULT 'free',
    source              text NOT NULL DEFAULT 'local',
    entitlements        jsonb NOT NULL DEFAULT '{}'::jsonb,
    current_period_end  timestamptz,
    effective_until     timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT organization_entitlements_status_check
        CHECK (status IN ('free', 'trialing', 'active', 'past_due', 'canceled', 'expired', 'grace')),
    CONSTRAINT organization_entitlements_source_check
        CHECK (source IN ('local', 'puppypay', 'admin', 'system')),
    CONSTRAINT organization_entitlements_json_object_check
        CHECK (jsonb_typeof(entitlements) = 'object')
);

CREATE INDEX IF NOT EXISTS organization_entitlements_plan_idx
    ON public.organization_entitlements (plan_id);

CREATE INDEX IF NOT EXISTS organization_entitlements_status_idx
    ON public.organization_entitlements (status);

CREATE OR REPLACE FUNCTION public._organization_entitlements_bump_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_organization_entitlements_updated_at
    ON public.organization_entitlements;

CREATE TRIGGER trg_organization_entitlements_updated_at
    BEFORE UPDATE ON public.organization_entitlements
    FOR EACH ROW
    EXECUTE FUNCTION public._organization_entitlements_bump_updated_at();

CREATE TABLE IF NOT EXISTS public.organization_entitlement_events (
    id                  text PRIMARY KEY DEFAULT (extensions.uuid_generate_v4())::text,
    org_id              text NOT NULL REFERENCES public.organizations(id) ON DELETE CASCADE,
    source              text NOT NULL DEFAULT 'puppypay',
    source_event_id     text,
    event_type          text,
    old_plan_id         text,
    new_plan_id         text,
    old_entitlements    jsonb,
    new_entitlements    jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT organization_entitlement_events_json_object_check
        CHECK (jsonb_typeof(new_entitlements) = 'object')
);

CREATE INDEX IF NOT EXISTS organization_entitlement_events_org_created_idx
    ON public.organization_entitlement_events (org_id, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS organization_entitlement_events_source_event_idx
    ON public.organization_entitlement_events (source_event_id)
    WHERE source_event_id IS NOT NULL;

ALTER TABLE public.organization_entitlements ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.organization_entitlement_events ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS organization_entitlements_service_role_all
    ON public.organization_entitlements;
CREATE POLICY organization_entitlements_service_role_all
    ON public.organization_entitlements
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

DROP POLICY IF EXISTS organization_entitlement_events_service_role_all
    ON public.organization_entitlement_events;
CREATE POLICY organization_entitlement_events_service_role_all
    ON public.organization_entitlement_events
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

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

GRANT SELECT ON public.organization_entitlements TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.organization_entitlements TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.organization_entitlement_events TO service_role;
