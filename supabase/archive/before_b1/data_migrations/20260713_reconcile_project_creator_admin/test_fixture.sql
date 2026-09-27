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
    ('00000000-0000-0000-0000-000000013101'::uuid, 'creator-repair-owner@example.test'),
    ('00000000-0000-0000-0000-000000013102'::uuid, 'creator-repair-missing@example.test'),
    ('00000000-0000-0000-0000-000000013103'::uuid, 'creator-repair-viewer@example.test')
) AS fixture(id, email);

INSERT INTO public.organizations (
    id, name, slug, type, plan, seat_limit, created_by
) VALUES (
    'creator-repair-org', 'Creator Repair Test', 'creator-repair-test',
    'team', 'enterprise', 10,
    '00000000-0000-0000-0000-000000013101'::uuid
);

INSERT INTO public.org_members (id, org_id, user_id, role)
VALUES
    ('creator-repair-owner-membership', 'creator-repair-org',
     '00000000-0000-0000-0000-000000013101'::uuid, 'owner'),
    ('creator-repair-viewer-membership', 'creator-repair-org',
     '00000000-0000-0000-0000-000000013103'::uuid, 'viewer');

-- Recreate facts written before the creator guards existed.  Only this local
-- fixture disables them; the migration under test runs with all guards active.
ALTER TABLE public.projects
    DISABLE TRIGGER trg_projects_creator_admin_guard;
ALTER TABLE public.project_members
    DISABLE TRIGGER trg_project_members_creator_admin_guard;

INSERT INTO public.projects (id, name, org_id, created_by)
VALUES
    ('creator-repair-missing-project', 'Missing Membership',
     'creator-repair-org',
     '00000000-0000-0000-0000-000000013102'::uuid),
    ('creator-repair-viewer-project', 'Downgraded Creator',
     'creator-repair-org',
     '00000000-0000-0000-0000-000000013103'::uuid);

INSERT INTO public.project_members (
    id, org_id, project_id, user_id, role, granted_by
) VALUES (
    'creator-repair-viewer-project-membership',
    'creator-repair-org',
    'creator-repair-viewer-project',
    '00000000-0000-0000-0000-000000013103'::uuid,
    'viewer',
    '00000000-0000-0000-0000-000000013101'::uuid
);

ALTER TABLE public.project_members
    ENABLE TRIGGER trg_project_members_creator_admin_guard;
ALTER TABLE public.projects
    ENABLE TRIGGER trg_projects_creator_admin_guard;

COMMIT;
