-- Keep packed-object location metadata behind the service-role boundary.
-- The table lives in the API-exposed public schema and therefore must never
-- rely on grants alone as its row-access boundary.

BEGIN;

ALTER TABLE public.version_object_locations ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON public.version_object_locations
    FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON public.version_object_locations TO service_role;

COMMIT;
