-- ISSUE-062 / Expand hardening. No data rewrite, activation or ACL changes.
-- Preserve the preceding migration's bytes and meet ISSUE-053's existing
-- SECURITY DEFINER contract, including explicit last-position pg_temp.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '2min';

ALTER FUNCTION public.apply_version_ref_transaction(text, text, uuid, bigint, jsonb, uuid, text)
    SET search_path = pg_catalog, public, pg_temp;
ALTER FUNCTION public.get_version_ref_transaction(text, text, uuid)
    SET search_path = pg_catalog, public, pg_temp;
ALTER FUNCTION public._version_fence_legacy_publication()
    SET search_path = pg_catalog, public, pg_temp;

NOTIFY pgrst, 'reload schema';
COMMIT;
