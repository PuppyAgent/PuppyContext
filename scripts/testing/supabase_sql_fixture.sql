-- Disposable-stack fixture only, NOT a product migration or real user.
-- The existing GC smoke probe explicitly skips when no organization exists.
-- One org exercises it without filling the global billing claim batch (25).
INSERT INTO auth.users
    (id, aud, role, email, encrypted_password, email_confirmed_at,
     raw_app_meta_data, raw_user_meta_data, created_at, updated_at)
VALUES ('00000000-0000-0000-0000-000000062003', 'authenticated', 'authenticated',
        'hosting-sql-probe@example.test', '', now(), '{}', '{}', now(), now());
INSERT INTO public.organizations (id, name, slug, type, plan, created_by)
VALUES ('hosting-sql-probe', 'Hosting SQL probe', 'hosting-sql-probe', 'team', 'free',
        '00000000-0000-0000-0000-000000062003');
