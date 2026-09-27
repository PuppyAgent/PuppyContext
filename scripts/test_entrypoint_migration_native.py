#!/usr/bin/env python3
"""Supplementary SQL/role/FK rehearsal on owned native PostgreSQL 17.

This is NOT the Supabase/installer acceptance test. It supplies a small auth
schema stub and omits only three Supabase extension CREATE statements. All
application DDL, triggers, migrations, the real data runner and receipts execute.
No existing/hosted database URL is accepted.
"""
from __future__ import annotations

import re
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

from test_entrypoint_migration import BASELINE, EXPAND, apply, rehearse
from src.infra.data_migrations.database import PsqlClient


def run(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=120).stdout


def main():
    # On Homebrew the versioned binaries may not be on PATH. Use one installation.
    postgres = shutil.which('postgres') or '/opt/homebrew/opt/postgresql@17/bin/postgres'
    binaries = Path(postgres).parent
    if not re.search(r'\b17\.', run(str(binaries / 'postgres'), '--version')):
        raise RuntimeError('This supplementary test requires PostgreSQL 17')
    with tempfile.TemporaryDirectory(prefix='issue049-native-') as temporary:
        root = Path(temporary)
        data = root / 'data'
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        run(str(binaries / 'initdb'), '-D', str(data), '-U', 'postgres', '--auth=trust', '--encoding=UTF8')
        started = False
        try:
            run(str(binaries / 'pg_ctl'), '-D', str(data), '-l', str(root / 'server.log'),
                '-o', f'-p {port} -h 127.0.0.1 -k {root}', 'start')
            started = True
            admin = PsqlClient(f'postgresql://postgres@127.0.0.1:{port}/postgres', executable=str(binaries / 'psql'))
            db = PsqlClient(f'postgresql://postgres@127.0.0.1:{port}/issue049_native', executable=str(binaries / 'psql'))
            admin.scalar('CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;')
            baseline, count = re.subn(
                r'^CREATE EXTENSION IF NOT EXISTS (pg_graphql|pg_net|supabase_vault).*?;\n',
                '', BASELINE.read_text(), flags=re.M,
            )
            if count != 3:
                raise RuntimeError('Supabase baseline extension inventory changed; review the native harness')
            baseline_path = root / 'baseline_application.sql'
            baseline_path.write_text(baseline)

            def reset():
                admin.scalar('DROP DATABASE IF EXISTS issue049_native WITH (FORCE)')
                admin.scalar('CREATE DATABASE issue049_native')
                db.scalar('''CREATE SCHEMA auth; CREATE SCHEMA extensions;
CREATE TABLE auth.users(instance_id uuid,id uuid PRIMARY KEY,aud text,role text,email text,
 encrypted_password text,email_confirmed_at timestamptz,raw_app_meta_data jsonb,
 raw_user_meta_data jsonb,created_at timestamptz,updated_at timestamptz);
CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE AS $$
 SELECT nullif(current_setting('request.jwt.claim.sub',true),'')::uuid $$;
CREATE FUNCTION auth.jwt() RETURNS jsonb LANGUAGE sql STABLE AS $$
 SELECT coalesce(nullif(current_setting('request.jwt.claims',true),''),'{}')::jsonb $$;
CREATE SCHEMA supabase_migrations;
CREATE TABLE supabase_migrations.schema_migrations(version text PRIMARY KEY,name text,statements text[]);''')
                apply(db, baseline_path)
                db.scalar("INSERT INTO supabase_migrations.schema_migrations(version,name) VALUES ('20260926000000','baseline_b1')")

            def expand():
                apply(db, EXPAND)
                db.scalar("INSERT INTO supabase_migrations.schema_migrations(version,name) VALUES ('20260927010000','expand_entrypoint_storage')")

            print('SUPPLEMENTARY PostgreSQL 17 SQL rehearsal; not Supabase/installer acceptance', flush=True)
            rehearse(db, reset, expand)
        finally:
            if started:
                run(str(binaries / 'pg_ctl'), '-D', str(data), '-m', 'fast', 'stop')


if __name__ == '__main__':
    main()
