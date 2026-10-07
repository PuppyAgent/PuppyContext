-- The first native schema release must remain compatible with stored projects.
-- Run only on disposable test databases; all synthetic writes roll back.
BEGIN;
SELECT no_plan();
\ir _support/data_api_containment_fixture.inc

SELECT has_function('public', 'get_version_repository_snapshot', ARRAY['text'],
    'repository authority discovery is installed');
SELECT is(public.get_version_repository_snapshot('issue053-project-a'), NULL::jsonb,
    'an existing project without native enrollment has an explicit absent snapshot');
SELECT is((SELECT count(*) FROM public.version_repositories), 0::bigint,
    'Expand does not enroll existing projects');

SET LOCAL ROLE anon;
SELECT throws_ok($sql$SELECT public.get_version_repository_snapshot('issue053-project-a')$sql$,
    '42501', 'permission denied for function get_version_repository_snapshot',
    'anonymous callers cannot inspect repository authority');
RESET ROLE;
SET LOCAL ROLE authenticated;
SELECT throws_ok($sql$SELECT public.get_version_repository_snapshot('issue053-project-a')$sql$,
    '42501', 'permission denied for function get_version_repository_snapshot',
    'direct authenticated clients cannot inspect repository authority');
RESET ROLE;

SET LOCAL ROLE service_role;
SELECT is(public.get_version_repository_snapshot('issue053-project-a'), NULL::jsonb,
    'the backend can discover absent native authority instead of receiving a missing RPC');
SELECT is((SELECT published FROM public.publish_version_project_update(
    p_project_id => 'issue053-project-a', p_old_root_hash => '',
    p_new_root_hash => repeat('a', 40), p_head_commit_id => repeat('b', 40),
    p_who => '00000000-0000-4000-8000-000000000053', p_message => 'Expand compatibility',
    p_event_type => 'commit', p_changes => '[]'::jsonb, p_conflicts => NULL::jsonb,
    p_created_at => now()::text, p_audit_agent_id => '', p_audit_detail => '{}'::jsonb,
    p_expected_scope_head_commit_id => '')), true,
    'existing project publication remains usable before native conversion');
SELECT is((SELECT version_root_hash FROM public.projects WHERE id='issue053-project-a'),
    repeat('a', 40), 'the existing authority retains its published root');
SELECT is(public.get_version_repository_snapshot('issue053-project-a'), NULL::jsonb,
    'a legacy write does not silently activate native authority');
SELECT is((SELECT published FROM public.publish_version_project_update(
    p_project_id => 'issue053-project-a', p_old_root_hash => repeat('a', 40),
    p_new_root_hash => repeat('c', 40), p_head_commit_id => repeat('d', 40),
    p_who => '00000000-0000-4000-8000-000000000053', p_message => 'Stale head',
    p_event_type => 'commit', p_changes => '[]'::jsonb, p_conflicts => NULL::jsonb,
    p_created_at => now()::text, p_audit_agent_id => '', p_audit_detail => '{}'::jsonb,
    p_expected_scope_head_commit_id => '')), false,
    'the new source-head CAS guard rejects stale publication');
RESET ROLE;

SELECT * FROM finish();
ROLLBACK;
