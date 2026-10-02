-- Synthetic two-tenant data for ISSUE-053. Local disposable databases ONLY.
-- The caller owns the transaction so pgTAP can roll back the entire fixture.
DO $$
DECLARE
    suffix text;
    user_id uuid;
    transaction_id bigint;
BEGIN
    FOREACH suffix IN ARRAY ARRAY['a', 'b'] LOOP
        user_id := CASE suffix
            WHEN 'a' THEN '00000000-0000-4000-8000-000000000053'::uuid
            ELSE '00000000-0000-4000-8000-000000000054'::uuid END;
        INSERT INTO auth.users (instance_id, id, aud, role, email, encrypted_password,
            email_confirmed_at, raw_app_meta_data, raw_user_meta_data, created_at, updated_at)
        VALUES ('00000000-0000-0000-0000-000000000000', user_id,
            'authenticated', 'authenticated', 'issue053-' || suffix || '@example.test',
            '', now(), '{}', '{}', now(), now());
        INSERT INTO public.organizations (id, name, slug, type, plan, seat_limit, created_by)
        VALUES ('issue053-org-' || suffix, 'Security fixture', 'issue053-org-' || suffix,
            'team', 'free', 1, user_id);
        INSERT INTO public.org_members (id, org_id, user_id, role)
        VALUES ('issue053-om-' || suffix, 'issue053-org-' || suffix, user_id, 'owner');
        INSERT INTO public.projects (id, name, org_id, created_by, share_token, lifecycle_status)
        VALUES ('issue053-project-' || suffix, 'Security fixture', 'issue053-org-' || suffix,
            user_id, 'issue053-share-' || suffix, 'ready');
        INSERT INTO public.project_members (id, org_id, project_id, user_id, role, granted_by)
        VALUES ('issue053-pm-' || suffix, 'issue053-org-' || suffix,
            'issue053-project-' || suffix, user_id, 'admin', user_id);
        INSERT INTO public.repository_scopes (id, project_id, name, path)
        VALUES ('issue053-scope-' || suffix, 'issue053-project-' || suffix, 'Docs', 'docs');
        INSERT INTO public.version_transactions (project_id, source_channel, actor, intent_type,
            status, audit_detail)
        VALUES ('issue053-project-' || suffix, 'test', user_id::text, 'operation',
            'committed', jsonb_build_object('private', suffix)) RETURNING id INTO transaction_id;
        INSERT INTO public.version_conflicts (pending_conflict_id, transaction_id, project_id,
            conflict_records)
        VALUES ('issue053-conflict-' || suffix, transaction_id, 'issue053-project-' || suffix,
            jsonb_build_array(jsonb_build_object('private', suffix)));
        INSERT INTO public.audit_logs (project_id, action, operator_id, transaction_id, metadata)
        VALUES ('issue053-project-' || suffix, 'fixture', user_id::text, transaction_id,
            jsonb_build_object('private', suffix));
        INSERT INTO public.bookmarks (project_id, path, label)
        VALUES ('issue053-project-' || suffix, 'docs/private', 'private-' || suffix);
        INSERT INTO public.connector_runs (id, connector_id, stdout)
        VALUES ('issue053-run-' || suffix, 'issue053-legacy-' || suffix, 'private-' || suffix);
        INSERT INTO public.scope_sync_events (project_id, scope_id, head_version, affected_paths)
        VALUES ('issue053-project-' || suffix, 'issue053-scope-' || suffix,
            repeat(suffix, 40), '["docs/private"]');
        INSERT INTO public.scope_sync_settings (project_id, scope_id)
        VALUES ('issue053-project-' || suffix, 'issue053-scope-' || suffix);
        INSERT INTO public.tables (id, project_id, name, data)
        VALUES ('issue053-table-' || suffix, 'issue053-project-' || suffix,
            'private-' || suffix, jsonb_build_object('private', suffix));
    END LOOP;
END $$;
