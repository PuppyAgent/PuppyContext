-- Deployment-owned policy source. Hosted databases keep fail-closed billing.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='2min';

CREATE TABLE public.repository_entitlement_source (
    singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
    mode text NOT NULL CHECK(mode IN ('db','disabled')),
    revision bigint NOT NULL CHECK(revision>0)
);
INSERT INTO public.repository_entitlement_source VALUES(true,'db',1);
ALTER TABLE public.repository_entitlement_source ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.repository_entitlement_source FROM PUBLIC,anon,authenticated,service_role;

CREATE FUNCTION public.configure_repository_entitlement_source(p_mode text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE current_mode text;
BEGIN
    IF p_mode IS NULL OR p_mode NOT IN ('db','disabled') THEN
        RAISE EXCEPTION 'unsupported_repository_entitlement_source';
    END IF;
    SELECT mode INTO current_mode FROM public.repository_entitlement_source WHERE singleton FOR UPDATE;
    IF current_mode=p_mode THEN RETURN; END IF;
    -- Only the installation owner can choose this policy, before native use.
    -- Runtime credentials cannot convert a hosted installation into unlimited mode.
    IF EXISTS(SELECT FROM public.organization_entitlements WHERE source='puppypay')
       OR EXISTS(SELECT FROM public.version_repositories WHERE authority='native') THEN
        RAISE EXCEPTION 'repository_entitlement_source_in_use';
    END IF;
    UPDATE public.repository_entitlement_source SET mode=p_mode,revision=revision+1 WHERE singleton;
END $$;
REVOKE ALL ON FUNCTION public.configure_repository_entitlement_source(text)
    FROM PUBLIC,anon,authenticated,service_role;

ALTER FUNCTION public._version_billing_entitlement(text,bigint)
    RENAME TO _version_puppypay_billing_entitlement;
CREATE FUNCTION public._version_billing_entitlement(p_org_id text,p_revision bigint DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE policy public.repository_entitlement_source%ROWTYPE;
BEGIN
    SELECT * INTO STRICT policy FROM public.repository_entitlement_source WHERE singleton FOR SHARE;
    IF policy.mode='db' THEN
        RETURN public._version_puppypay_billing_entitlement(p_org_id,p_revision);
    END IF;
    IF p_revision IS NOT NULL AND p_revision IS DISTINCT FROM policy.revision THEN
        RAISE EXCEPTION 'storage_billing_entitlement_changed' USING ERRCODE='55000';
    END IF;
    RETURN jsonb_build_object('org_id',p_org_id,'source_revision',policy.revision,
        'storage_limit',NULL,'policy_source','standalone_disabled');
END $$;

ALTER FUNCTION public._version_file_policy_limit(text,bigint)
    RENAME TO _version_puppypay_file_policy_limit;
CREATE FUNCTION public._version_file_policy_limit(p_org_id text,p_revision bigint DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE policy public.repository_entitlement_source%ROWTYPE;
BEGIN
    SELECT * INTO STRICT policy FROM public.repository_entitlement_source WHERE singleton FOR SHARE;
    IF policy.mode='db' THEN
        RETURN public._version_puppypay_file_policy_limit(p_org_id,p_revision);
    END IF;
    RETURN public._version_billing_entitlement(p_org_id,p_revision)
        || jsonb_build_object('file_limit',NULL);
END $$;
REVOKE ALL ON FUNCTION public._version_billing_entitlement(text,bigint),
    public._version_file_policy_limit(text,bigint),
    public._version_puppypay_billing_entitlement(text,bigint),
    public._version_puppypay_file_policy_limit(text,bigint)
    FROM PUBLIC,anon,authenticated,service_role;
COMMIT;
