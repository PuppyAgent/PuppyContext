BEGIN;

INSERT INTO auth.users (
    instance_id, id, aud, role, email, encrypted_password,
    email_confirmed_at, raw_app_meta_data, raw_user_meta_data,
    created_at, updated_at
)
SELECT
    '00000000-0000-0000-0000-000000000000'::uuid,
    fixture.id,
    'authenticated',
    'authenticated',
    fixture.email,
    '',
    now(),
    '{}'::jsonb,
    '{}'::jsonb,
    now(),
    now()
FROM (VALUES
    ('00000000-0000-0000-0000-000000012901'::uuid, 'db-migration-owner@example.test'),
    ('00000000-0000-0000-0000-000000012902'::uuid, 'db-migration-viewer@example.test')
) AS fixture(id, email);

INSERT INTO public.organizations (
    id, name, slug, type, plan, seat_limit, created_by
) VALUES (
    'db-migration-org', 'DB Migration Test', 'db-migration-test',
    'team', 'enterprise', 10,
    '00000000-0000-0000-0000-000000012901'::uuid
);

INSERT INTO public.org_members (id, org_id, user_id, role)
VALUES
    ('db-migration-owner-membership', 'db-migration-org',
     '00000000-0000-0000-0000-000000012901'::uuid, 'owner'),
    ('db-migration-viewer-membership', 'db-migration-org',
     '00000000-0000-0000-0000-000000012902'::uuid, 'member');

SELECT * FROM public.create_project_with_admin(
    'db-migration-project',
    'DB Migration Project',
    NULL,
    'db-migration-org',
    '00000000-0000-0000-0000-000000012901'::uuid,
    'db-migration-share-token'
);

INSERT INTO public.repo_scopes (
    id, project_id, name, path, exclude, mode, is_root
) VALUES (
    'db-migration-root', 'db-migration-project', 'Root', '', '[]', 'rw', true
);

INSERT INTO public.repo_user_permissions (
    id, project_id, user_id, role, granted_by
) VALUES (
    'db-migration-legacy-viewer',
    'db-migration-project',
    '00000000-0000-0000-0000-000000012902'::uuid,
    'reader',
    '00000000-0000-0000-0000-000000012901'::uuid
);

COMMIT;
