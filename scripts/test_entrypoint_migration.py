#!/usr/bin/env python3
"""Rehearse ISSUE-049 on an owned ephemeral Supabase, never a linked project.

Exercises the checked-in schema/data runner, live transition writes, the future
Contract, real roles/FKs, and fresh installation. Requires the locked backend env.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from database_baseline import LocalStack, ROOT
from src.infra.data_migrations.catalog import DataMigrationCatalog
from src.infra.data_migrations.database import PsqlClient
from src.infra.data_migrations.errors import ExecutionError
from src.infra.data_migrations.runner import DataMigrationRunner
from self_hosted_migrate import migrate_data

BASELINE = ROOT / 'supabase/migrations/20260926000000_baseline_b1.sql'
EXPAND = ROOT / 'supabase/migrations/20260927010000_expand_entrypoint_storage.sql'
MIGRATION_ID = '20260927_entrypoint_storage_backfill'
DATA = ROOT / 'supabase/data_migrations' / MIGRATION_ID
FIXTURE = ROOT / 'supabase/test_fixtures/entrypoint_storage.sql'


def apply(db, path):
    db.command(['-q', '-v', 'ON_ERROR_STOP=1', '-f', str(path)])


def expect_error(db, sql, message):
    try:
        db.scalar(sql)
    except ExecutionError as error:
        assert message in str(error), str(error)
    else:
        raise AssertionError('Expected database rejection: ' + message)


def snapshot(db, table, *, where='true', exclude=()):
    expression = 'to_jsonb(t)' + ''.join(f" - '{column}'" for column in exclude)
    return json.loads(db.scalar(
        f"SELECT coalesce(jsonb_agg({expression} ORDER BY id),'[]') FROM public.{table} t WHERE {where}"
    ))


def permissions(db):
    for table in ('access_tools', 'github_sync_bindings', 'github_sync_log', 'search_index_tasks'):
        assert db.scalar(f"SELECT relrowsecurity FROM pg_class WHERE oid='public.{table}'::regclass") == 't'
        for role in ('anon', 'authenticated'):
            assert db.scalar(f"SELECT has_table_privilege('{role}','public.{table}','SELECT,INSERT,UPDATE,DELETE,TRUNCATE')") == 'f'
            expect_error(db, f'SET ROLE {role}; SELECT * FROM public.{table}', 'permission denied')
        assert db.scalar(f'SET ROLE service_role; SELECT count(*) >= 0 FROM public.{table}') == 't'


def final_shape(db):
    assert db.scalar("SELECT to_regclass('public.github_integrations') IS NULL") == 't'
    assert db.scalar("SELECT count(*) FROM information_schema.columns WHERE table_schema='public' AND ((table_name='access_tools' AND column_name='access_point_id') OR (table_name='github_sync_log' AND column_name='integration_id'))") == '0'
    assert db.scalar("SELECT count(*) FROM public.uploads WHERE type='search_index'") == '0'
    assert db.scalar("SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND tgname IN ('mirror_search_tasks_from_uploads','mirror_search_tasks_to_uploads','a00_sync_access_tool_surface_columns','a00_sync_github_binding_columns')") == '0'
    assert db.scalar("SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.prosrc LIKE '%access_point_id%' AND (p.prosrc LIKE '%access_tools%' OR p.proname='_validate_access_tool_project_boundary')") == '0'
    assert db.scalar("SELECT to_regclass('puppypay.billing_accounts') IS NULL") == 't'
    permissions(db)


def rehearse(db, reset, expand):
    runner = DataMigrationRunner(DataMigrationCatalog(ROOT), db, environment={})

    reset()
    expand()
    apply(db, DATA / 'contract.pending.sql')
    # Empty installs need no historical receipt; a later runner invocation is safe.
    runner.run(MIGRATION_ID)
    runner.run(MIGRATION_ID)
    final_shape(db)
    print('PASS fresh B1 -> Expand -> Contract -> repeated data runner', flush=True)

    reset()
    apply(db, FIXTURE)
    original_uploads = snapshot(db, 'uploads', where="type <> 'search_index'")
    original_tasks = snapshot(db, 'uploads', where="type = 'search_index'", exclude=('type',))
    original_bindings = snapshot(db, 'github_integrations')
    original_logs = snapshot(db, 'github_sync_log')
    original_access = snapshot(db, 'access_tools')
    original_oauth = snapshot(db, 'oauth_connections')
    expand()
    # A populated database cannot bypass its immutable verified data receipt.
    try:
        apply(db, DATA / 'contract.pending.sql')
    except ExecutionError as error:
        assert 'DATA_MIGRATION_REQUIRED:' + MIGRATION_ID in str(error)
    else:
        raise AssertionError('Contract accepted missing receipt')
    assert db.scalar("SELECT to_regclass('public.github_integrations') IS NOT NULL") == 't'
    assert snapshot(db, 'access_tools', exclude=('access_surface_id',)) == original_access

    # Dirty target causes rollback of the backfill and never earns a receipt.
    db.scalar("ALTER TABLE public.search_index_tasks DISABLE TRIGGER mirror_search_tasks_to_uploads; INSERT INTO public.search_index_tasks(id,project_id,status) VALUES ('issue049-tool-1','issue049-project','failed'); ALTER TABLE public.search_index_tasks ENABLE TRIGGER mirror_search_tasks_to_uploads")
    try:
        runner.run(MIGRATION_ID)
    except ExecutionError as error:
        assert 'search index task mismatch' in str(error)
    else:
        raise AssertionError('Conflicting target accepted')
    assert db.receipt(MIGRATION_ID) is None
    assert db.scalar('SELECT count(*) FROM public.access_tools WHERE access_surface_id IS NULL') == '1'
    db.scalar("ALTER TABLE public.search_index_tasks DISABLE TRIGGER mirror_search_tasks_to_uploads; DELETE FROM public.search_index_tasks; ALTER TABLE public.search_index_tasks ENABLE TRIGGER mirror_search_tasks_to_uploads")
    migrate_data(ROOT, db)
    receipt = db.receipt(MIGRATION_ID)
    migrate_data(ROOT, db)
    assert db.receipt(MIGRATION_ID) == receipt
    assert snapshot(db, 'uploads', where="type <> 'search_index'") == original_uploads
    assert snapshot(db, 'search_index_tasks') == original_tasks
    assert snapshot(db, 'github_sync_bindings') == original_bindings
    assert snapshot(db, 'github_sync_log', exclude=('binding_id',)) == original_logs
    assert snapshot(db, 'access_tools', exclude=('access_surface_id',)) == original_access
    assert snapshot(db, 'oauth_connections') == original_oauth
    permissions(db)
    print('PASS populated upgrade, conflict rollback, exact payloads, receipt and idempotent retry', flush=True)

    # Old and new writers keep one observable record during the rollback window.
    db.scalar("UPDATE public.uploads SET progress=61, message='late old worker' WHERE id='issue049-tool-2'")
    assert db.scalar("SELECT progress FROM public.search_index_tasks WHERE id='issue049-tool-2'") == '61'
    db.scalar("UPDATE public.search_index_tasks SET progress=73, message='new worker' WHERE id='issue049-tool-2'")
    assert db.scalar("SELECT progress FROM public.uploads WHERE id='issue049-tool-2'") == '73'
    db.scalar("UPDATE public.github_integrations SET last_imported_sha=repeat('c',40) WHERE id='issue049-binding'")
    assert db.scalar("SELECT last_imported_sha FROM public.github_sync_bindings WHERE id='issue049-binding'") == 'c'*40
    db.scalar("INSERT INTO public.github_sync_log(id,binding_id,direction,status) VALUES ('issue049-new-log','issue049-binding','import','pending')")
    assert db.scalar("SELECT integration_id FROM public.github_sync_log WHERE id='issue049-new-log'") == 'issue049-binding'
    db.scalar("INSERT INTO public.access_tools(id,access_surface_id,tool_id) VALUES ('issue049-new-link','issue049-surface','issue049-tool-2')")
    assert db.scalar("SELECT access_point_id FROM public.access_tools WHERE id='issue049-new-link'") == 'issue049-surface'
    expect_error(db, "UPDATE public.access_tools SET access_surface_id='issue049-other-surface' WHERE id='issue049-link'", 'crosses Project or Organization boundary')
    expect_error(db, "INSERT INTO public.search_index_tasks(id,project_id) VALUES ('issue049-upload-1','issue049-project')", 'collides with a non-search upload')
    expect_error(db, "INSERT INTO public.access_tools(access_surface_id,access_point_id,tool_id) VALUES ('issue049-surface','issue049-other-surface','issue049-tool-3')", 'conflicting access surface identifiers')
    db.scalar("INSERT INTO public.uploads(id,project_id,type) VALUES ('issue049-delete','issue049-project','search_index'); DELETE FROM public.search_index_tasks WHERE id='issue049-delete'")
    assert db.scalar("SELECT count(*) FROM public.uploads WHERE id='issue049-delete'") == '0'
    # PostgreSQL FK cascade semantics remain intact through the mirror triggers.
    db.scalar("INSERT INTO public.projects(id,name,org_id,share_token,lifecycle_status) VALUES ('issue049-cascade-project','Cascade fixture','issue049-other-org','issue049-cascade-share','ready'); INSERT INTO public.uploads(id,project_id,type) VALUES ('issue049-cascade','issue049-cascade-project','search_index'); DELETE FROM public.projects WHERE id='issue049-cascade-project'")
    assert db.scalar("SELECT count(*) FROM public.search_index_tasks WHERE id='issue049-cascade'") == '0'
    db.scalar("INSERT INTO public.github_integrations(id,project_id,github_repo_owner,github_repo_name) VALUES ('issue049-old-insert','issue049-other-project','fixture','old-client'); DELETE FROM public.github_integrations WHERE id='issue049-old-insert'")
    assert db.scalar("SELECT count(*) FROM public.github_sync_bindings WHERE id='issue049-old-insert'") == '0'
    runner.verify(MIGRATION_ID)
    print('PASS old/new live writes, conflict rejection, deletion, tenant boundary and cascades', flush=True)

    # Runtime privilege cannot bypass RLS through the legacy name.
    for role in ('anon','authenticated'):
        expect_error(db, f'SET ROLE {role}; SELECT * FROM public.github_integrations', 'permission denied')
    assert db.scalar("SELECT reloptions::text LIKE '%security_invoker=true%' FROM pg_class WHERE oid='public.github_integrations'::regclass") == 't'

    # A receipt alone cannot authorize a destructive operation after drift.
    db.scalar("ALTER TABLE public.search_index_tasks DISABLE TRIGGER mirror_search_tasks_to_uploads; UPDATE public.search_index_tasks SET progress=99 WHERE id='issue049-tool-2'; ALTER TABLE public.search_index_tasks ENABLE TRIGGER mirror_search_tasks_to_uploads")
    try:
        apply(db, DATA / 'contract.pending.sql')
    except ExecutionError as error:
        assert 'search index task mismatch' in str(error)
    else:
        raise AssertionError('Contract ignored data drift')
    assert db.scalar("SELECT to_regclass('public.github_integrations') IS NOT NULL") == 't'
    db.scalar("UPDATE public.search_index_tasks SET progress=73 WHERE id='issue049-tool-2'")
    runner.verify(MIGRATION_ID)
    before_contract = snapshot(db, 'search_index_tasks')
    apply(db, DATA / 'contract.pending.sql')
    final_shape(db)
    runner.verify(MIGRATION_ID)
    runner.run(MIGRATION_ID)
    assert snapshot(db, 'search_index_tasks') == before_contract
    assert snapshot(db, 'uploads') == original_uploads
    assert snapshot(db, 'oauth_connections') == original_oauth
    expect_error(db, "UPDATE public.access_tools SET access_surface_id='issue049-other-surface' WHERE id='issue049-link'", 'crosses Project or Organization boundary')
    expect_error(db, "INSERT INTO public.access_tools(access_surface_id,tool_id) VALUES ('missing','issue049-tool-3')", 'surface not found')
    expect_error(db, "INSERT INTO public.github_sync_log(binding_id,direction,status) VALUES ('missing','import','pending')", 'foreign key constraint')
    expect_error(db, "INSERT INTO public.uploads(id,project_id,type) VALUES ('no-old-writer','issue049-project','search_index')", 'chk_uploads_type')
    db.scalar("INSERT INTO public.access_tools(id,access_surface_id,tool_id) VALUES ('final-link','issue049-surface','issue049-tool-3') ON CONFLICT (access_surface_id,tool_id) DO UPDATE SET enabled=EXCLUDED.enabled")
    assert db.scalar("SELECT (public.unified_authorization_preflight()->>'invalid_access_tool_bindings')::int") == '0'
    db.scalar("DELETE FROM public.github_sync_bindings WHERE id='issue049-binding'")
    assert db.scalar('SELECT count(*) FROM public.github_sync_log') == '0'
    print('PASS guarded Contract, final schema/RPCs/FKs, post-contract retry and unrelated uploads retained', flush=True)


def main():
    os.environ.setdefault('SUPABASE_INTERNAL_IMAGE_REGISTRY', 'docker.io')
    with tempfile.TemporaryDirectory(prefix='issue049-db-') as directory:
        stack = LocalStack(Path(directory), [BASELINE])
        try:
            stack.cli('start', '--exclude', 'studio,imgproxy,mailpit,edge-runtime,logflare,vector,supavisor,realtime,storage-api')
            db = PsqlClient('postgresql://postgres:postgres@127.0.0.1:55392/postgres')

            def reset():
                stack.replace_migrations([BASELINE])
                stack.cli('db', 'reset', '--local', '--no-seed')

            def expand():
                stack.replace_migrations([BASELINE, EXPAND])
                stack.cli('migration', 'up', '--local')

            rehearse(db, reset, expand)
        finally:
            stack.cli('stop', '--no-backup')


if __name__ == '__main__':
    main()
