-- Local/CI assertion only. Never run against a hosted database shared with Pay.
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_namespace WHERE nspname = 'puppypay') THEN
        RAISE EXCEPTION 'Standalone test must not have a PuppyPay schema';
    END IF;
    IF to_regclass('public.ai_credit_accounts') IS NOT NULL
       OR to_regclass('public.billing_accounts') IS NOT NULL THEN
        RAISE EXCEPTION 'Standalone test must not contain private financial tables';
    END IF;
    IF to_regclass('public.projects') IS NULL
       OR to_regclass('public.project_members') IS NULL
       OR to_regclass('public.organization_entitlements') IS NULL THEN
        RAISE EXCEPTION 'PuppyOne must own and install its complete product schema';
    END IF;
END;
$$;
