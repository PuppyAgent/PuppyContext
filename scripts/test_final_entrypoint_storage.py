#!/usr/bin/env python3
"""Owned PostgreSQL 17 SQL/role rehearsal for final ISSUE-049 storage.

Native mode stubs only Supabase Auth/extension infrastructure; it is explicitly
not the separate full Supabase/GoTrue/installer/runtime acceptance. It accepts
no existing database address, touches no hosted database and retains no secrets.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

from entrypoint_source_decisions import INVENTORY, approve, freeze, reviewed_rows
from src.infra.data_migrations.catalog import DataMigrationCatalog
from src.infra.data_migrations.database import PsqlClient
from src.infra.data_migrations.errors import ExecutionError
from src.infra.data_migrations.runner import DataMigrationRunner
from test_entrypoint_migration import (
    BASELINE,
    EXPAND,
    FIXTURE,
    ROOT,
    apply,
    expect_error,
    snapshot,
)
from test_entrypoint_migration_native import run

NEW_EXPAND = (
    ROOT / "supabase/migrations/20261003220000_expand_final_entrypoint_storage.sql"
)
CONTAINMENT = (
    ROOT / "supabase/migrations/20261003000000_contain_internal_data_api_access.sql"
)
MIGRATION = "20261003_final_entrypoint_storage"


@contextmanager
def owned_native():
    binaries = Path(
        shutil.which("postgres") or "/opt/homebrew/opt/postgresql@17/bin/postgres"
    ).parent
    assert re.search(r"\b17\.", run(str(binaries / "postgres"), "--version"))
    with tempfile.TemporaryDirectory(prefix="issue049-final-native-") as name:
        root = Path(name)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        run(
            str(binaries / "initdb"),
            "-D",
            str(root / "data"),
            "-U",
            "postgres",
            "--auth=trust",
            "--encoding=UTF8",
        )
        run(
            str(binaries / "pg_ctl"),
            "-D",
            str(root / "data"),
            "-l",
            str(root / "server.log"),
            "-o",
            f"-p {port} -h 127.0.0.1 -k {root}",
            "start",
        )
        try:
            url = f"postgresql://postgres@127.0.0.1:{port}/postgres"
            db = PsqlClient(
                url,
                executable=str(binaries / "psql"),
                base_environment={"PATH": os.environ["PATH"]},
            )
            db.scalar("""CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;
CREATE SCHEMA auth; CREATE SCHEMA extensions;
CREATE TABLE auth.users(instance_id uuid,id uuid PRIMARY KEY,aud text,role text,email text,
 encrypted_password text,email_confirmed_at timestamptz,raw_app_meta_data jsonb,
 raw_user_meta_data jsonb,created_at timestamptz,updated_at timestamptz);
CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE AS $$ SELECT nullif(current_setting('request.jwt.claim.sub',true),'')::uuid $$;
CREATE FUNCTION auth.jwt() RETURNS jsonb LANGUAGE sql STABLE AS $$ SELECT coalesce(nullif(current_setting('request.jwt.claims',true),''),'{}')::jsonb $$;
CREATE SCHEMA supabase_migrations;
CREATE TABLE supabase_migrations.schema_migrations(version text PRIMARY KEY,name text,statements text[]);""")
            sql, count = re.subn(
                r"^CREATE EXTENSION IF NOT EXISTS (pg_graphql|pg_net|supabase_vault).*?;\n",
                "",
                BASELINE.read_text(),
                flags=re.MULTILINE,
            )
            assert count == 3
            baseline = root / "baseline.sql"
            baseline.write_text(sql)
            apply(db, baseline)
            db.scalar(
                "INSERT INTO supabase_migrations.schema_migrations(version,name) VALUES ('20260926000000','baseline_b1')"
            )
            yield db, root
        finally:
            run(
                str(binaries / "pg_ctl"), "-D", str(root / "data"), "-m", "fast", "stop"
            )


def expanded(db):
    for sql in (EXPAND, CONTAINMENT, NEW_EXPAND):
        apply(db, sql)
        version, label = sql.stem.split("_", 1)
        db.scalar(
            "INSERT INTO supabase_migrations.schema_migrations(version,name) VALUES (:'version',:'name')",
            variables={"version": version, "name": label},
        )


def frozen(db):
    # No application runs in this owned SQL-only test. This synthetic control
    # record tests the gate; it is NOT hosted producer/queue exit evidence.
    freeze(
        db,
        json.dumps(
            {
                "environment": "owned SQL-only native fixture; no application processes",
                "producer_stop_verified": True,
                "queue_drain_verified": True,
                "old_consumers_exited": True,
                "records": [
                    {
                        "fixture_only": True,
                        "fact": "This script owns the only writer; no queue or API is started",
                    }
                ],
            }
        ).encode(),
        restore_point_ref="owned-native-fixture",
        approved_by="SQL-test-only",
    )


def owner_write(db, sql):
    db.scalar(
        """BEGIN; DO $$ BEGIN PERFORM set_config('puppyone.entrypoint_migration_token',
      (SELECT migration_token::text FROM public.entrypoint_cutover_control WHERE singleton),true); END $$;
"""
        + sql
        + "; COMMIT;"
    )


def expect_job_error(db, runner, message):
    before = snapshot(db, "import_database_sources")
    try:
        runner.run(MIGRATION)
    except ExecutionError as error:
        assert message in str(error), str(error)
    else:
        raise AssertionError("Migration accepted an invalid state: " + message)
    assert db.receipt(MIGRATION) is None
    assert snapshot(db, "import_database_sources") == before


def source_fixture(db):
    db.scalar("""
INSERT INTO public.connections(id,org_id,project_id,provider,name,direction,config,target_path,created_by)
VALUES
 ('binding','issue049-org','issue049-project','url','Binding','inbound',
  '{"source":{"resource_url":"https://example.test"},"nested":{"connection_id":"user-value"}}','notes','00000000-0000-4000-8000-000000000049'),
 ('source','issue049-org','issue049-project','database','Source','inbound',
  '{"db_provider":"supabase","db_config":{"ciphertext":"opaque-encrypted-value","nonce":"opaque-nonce"},"retained":"metadata"}','','00000000-0000-4000-8000-000000000049'),
 ('dual','issue049-org','issue049-project','url','Dual','inbound',
  '{"source":{"resource_url":"https://example.test"},"db_provider":"supabase","db_config":{"ciphertext":"dual-encrypted-value"}}','dual-notes','00000000-0000-4000-8000-000000000049'),
 ('legacy','issue049-org','issue049-project','database','Legacy history','inbound',
  '{"db_provider":"supabase","db_config":{"ciphertext":"retained-encrypted-value"}}','','00000000-0000-4000-8000-000000000049'),
 ('history-only','issue049-org','issue049-project','database','Historical binding only','inbound',
  '{"db_config":{"ciphertext":"binding-only-opaque"}}','historical-target','00000000-0000-4000-8000-000000000049');
INSERT INTO public.sync_runs(id,connection_id,project_id,triggered_by,direction,status,result)
VALUES ('run-dual','dual','issue049-project','manual','inbound','failed','{"connection_id":"user-history"}'),
       ('run-legacy','legacy','issue049-project','manual','inbound','failed','{"error":"historical"}'),
       ('run-history-only','history-only','issue049-project','manual','inbound','failed','{"error":"binding-only-history"}');
UPDATE public.connections SET last_sync_run_id='run-dual' WHERE id='dual';
UPDATE public.connections SET last_sync_run_id='run-legacy' WHERE id='legacy';
UPDATE public.connections SET last_sync_run_id='run-history-only' WHERE id='history-only';
""")


def decisions(db):
    rows = json.loads(db.scalar(INVENTORY))
    serialized = json.dumps(rows)
    assert (
        "opaque-encrypted" not in serialized
        and "dual-encrypted" not in serialized
        and "resource_url" not in serialized
    )
    assert all(row["disposition"] is None for row in rows)
    by_id = {row["legacy_id"]: row for row in rows}
    for row in rows:
        row.update(
            approved_by="local-fixture-review",
            evidence_ref="explicit-fixture-caller-and-history",
        )
    by_id["binding"].update(disposition="synchronize")
    by_id["history-only"].update(
        disposition="synchronize",
        binding_read_only_reason="Reviewed historical binding only; no Import caller or source exists",
    )
    by_id["source"].update(disposition="import", import_database_source_id="source")
    by_id["dual"].update(
        disposition="both", import_database_source_id="explicit-dual-source"
    )
    by_id["legacy"].update(
        disposition="both",
        import_database_source_id="legacy-source",
        binding_read_only_reason="Historical database provider: read-only retention, not executable",
    )
    return reviewed_rows({"format_version": 1, "rows": rows})


def rehearse_expand(db):
    runner = DataMigrationRunner(DataMigrationCatalog(ROOT), db, environment={})
    original = snapshot(db, "connections")
    runs = snapshot(db, "sync_runs")
    expect_job_error(db, runner, "ENTRYPOINT_FREEZE_REQUIRED")
    frozen(db)
    for role in ("anon", "authenticated", "service_role"):
        expect_error(
            db,
            f"SET ROLE {role}; SELECT * FROM public.entrypoint_cutover_control",
            "permission denied",
        )
    expect_error(
        db,
        "SET ROLE service_role; UPDATE public.connections SET name='unsafe' WHERE id='source'",
        "ENTRYPOINT_WRITES_FROZEN",
    )
    expect_error(
        db,
        "SET ROLE service_role; INSERT INTO public.entrypoint_source_decisions(legacy_id) VALUES ('injected')",
        "permission denied",
    )
    for role in ("anon", "authenticated"):
        expect_error(
            db,
            f"SET ROLE {role}; SELECT * FROM public.import_database_sources",
            "permission denied",
        )
    expect_job_error(db, runner, "ENTRYPOINT_CLASSIFICATION_REQUIRED")
    rows = decisions(db)
    approve(db, rows)
    approve(db, rows)
    # A structurally source-looking database row WITH history cannot be deleted.
    db.scalar(
        "UPDATE public.entrypoint_source_decisions SET disposition='import', import_database_source_id='legacy', binding_read_only_reason=NULL WHERE legacy_id='legacy'"
    )
    expect_job_error(db, runner, "ENTRYPOINT_IMPORT_WOULD_LOSE_BINDING_FACTS")
    db.scalar("DELETE FROM public.entrypoint_source_decisions WHERE legacy_id='legacy'")
    approve(db, [row for row in rows if row["legacy_id"] == "legacy"])
    owner_write(
        db,
        "UPDATE public.connections SET name='changed-after-review' WHERE id='source'",
    )
    expect_job_error(db, runner, "ENTRYPOINT_CLASSIFICATION_REQUIRED")
    # Restoring a value is insufficient: the changed updated_at is also evidence.
    owner_write(db, "UPDATE public.connections SET name='Source' WHERE id='source'")
    expect_job_error(db, runner, "ENTRYPOINT_CLASSIFICATION_REQUIRED")
    db.scalar("DELETE FROM public.entrypoint_source_decisions WHERE legacy_id='source'")
    approve(db, [row for row in decisions(db) if row["legacy_id"] == "source"])
    original = snapshot(db, "connections")
    # A conflicting preexisting target rolls back ALL copies and the receipt.
    owner_write(
        db,
        "INSERT INTO public.import_database_sources(id,project_id,org_id,name,provider) VALUES ('source','issue049-project','issue049-org','conflicting','supabase')",
    )
    expect_job_error(db, runner, "ENTRYPOINT_SOURCE_COPY_MISMATCH")
    owner_write(db, "DELETE FROM public.import_database_sources WHERE id='source'")
    runner.run(MIGRATION)
    receipt = db.receipt(MIGRATION)
    assert receipt and receipt["verified"]
    runner.run(MIGRATION)
    runner.verify(MIGRATION)
    assert db.receipt(MIGRATION) == receipt
    assert snapshot(db, "connections") == original and snapshot(db, "sync_runs") == runs
    sources = snapshot(db, "import_database_sources")
    assert {row["id"] for row in sources} == {
        "source",
        "explicit-dual-source",
        "legacy-source",
    }
    assert (
        next(s for s in sources if s["id"] == "source")["synchronize_binding_id"]
        is None
    )
    assert (
        next(s for s in sources if s["id"] == "explicit-dual-source")[
            "synchronize_binding_id"
        ]
        == "dual"
    )
    for row in sources:
        old_id = row["synchronize_binding_id"] or row["id"]
        old = next(b for b in original if b["id"] == old_id)
        assert row["config"] == old["config"] and row["created_by"] == old["created_by"]
        assert (
            row["created_at"] == old["created_at"]
            and row["updated_at"] == old["updated_at"]
        )
    print(
        "PASS explicit classification, missing/stale/lossy/colliding refusal, atomic receipt, exact ciphertext/history, roles and retry",
        flush=True,
    )


def rehearse_contract(db, *, populated):
    old_contract = (
        ROOT
        / "supabase/data_migrations/20260927_entrypoint_storage_backfill/contract.pending.sql"
    )
    contract = ROOT / "supabase/data_migrations" / MIGRATION / "contract.pending.sql"
    runner = DataMigrationRunner(DataMigrationCatalog(ROOT), db, environment={})
    original_bindings = snapshot(db, "connections")
    original_runs = snapshot(db, "sync_runs")
    original_github = snapshot(db, "github_sync_bindings")
    original_logs = snapshot(db, "github_sync_log", exclude=("integration_id",))
    original_sources = snapshot(db, "import_database_sources")
    apply(db, old_contract)
    if populated:
        saved = db.scalar(
            "SELECT summary::text FROM public.migration_log WHERE name='20261003_final_entrypoint_storage'"
        )
        db.scalar(
            "UPDATE public.migration_log SET summary=jsonb_set(summary,'{artifact_checksum}','\"wrong\"') WHERE name='20261003_final_entrypoint_storage'"
        )
        try:
            apply(db, contract)
        except ExecutionError as error:
            assert "DATA_MIGRATION_REQUIRED" in str(error)
        else:
            raise AssertionError("Contract accepted wrong checksum")
        assert snapshot(db, "sync_runs") == original_runs
        db.scalar(
            "UPDATE public.migration_log SET summary=:'saved'::jsonb WHERE name='20261003_final_entrypoint_storage'",
            variables={"saved": saved},
        )
        db.scalar("GRANT SELECT(config) ON public.connections TO authenticated")
    apply(db, contract)
    runner.run(MIGRATION)
    runner.verify(MIGRATION)
    runs = snapshot(db, "synchronize_runs")
    assert runs == [
        {
            ("synchronize_binding_id" if k == "connection_id" else k): v
            for k, v in row.items()
        }
        for row in original_runs
    ]
    github = snapshot(db, "synchronize_github_bindings")
    mapping = {
        "auto_import": "auto_pull",
        "last_imported_sha": "last_pulled_sha",
        "last_imported_at": "last_pulled_at",
        "last_exported_sha": "last_pushed_sha",
        "last_exported_at": "last_pushed_at",
    }
    assert github == [
        {mapping.get(k, k): v for k, v in row.items()} for row in original_github
    ]
    logs = snapshot(db, "synchronize_github_logs")
    assert logs == [
        {
            ("synchronize_github_binding_id" if k == "binding_id" else k): (
                {"import": "inbound", "export": "outbound"}[v]
                if k == "direction"
                else v
            )
            for k, v in row.items()
        }
        for row in original_logs
    ]
    assert snapshot(db, "import_database_sources") == original_sources
    assert (
        db.scalar(
            "SELECT has_column_privilege('authenticated','public.synchronize_bindings','config','SELECT')"
        )
        == "f"
    )
    if populated:
        assert {b["id"] for b in snapshot(db, "synchronize_bindings")} == {
            "binding",
            "dual",
            "legacy",
            "history-only",
        }
        binding_key_mapping = {
            "last_sync_run_id": "last_synchronize_run_id",
            "last_sync_commit_id": "last_synchronize_commit_id",
        }
        for before in original_bindings:
            if before["id"] == "source":
                continue
            after = next(
                b
                for b in snapshot(db, "synchronize_bindings")
                if b["id"] == before["id"]
            )
            for key, value in before.items():
                if key not in {
                    "config",
                    "legacy_read_only_reason",
                    "status",
                    "error_message",
                }:
                    assert after[binding_key_mapping.get(key, key)] == value, key
            if before["id"] == "history-only":
                assert (
                    after["config"] == before["config"]
                    and after["status"] == "disabled"
                )
                assert not any(
                    s["synchronize_binding_id"] == "history-only"
                    for s in original_sources
                )
        assert (
            db.scalar(
                "SELECT status FROM public.synchronize_bindings WHERE id='legacy'"
            )
            == "disabled"
        )
        assert (
            db.scalar(
                "SELECT legacy_read_only_reason IS NOT NULL FROM public.synchronize_bindings WHERE id='legacy'"
            )
            == "t"
        )
        assert (
            db.scalar(
                "SELECT count(*) FROM public.context_activity_items WHERE kind='synchronize_run'"
            )
            == "3"
        )
        expect_error(
            db,
            "UPDATE public.synchronize_runs SET project_id='issue049-other-project' WHERE id='run-dual'",
            "foreign key constraint",
        )
        expect_error(
            db,
            "UPDATE public.synchronize_bindings SET last_synchronize_run_id='run-dual' WHERE id='binding'",
            "foreign key constraint",
        )
        expect_error(
            db,
            "UPDATE public.import_database_sources SET project_id='issue049-other-project' WHERE id='source'",
            "crosses Project or Organization boundary",
        )
        expect_error(
            db,
            "UPDATE public.synchronize_bindings SET status='active' WHERE id='legacy'",
            "synchronize_bindings_read_only_status",
        )
        expect_error(
            db,
            "INSERT INTO public.connections(id) VALUES ('old-writer')",
            "does not exist",
        )
        # New-schema writes and ordinary deletes remain compatible with replay;
        # migration checks do not freeze normal application timestamps forever.
        db.scalar(
            "SET ROLE service_role; UPDATE public.import_database_sources SET last_used_at=now() WHERE id='source'"
        )
        runner.verify(MIGRATION)
        assert (
            json.loads(db.scalar("SELECT public.repository_target_integrity_report()"))[
                "orphan_scope_dependents"
            ]
            == 0
        )
    print(
        "PASS final Contract: guarded receipt, exact runs/GitHub/config, canonical Activity, composite tenant FKs, grants and post-contract retry",
        flush=True,
    )


def restore_accepted_writes(db, root):
    # Post-Contract recovery must retain NEW accepted data, not restore an old
    # baseline and claim that losing those writes is a successful rollback.
    db.scalar("""SET ROLE service_role;
UPDATE public.import_database_sources SET name='accepted-after-contract' WHERE id='source';
INSERT INTO public.synchronize_bindings(id,org_id,project_id,provider,name,direction,target_path)
 VALUES ('new-binding','issue049-org','issue049-project','url','Accepted after Contract','inbound','new-target');
INSERT INTO public.synchronize_runs(id,synchronize_binding_id,project_id,triggered_by,direction,status,result)
 VALUES ('new-run','new-binding','issue049-project','manual','inbound','completed','{"connection_id":"opaque-user-metadata"}');
""")
    tables = (
        "synchronize_bindings",
        "synchronize_runs",
        "synchronize_github_bindings",
        "synchronize_github_logs",
        "import_database_sources",
        "access_tools",
        "search_index_tasks",
    )

    def recovered_facts(client):
        return {
            **{table: snapshot(client, table) for table in tables},
            "migration_log": json.loads(
                client.scalar(
                    "SELECT jsonb_agg(to_jsonb(m) ORDER BY name) FROM public.migration_log m"
                )
            ),
        }

    expected = recovered_facts(db)
    binaries = Path(db.executable).parent
    backup = root / "accepted-post-contract.dump"
    subprocess.run(
        [str(binaries / "pg_dump"), "-Fc", "-f", str(backup)],
        env=db.environment,
        check=True,
        capture_output=True,
        timeout=120,
    )
    db.scalar("CREATE DATABASE entrypoint_recovered WITH TEMPLATE template0")
    recovered = PsqlClient(
        f"postgresql://postgres@127.0.0.1:{db.environment['PGPORT']}/entrypoint_recovered",
        executable=db.executable,
        base_environment={"PATH": os.environ["PATH"]},
    )
    subprocess.run(
        [
            str(binaries / "pg_restore"),
            "--clean",
            "--if-exists",
            "--exit-on-error",
            "-d",
            "entrypoint_recovered",
            str(backup),
        ],
        env=recovered.environment,
        check=True,
        capture_output=True,
        timeout=120,
    )
    assert recovered_facts(recovered) == expected
    DataMigrationRunner(DataMigrationCatalog(ROOT), recovered, environment={}).verify(
        MIGRATION
    )
    recovered.scalar(
        "SET ROLE service_role; UPDATE public.import_database_sources SET last_used_at=now() WHERE id='source'"
    )
    assert (
        json.loads(
            recovered.scalar("SELECT public.repository_target_integrity_report()")
        )["orphan_scope_dependents"]
        == 0
    )
    print(
        "PASS isolated post-Contract pg_dump/restore retains accepted new writes, history, opaque metadata and receipts; resumed final-schema write succeeds",
        flush=True,
    )


def main():
    with owned_native() as (db, _):
        expanded(db)
        runner = DataMigrationRunner(DataMigrationCatalog(ROOT), db, environment={})
        runner.run(MIGRATION)
        runner.verify(MIGRATION)
        runner.run(MIGRATION)
        assert snapshot(db, "import_database_sources") == []
        print(
            "PASS fresh Expand/data and idempotent replay (native PostgreSQL, not installer)",
            flush=True,
        )
        rehearse_contract(db, populated=False)
    with owned_native() as (db, root):
        apply(db, FIXTURE)
        expanded(db)
        source_fixture(db)
        DataMigrationRunner(DataMigrationCatalog(ROOT), db, environment={}).run(
            "20260927_entrypoint_storage_backfill"
        )
        rehearse_expand(db)
        rehearse_contract(db, populated=True)
        restore_accepted_writes(db, root)


if __name__ == "__main__":
    main()
