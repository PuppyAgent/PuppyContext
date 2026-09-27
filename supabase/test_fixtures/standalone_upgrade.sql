-- Synthetic user data at the supported upgrade baseline; no Pay account exists.
INSERT INTO auth.users (
    instance_id, id, aud, role, email, encrypted_password,
    email_confirmed_at, raw_app_meta_data, raw_user_meta_data, created_at, updated_at
) VALUES (
    '00000000-0000-0000-0000-000000000000',
    '00000000-0000-4000-8000-000000092601',
    'authenticated', 'authenticated', 'standalone-upgrade@example.test', '',
    now(), '{}', '{"full_name":"Standalone upgrade sentinel"}', now(), now()
);

INSERT INTO public.organizations (id, name, slug, type, plan, seat_limit, created_by)
VALUES (
    'standalone-upgrade-org', 'Standalone upgrade sentinel', 'standalone-upgrade',
    'team', 'free', 1, '00000000-0000-4000-8000-000000092601'
);
