-- Unified Project authorization and user-owned Git credential contracts.
--
-- These tests exercise real RPCs, constraints, triggers, and current-role
-- evaluation. Cloud stores no local folder, checkout, or computer identity.

BEGIN;

SELECT plan(59);

DO $$
DECLARE
    owner_id    uuid := '00000000-0000-0000-0000-000000029101';
    editor_id   uuid := '00000000-0000-0000-0000-000000029102';
    viewer_id   uuid := '00000000-0000-0000-0000-000000029103';
    baseline_id uuid := '00000000-0000-0000-0000-000000029104';
    outsider_id uuid := '00000000-0000-0000-0000-000000029105';
BEGIN
    INSERT INTO auth.users (
        instance_id, id, aud, role, email, encrypted_password,
        email_confirmed_at, raw_app_meta_data, raw_user_meta_data,
        created_at, updated_at
    )
    SELECT
        '00000000-0000-0000-0000-000000000000'::uuid,
        fixture.id, 'authenticated', 'authenticated', fixture.email, '',
        now(), '{}'::jsonb, '{}'::jsonb, now(), now()
    FROM (VALUES
        (owner_id,    'issue029-owner@example.test'),
        (editor_id,   'issue029-editor@example.test'),
        (viewer_id,   'issue029-viewer@example.test'),
        (baseline_id, 'issue029-baseline@example.test'),
        (outsider_id, 'issue029-outsider@example.test')
    ) AS fixture(id, email);

    INSERT INTO public.organizations (
        id, name, slug, type, plan, seat_limit, created_by
    ) VALUES (
        'issue029-org', 'ISSUE-029 Test Org', 'issue029-test-org',
        'team', 'enterprise', 20, owner_id
    );
    INSERT INTO public.org_members (id, org_id, user_id, role)
    VALUES
        ('issue029-om-owner', 'issue029-org', owner_id, 'owner'),
        ('issue029-om-editor', 'issue029-org', editor_id, 'member'),
        ('issue029-om-viewer', 'issue029-org', viewer_id, 'viewer'),
        ('issue029-om-baseline', 'issue029-org', baseline_id, 'member');

    INSERT INTO public.projects (
        id, name, description, org_id, created_by, share_token,
        lifecycle_status
    ) VALUES (
        'issue029-private', 'Private Project', NULL, 'issue029-org',
        owner_id, 'issue029-private-share-token', 'ready'
    );
    INSERT INTO public.project_members (
        id, org_id, project_id, user_id, role, granted_by
    ) VALUES (
        'issue029-private-owner', 'issue029-org', 'issue029-private',
        owner_id, 'admin', owner_id
    );
    UPDATE public.projects SET visibility = 'private'
    WHERE id = 'issue029-private';
    PERFORM * FROM public.add_project_member_authorized(
        'issue029-private', editor_id, 'editor', owner_id
    );
    PERFORM * FROM public.add_project_member_authorized(
        'issue029-private', viewer_id, 'viewer', owner_id
    );

    INSERT INTO public.projects (
        id, name, description, org_id, created_by, share_token,
        lifecycle_status
    ) VALUES (
        'issue029-org-visible', 'Org Visible Project', NULL, 'issue029-org',
        owner_id, 'issue029-org-share-token', 'ready'
    );
    INSERT INTO public.project_members (
        id, org_id, project_id, user_id, role, granted_by
    ) VALUES (
        'issue029-org-visible-owner', 'issue029-org',
        'issue029-org-visible', owner_id, 'admin', owner_id
    );
    UPDATE public.projects SET visibility = 'org'
    WHERE id = 'issue029-org-visible';

    INSERT INTO public.repository_scopes (
        id, project_id, name, path, exclude, max_mode
    ) VALUES (
        'issue029-child', 'issue029-private', 'Docs', 'docs', '[]', 'rw'
    );

    INSERT INTO public.access_surfaces (
        id, org_id, project_id, scope_id, kind, name, status,
        principal_type, principal_id, config, created_by
    ) VALUES
        ('issue029-surface-root', 'issue029-org', 'issue029-private',
         NULL, 'git_remote', 'Root Git', 'active', 'project',
         'issue029-private', '{"mode":"rw"}'::jsonb, owner_id),
        ('issue029-surface-child', 'issue029-org', 'issue029-private',
         'issue029-child', 'git_remote', 'Docs Git', 'active', 'scope',
         'issue029-child', '{"mode":"rw"}'::jsonb, owner_id),
        ('issue029-org-surface-root', 'issue029-org', 'issue029-org-visible',
         NULL, 'git_remote', 'Root Git', 'active', 'project',
         'issue029-org-visible', '{"mode":"rw"}'::jsonb, owner_id),
        ('issue029-cli-root', 'issue029-org', 'issue029-private',
         NULL, 'cli', 'Root CLI', 'active', 'project',
         'issue029-private', '{"mode":"rw"}'::jsonb, owner_id);

    PERFORM public.issue_user_git_http_credential(
        'issue029-credential-editor-v1', 'unused-editor-surface',
        'issue029-org', 'issue029-private', 'issue029-child', editor_id,
        'rw', 'pwg', 'e001', 'issue029-hash-editor-v1', 'hmac_sha256_v1'
    );
    PERFORM public.issue_user_git_http_credential(
        'issue029-credential-viewer-v1', 'unused-viewer-surface',
        'issue029-org', 'issue029-private', NULL, viewer_id,
        'r', 'pwg', 'v001', 'issue029-hash-viewer-v1', 'hmac_sha256_v1'
    );
    PERFORM public.issue_user_git_http_credential(
        'issue029-credential-removal-v1', 'unused-removal-surface',
        'issue029-org', 'issue029-private', NULL, viewer_id,
        'r', 'pwg', 'r001', 'issue029-hash-removal-v1', 'hmac_sha256_v1'
    );
    PERFORM public.issue_user_git_http_credential(
        'issue029-credential-org-baseline-v1', 'unused-org-surface',
        'issue029-org', 'issue029-org-visible', NULL, baseline_id,
        'r', 'pwg', 'o001', 'issue029-hash-org-baseline-v1', 'hmac_sha256_v1'
    );

    INSERT INTO public.access_surface_credentials (
        id, org_id, project_id, access_surface_id, credential_type,
        key_prefix, key_last4, key_hash, hash_alg, status, created_by
    ) VALUES (
        'issue029-shared-v1', 'issue029-org', 'issue029-private',
        'issue029-cli-root', 'bearer_token', 'cli', 's001',
        'issue029-hash-shared-v1', 'hmac_sha256_v1', 'active', owner_id
    );

    INSERT INTO public.tools (
        id, created_by, project_id, json_path, type, name, category, org_id
    ) VALUES
        ('issue029-tool-private', owner_id, 'issue029-private', '',
         'search', 'Private Tool', 'builtin', 'issue029-org'),
        ('issue029-tool-sibling', owner_id, 'issue029-org-visible', '',
         'search', 'Sibling Tool', 'builtin', 'issue029-org'),
        ('issue029-tool-org', owner_id, NULL, '',
         'search', 'Organization Tool', 'builtin', 'issue029-org');
    INSERT INTO public.access_tools (
        id, access_surface_id, tool_id, enabled, mcp_exposed
    ) VALUES (
        'issue029-access-tool-valid', 'issue029-surface-root',
        'issue029-tool-private', true, true
    );
END;
$$;

SELECT hasnt_table('public', 'project_workspace_bindings',
    'Cloud has no local checkout registration table');
SELECT hasnt_table('public', 'repo_user_permissions',
    'legacy human permission table is retired');
SELECT hasnt_table('public', 'repo_scopes',
    'synthetic-root Scope table name is retired');
SELECT has_column('public', 'access_surface_credentials', 'user_id',
    'Git credentials may identify a human principal');
SELECT hasnt_column('public', 'access_surface_credentials', 'workspace_binding_id',
    'Git credentials do not identify a local workspace');
SELECT ok(
    NOT has_function_privilege(
        'anon', 'public.unified_authorization_preflight()', 'EXECUTE'
    ),
    'anonymous clients cannot execute authorization preflight'
);
SELECT ok(
    NOT has_function_privilege(
        'authenticated', 'public.unified_authorization_preflight()', 'EXECUTE'
    ),
    'human clients cannot execute authorization preflight'
);
SELECT is(
    (SELECT count(*) FROM information_schema.tables
     WHERE table_schema = 'public'
       AND table_name = ANY (ARRAY[
         'organizations', 'org_members', 'projects', 'project_members',
         'repository_scopes', 'access_surfaces',
         'access_surface_credentials', 'access_surface_policies'
       ])),
    8::bigint,
    'all authorization and runtime boundary tables exist'
);
SELECT is(
    (SELECT role FROM public.project_members
     WHERE project_id = 'issue029-private'
       AND user_id = '00000000-0000-0000-0000-000000029101'),
    'admin', 'Project creation publishes creator Admin atomically'
);

SELECT throws_ok(
    $$SELECT * FROM public.update_project_member_role_authorized(
        'issue029-private',
        '00000000-0000-0000-0000-000000029101'::uuid,
        'viewer',
        '00000000-0000-0000-0000-000000029101'::uuid
    )$$,
    '23514',
    'project creator must retain explicit Project Admin membership',
    'Project creator cannot be downgraded'
);
SELECT throws_ok(
    $$SELECT public.remove_project_member_authorized(
        'issue029-private',
        '00000000-0000-0000-0000-000000029101'::uuid,
        '00000000-0000-0000-0000-000000029101'::uuid
    )$$,
    '23514',
    'project creator must retain explicit Project Admin membership',
    'Project creator cannot be removed'
);
SELECT throws_ok(
    $$DELETE FROM public.org_members
      WHERE org_id = 'issue029-org'
        AND user_id = '00000000-0000-0000-0000-000000029101'::uuid$$,
    '23514',
    'project creator must retain explicit Project Admin membership',
    'tenant membership cannot be removed before ownership transfer'
);
SELECT is(
    (
        public.create_project_idempotent(
            '99999999-9999-4999-8999-999999999999', repeat('9', 64),
            'issue029-rejected', 'Rejected', NULL, 'issue029-org',
            '00000000-0000-0000-0000-000000029105'::uuid,
            'rejected-token', 'empty', 20
        )->>'outcome'
    ),
    'forbidden',
    'a non-tenant creator cannot publish a partial Project fact'
);
SELECT is(
    (SELECT count(*) FROM public.projects WHERE id = 'issue029-rejected'),
    0::bigint, 'failed Project creation leaves no row'
);

SELECT is(
    (SELECT effective_role FROM public.resolve_project_role(
        'issue029-private', '00000000-0000-0000-0000-000000029101')),
    'admin', 'Organization owner inherits Project Admin'
);
SELECT is(
    (SELECT grant_source FROM public.resolve_project_role(
        'issue029-private', '00000000-0000-0000-0000-000000029101')),
    'org_owner', 'owner inheritance has an explicit source'
);
SELECT is(
    (SELECT effective_role FROM public.resolve_project_role(
        'issue029-private', '00000000-0000-0000-0000-000000029102')),
    'editor', 'explicit Project Editor is resolved'
);
SELECT is(
    (SELECT effective_role FROM public.resolve_project_role(
        'issue029-private', '00000000-0000-0000-0000-000000029103')),
    'viewer', 'explicit Project Viewer is resolved'
);
SELECT is(
    (SELECT effective_role FROM public.resolve_project_role(
        'issue029-org-visible', '00000000-0000-0000-0000-000000029104')),
    'viewer', 'org-visible baseline is Viewer'
);
SELECT is(
    (SELECT grant_source FROM public.resolve_project_role(
        'issue029-org-visible', '00000000-0000-0000-0000-000000029104')),
    'org_visibility', 'org-visible Viewer records its source'
);
SELECT is_empty(
    $$SELECT * FROM public.resolve_project_role(
        'issue029-private', '00000000-0000-0000-0000-000000029104')$$,
    'private Project denies an unlisted org member'
);
SELECT is_empty(
    $$SELECT * FROM public.resolve_project_role(
        'issue029-org-visible', '00000000-0000-0000-0000-000000029105')$$,
    'Organization membership remains the tenant boundary'
);

SELECT is(
    (SELECT s.scope_id
     FROM public.access_surface_credentials c
     JOIN public.access_surfaces s ON s.id = c.access_surface_id
     WHERE c.id = 'issue029-credential-editor-v1'),
    'issue029-child', 'the Editor credential targets the exact Scope view'
);
SELECT is(
    (SELECT user_id FROM public.access_surface_credentials
     WHERE id = 'issue029-credential-viewer-v1'),
    '00000000-0000-0000-0000-000000029103'::uuid,
    'the Viewer credential identifies only its human owner'
);
SELECT is(
    (SELECT count(*) FROM public.access_tools
     WHERE id = 'issue029-access-tool-valid'),
    1::bigint, 'same-Project Agent/tool association is accepted'
);
SELECT throws_ok(
    $$INSERT INTO public.access_tools (
        id, access_surface_id, tool_id, enabled, mcp_exposed
      ) VALUES (
        'issue029-access-tool-cross-project', 'issue029-surface-root',
        'issue029-tool-sibling', true, true
      )$$,
    'P0001',
    'access tool crosses Project or Organization boundary',
    'Agent child permissions cannot import a sibling Project tool'
);
SELECT lives_ok(
    $$INSERT INTO public.access_tools (
        id, access_surface_id, tool_id, enabled, mcp_exposed
      ) VALUES (
        'issue029-access-tool-org', 'issue029-surface-root',
        'issue029-tool-org', true, true
      )$$,
    'same-tenant Organization tool may target a Project surface'
);
SELECT throws_ok(
    $$INSERT INTO public.access_surfaces (
        id, org_id, project_id, scope_id, kind, name, status, config
      ) VALUES (
        'issue029-cross-project-surface', 'issue029-org',
        'issue029-org-visible', 'issue029-child', 'mcp', 'Invalid',
        'active', '{}'::jsonb
      )$$,
    '23503',
    'insert or update on table "access_surfaces" violates foreign key constraint "access_surfaces_scope_project_fkey"',
    'a Scope from another Project cannot target an Access Surface'
);
SELECT throws_ok(
    $$INSERT INTO public.access_surfaces (
        id, org_id, project_id, scope_id, kind, name, status, config
      ) VALUES (
        'issue029-duplicate-root-git', 'issue029-org',
        'issue029-private', NULL, 'git_remote', 'Duplicate Root Git',
        'active', '{}'::jsonb
      )$$,
    '23505',
    'duplicate key value violates unique constraint "uq_access_surfaces_builtin_target_kind"',
    'one standard root Git Surface exists per Project'
);
SELECT throws_ok(
    $$INSERT INTO public.repository_scopes (
        id, project_id, name, path, exclude, max_mode
      ) VALUES (
        'issue029-empty-scope', 'issue029-private', 'Invalid', '', '[]', 'rw'
      )$$,
    '23514',
    'new row for relation "repository_scopes" violates check constraint "repository_scopes_path_canonical"',
    'an empty path cannot create a synthetic root Scope'
);

SELECT throws_ok(
    $$SELECT public.issue_user_git_http_credential(
        'issue029-invalid-viewer-rw', 'unused-invalid-surface',
        'issue029-org', 'issue029-private', NULL,
        '00000000-0000-0000-0000-000000029103'::uuid,
        'rw', 'pwg', 'iv01', 'issue029-hash-invalid-viewer-rw',
        'hmac_sha256_v1'
    )$$,
    '42501',
    'Project Git write credential authorization denied',
    'Viewer cannot mint a read-write Git credential'
);
SELECT is(
    (SELECT key_hash FROM public.access_surface_credentials
     WHERE id = 'issue029-credential-editor-v1'),
    'issue029-hash-editor-v1', 'user credential stores only its hash'
);
SELECT hasnt_column('public', 'access_surface_credentials', 'access_key',
    'credential storage has no plaintext access-key column');

SELECT lives_ok(
    $$SELECT public.rotate_access_surface_bearer_token(
        'issue029-cli-root', 'issue029-org', 'issue029-private',
        'cli', 's002', 'issue029-hash-shared-v2', 'hmac_sha256_v1',
        '00000000-0000-0000-0000-000000029101'::uuid, NULL
    )$$,
    'shared Surface credential rotates atomically'
);
SELECT is(
    (SELECT status FROM public.access_surface_credentials
     WHERE id = 'issue029-credential-editor-v1'),
    'active', 'shared rotation does not revoke a user credential'
);
SELECT is(
    (SELECT status FROM public.access_surface_credentials
     WHERE id = 'issue029-shared-v1'),
    'revoked', 'shared rotation revokes the previous shared key'
);
SELECT is(
    (SELECT count(*) FROM public.access_surface_credentials
     WHERE credential_lifecycle = 'shared'
       AND access_surface_id = 'issue029-cli-root'
       AND status = 'active'),
    1::bigint, 'shared rotation leaves one active shared key'
);

SELECT lives_ok(
    $$SELECT public.issue_user_git_http_credential(
        'issue029-credential-editor-v2', 'unused-editor-surface-v2',
        'issue029-org', 'issue029-private', 'issue029-child',
        '00000000-0000-0000-0000-000000029102'::uuid,
        'rw', 'pwg', 'e002', 'issue029-hash-editor-v2', 'hmac_sha256_v1'
    )$$,
    'one user may issue another independent Git credential'
);
SELECT is(
    (SELECT status FROM public.access_surface_credentials
     WHERE id = 'issue029-credential-editor-v1'),
    'active', 'new issuance does not rotate an existing user credential'
);
SELECT is(
    (SELECT status FROM public.access_surface_credentials
     WHERE id = 'issue029-credential-editor-v2'),
    'active', 'the second user credential is active'
);

SELECT lives_ok(
    $$UPDATE public.repository_scopes SET max_mode = 'r'
      WHERE id = 'issue029-child'$$,
    'Scope policy can be tightened independently'
);
SELECT is(
    (SELECT effective_mode FROM public.resolve_git_runtime_credential(
        'issue029-hash-editor-v1')),
    'r', 'Scope downgrade caps the first credential to read-only'
);
SELECT is(
    (SELECT effective_mode FROM public.resolve_git_runtime_credential(
        'issue029-hash-editor-v2')),
    'r', 'Scope downgrade caps every credential for that target'
);
SELECT lives_ok(
    $$UPDATE public.repository_scopes SET max_mode = 'rw'
      WHERE id = 'issue029-child'$$,
    'Scope policy may be restored'
);
SELECT is(
    (SELECT effective_mode FROM public.resolve_git_runtime_credential(
        'issue029-hash-editor-v2')),
    'rw', 'restored target policy permits the credential ceiling again'
);

SELECT ok(
    public.revoke_user_git_http_credential(
        'issue029-credential-editor-v1', 'issue029-private',
        '00000000-0000-0000-0000-000000029102'::uuid
    ),
    'the user can revoke one exact Git credential'
);
SELECT is(
    (SELECT status FROM public.access_surface_credentials
     WHERE id = 'issue029-credential-editor-v1'),
    'revoked', 'credential revocation changes its own lifecycle'
);
SELECT is(
    (SELECT status FROM public.access_surface_credentials
     WHERE id = 'issue029-credential-editor-v2'),
    'active', 'revoking one credential leaves another active'
);

SELECT lives_ok(
    $$SELECT * FROM public.update_project_member_role_authorized(
        'issue029-private',
        '00000000-0000-0000-0000-000000029102'::uuid,
        'viewer',
        '00000000-0000-0000-0000-000000029101'::uuid
    )$$,
    'Project role downgrade is committed through the authorized RPC'
);
SELECT is(
    (SELECT effective_mode FROM public.resolve_git_runtime_credential(
        'issue029-hash-editor-v2')),
    'r', 'Editor-to-Viewer downgrade takes effect on the next Git request'
);
SELECT throws_ok(
    $$SELECT * FROM public.update_project_member_role_authorized(
        'issue029-private',
        '00000000-0000-0000-0000-000000029103'::uuid,
        'editor',
        '00000000-0000-0000-0000-000000029102'::uuid
    )$$,
    '42501',
    'project member management denied',
    'a Viewer cannot promote another Project member'
);
SELECT is(
    (SELECT count(*) FROM public.audit_logs
     WHERE project_id = 'issue029-private'
       AND action = 'project_member.role.update'),
    1::bigint, 'member role mutation and audit fact commit together'
);
SELECT lives_ok(
    $$SELECT public.remove_project_member_authorized(
        'issue029-private',
        '00000000-0000-0000-0000-000000029103'::uuid,
        '00000000-0000-0000-0000-000000029101'::uuid
    )$$,
    'Project membership can be revoked through the authorized RPC'
);
SELECT is_empty(
    $$SELECT * FROM public.resolve_git_runtime_credential(
        'issue029-hash-removal-v1')$$,
    'membership loss invalidates the credential on the next request'
);
SELECT is_empty(
    $$SELECT * FROM public.resolve_project_role(
        'issue029-private', '00000000-0000-0000-0000-000000029103')$$,
    'revoked private-Project member has no residual Human grant'
);
SELECT lives_ok(
    $$DELETE FROM public.org_members
      WHERE org_id = 'issue029-org'
        AND user_id = '00000000-0000-0000-0000-000000029104'::uuid$$,
    'Organization membership revoke removes the tenant principal'
);
SELECT is(
    (SELECT count(*) FROM public.access_surface_credentials
     WHERE id = 'issue029-credential-org-baseline-v1'),
    0::bigint, 'tenant revocation cascades its user-owned credential'
);
SELECT is(
    (public.unified_authorization_preflight()->>'invalid_project_members')::bigint
      + (public.unified_authorization_preflight()->>'creator_admin_unresolved')::bigint
      + (public.unified_authorization_preflight()->>'invalid_repository_scopes')::bigint
      + (public.unified_authorization_preflight()->>'orphan_access_surfaces')::bigint
      + (public.unified_authorization_preflight()->>'orphan_access_credentials')::bigint
      + (public.unified_authorization_preflight()->>'invalid_access_tool_bindings')::bigint,
    0::bigint,
    'post-cutover preflight reports no authorization integrity defects'
);
SELECT is(
    public.unified_authorization_preflight()->>'legacy_table_present',
    'false', 'preflight proves the duplicate permission source is absent'
);

SELECT * FROM finish();
ROLLBACK;
