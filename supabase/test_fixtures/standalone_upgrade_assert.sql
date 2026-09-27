DO $$
BEGIN
    IF NOT EXISTS (
        SELECT FROM public.profiles
        WHERE user_id = '00000000-0000-4000-8000-000000092601'
          AND display_name = 'Standalone upgrade sentinel'
    ) OR NOT EXISTS (
        SELECT FROM public.organizations
        WHERE id = 'standalone-upgrade-org' AND name = 'Standalone upgrade sentinel'
    ) THEN
        RAISE EXCEPTION 'Standalone upgrade lost or changed the fixture user/organization';
    END IF;
END;
$$;
