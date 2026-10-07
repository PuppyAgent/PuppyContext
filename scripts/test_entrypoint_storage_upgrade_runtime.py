#!/usr/bin/env python3
"""Owned local populated Release A -> B rehearsal with real Auth/API/CLI/workers.

Reuses the local-runtime harness, not substitutes for its dependencies. Historical
rows are explicit SQL fixtures; Database Import queries real local PostgREST.
No hosted target, real GitHub OAuth/webhook delivery, or installed Desktop release
is certified. Backups and credentials remain in a private artifact directory.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit

import httpx
from test_entrypoint_local_runtime import ROOT, SOURCE, Stack

sys.path.insert(0, str(ROOT / "backend"))
from entrypoint_source_decisions import INVENTORY, approve, freeze, reviewed_rows
from src.infra.data_migrations.catalog import DataMigrationCatalog
from src.infra.data_migrations.database import PsqlClient
from src.infra.data_migrations.runner import DataMigrationRunner

MIGRATION = "20261003_final_entrypoint_storage"
RELEASE_A = "1d20cf1820df46551e5970e1cfdd9820091081e8"
ROLES = ("api", "mcp", "import", "synchronize", "upload")


@contextmanager
def local_database_route(stack):
    """Route one fixture hostname to real local Kong; fabricate no DB responses.

    The released Provider admits *.supabase.co URLs only. An owned HTTP routing
    proxy avoids changing that policy or mutating system DNS for this rehearsal.
    No other hostname is forwarded and no external credential/host is used.
    """
    fixture_host = "entrypoint-upgrade.supabase.co"

    class Route(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            target = urlsplit(self.path)
            if (
                target.scheme != "http"
                or target.netloc != fixture_host
                or not target.path.startswith("/rest/v1/")
            ):
                self.send_error(403)
                return
            path = target.path + ("?" + target.query if target.query else "")
            headers = {
                key: self.headers[key]
                for key in ("apikey", "Authorization", "Accept")
                if key in self.headers
            }
            with httpx.Client(trust_env=False, timeout=30) as client:
                response = client.get(stack.auth + path, headers=headers)
            self.send_response(response.status_code)
            self.send_header(
                "Content-Type", response.headers.get("content-type", "application/json")
            )
            self.send_header("Content-Length", str(len(response.content)))
            if "content-range" in response.headers:
                self.send_header("Content-Range", response.headers["content-range"])
            self.end_headers()
            self.wfile.write(response.content)

    server = ThreadingHTTPServer(("127.0.0.1", stack.port + 94), Route)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    stack.env["HTTP_PROXY"] = f"http://127.0.0.1:{stack.port + 94}"
    try:
        yield "http://" + fixture_host
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def facts(db, table):
    # Call sites supply owned constant identifiers, never HTTP input.
    return json.loads(
        db.scalar(
            f"SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY id),'[]') FROM public.{table} t"
        )
    )


def rejected(stack, method, path, expected, **kwargs):
    response = stack.http.request(
        method,
        stack.base + "/api/v1" + path,
        headers={
            "Authorization": "Bearer " + stack.token,
            "X-PuppyOne-Repository-Contract": "2",
        },
        **kwargs,
    )
    assert response.status_code == expected, (method, path, response.status_code)


def stopped(stack):
    records = [stack.stop(role) for role in ROLES]
    assert all(process.poll() is not None for process in stack.processes.values())
    try:
        stack.http.get(stack.base + "/live", timeout=1)
    except httpx.ConnectError:
        pass
    else:
        raise AssertionError("Old producer still accepts requests")
    return records


def migrate(stack, name, *, expected=0):
    env = {
        **stack.env,
        "PATH": str(stack.supabase.parent) + ":" + stack.env["PATH"],
        "POSTGRES_PASSWORD": stack.values["POSTGRES_PASSWORD"],
        "PGHOST": "127.0.0.1",
        "PGPORT": str(stack.port + 32),
    }
    return stack.run(
        name,
        stack.python,
        str(ROOT / "scripts/self_hosted_migrate.py"),
        env=env,
        expected=expected,
    )


def exercise(stack, database_url, desktop_source):
    assert stack.git("rev-parse", "HEAD") == RELEASE_A
    assert not stack.git("diff", "HEAD", "--"), (
        "Release A tracked source must match its commit"
    )
    stack.receipt["release_a"] = {"sha": RELEASE_A, "tracked_source_clean": True}
    stack.receipt["limits"] = [
        "isolated generated local infrastructure and accounts only; not hosted consumer-exit evidence",
        "historical binding/run/GitHub records are SQL fixtures, not real GitHub OAuth/webhook delivery",
        "real local Database Provider, URL Provider, Auth, CLI/API, PostgreSQL, Redis, MinIO and workers",
        "fixture *.supabase.co hostname routed through an owned loopback HTTP proxy to real local PostgREST; no external database",
        "public control-plane objects/data/ACLs restored into retained real Supabase Auth/platform; not Auth or platform disaster recovery",
        "database backup/restore plus retained object storage; not an independent object-store disaster restore",
    ]
    stack.setup()
    binary = Path(shutil.which("psql") or "/opt/homebrew/opt/postgresql@17/bin/psql")
    db = PsqlClient(
        f"postgresql://postgres:{stack.values['POSTGRES_PASSWORD']}@127.0.0.1:{stack.port + 32}/postgres",
        executable=str(binary),
        base_environment=stack.env,
    )
    stack.token = stack.signup("upgrade-owner")
    org = stack.api(
        "POST", "/organizations/", expected=201, json={"name": "Owned upgrade"}
    )
    project = stack.api(
        "POST",
        "/projects/",
        expected=201,
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"name": "Populated upgrade", "org_id": org["id"]},
    )
    stack.project = project["id"]
    created = stack.cli(
        "synchronize", "add", "url", SOURCE, "--folder", "before-upgrade"
    )
    binding_id = created["binding"]["id"]
    run = stack.wait(
        "/synchronize/runs/" + created["execution_result"]["synchronize_run_id"],
        {"success"},
    )
    access = stack.cli("access", "add", "mcp", "Surviving access")["access"]
    source_body = {
        "name": "Reusable local database",
        "provider": "supabase",
        "project_url": database_url,
        "api_key": stack.values["SERVICE_ROLE_KEY"],
        "key_type": "service_role",
    }
    source_id = stack.api(
        "POST",
        "/imports/database/sources",
        expected=201,
        params={"project_id": stack.project},
        json=source_body,
    )["source"]["id"]
    db.scalar(
        """
CREATE TABLE public.entrypoint_upgrade_rows(id integer PRIMARY KEY, payload jsonb NOT NULL);
ALTER TABLE public.entrypoint_upgrade_rows ENABLE ROW LEVEL SECURITY;
GRANT SELECT ON public.entrypoint_upgrade_rows TO service_role;
INSERT INTO public.entrypoint_upgrade_rows VALUES (1,'{"connection_id":"opaque-user-data","value":"survives"}');
UPDATE public.connections b SET config=b.config || jsonb_build_object(
 'db_provider','supabase','db_config',(SELECT config->'db_config' FROM public.connections WHERE id=:'source'),
 'options',coalesce(b.config->'options','{}') || jsonb_build_object(
   'fixture_metadata',jsonb_build_object('connection_id','opaque-binding-metadata'))) WHERE id=:'binding';
INSERT INTO public.connections(id,org_id,project_id,provider,name,direction,config,target_path,created_by)
SELECT 'retained-history',org_id,project_id,'database','Reviewed historical binding','inbound',
 '{"historical":{"connection_id":"opaque-private-history"}}','historical',created_by
FROM public.connections WHERE id=:'binding';
INSERT INTO public.sync_runs(id,connection_id,project_id,triggered_by,direction,status,result)
VALUES ('retained-run','retained-history',:'project','manual','inbound','failed','{"error":"original history: connection_id / sync_run"}');
UPDATE public.connections SET last_sync_run_id='retained-run' WHERE id='retained-history';
INSERT INTO public.github_sync_bindings(id,project_id,github_repo_owner,github_repo_name,webhook_secret,
 last_imported_sha,last_imported_at,last_exported_sha,last_exported_at)
VALUES ('github-history',:'project','fixture-owner','fixture-repo','private-fixture-webhook',
 'external-pull',now(),'external-push',now());
INSERT INTO public.github_sync_log(id,integration_id,direction,status,git_sha,version_commit_id)
VALUES ('github-in','github-history','import','success','external-pull','internal-pull'),
       ('github-out','github-history','export','success','external-push','internal-push');
NOTIFY pgrst,'reload schema';
""",
        variables={
            "source": source_id,
            "binding": binding_id,
            "project": stack.project,
        },
    )
    original = {
        table: facts(db, table)
        for table in (
            "connections",
            "sync_runs",
            "github_sync_bindings",
            "github_sync_log",
        )
    }
    source_row = next(row for row in original["connections"] if row["id"] == source_id)
    assert source_row["config"]["db_config"]["_enc"]["alg"] == "AES-256-GCM"
    assert source_body["api_key"] not in json.dumps(source_row["config"])
    stack.check(
        "Release A real client writes plus explicit mixed/history fixtures",
        binding_id=binding_id,
        source_id=source_id,
        run_id=run["id"],
        access_surface_id=access["id"],
    )
    time.sleep(1)
    drain = stack.scan("pre-upgrade-drain", 0)
    stops = stopped(stack)
    backup = stack.directory / "before-upgrade.dump"
    stack.run(
        "backup-before-upgrade",
        str(binary.with_name("pg_dump")),
        "-Fc",
        "-f",
        str(backup),
        env=db.environment,
    )
    backup.chmod(0o600)
    stack.use_source(ROOT)
    stack.receipt["release_b"] = {
        "sha": stack.git("rev-parse", "HEAD"),
        "source_dirty": bool(stack.git("status", "--porcelain")),
    }
    # The actual installer, not a duplicated preflight, must reject unreviewed populated data.
    migrate(stack, "unreviewed-installer-blocked", expected=1)
    assert db.scalar("SELECT to_regclass('public.synchronize_bindings') IS NULL") == "t"
    assert facts(db, "connections") == original["connections"]
    assert db.receipt(MIGRATION) is None
    stack.check(
        "populated installer rejects missing reviewed cutover without deleting old rows"
    )
    # The preceding job is verified before freezing its Access/search writers.
    runner = DataMigrationRunner(DataMigrationCatalog(ROOT), db, environment=stack.env)
    runner.run("20260927_entrypoint_storage_backfill")
    inventory = json.loads(db.scalar(INVENTORY))
    choices = {
        source_id: ("import", source_id, None),
        binding_id: ("both", "explicit-dual-source", None),
        "retained-history": (
            "synchronize",
            None,
            "Reviewed history-only fixture; no executable source",
        ),
    }
    assert {row["legacy_id"] for row in inventory} == choices.keys()
    for row in inventory:
        disposition, import_id, reason = choices[row["legacy_id"]]
        row.update(
            disposition=disposition,
            import_database_source_id=import_id,
            binding_read_only_reason=reason,
            approved_by="owned-local-rehearsal",
            evidence_ref="explicit-fixture-definitions-and-real-client-writes",
        )
    approve(db, reviewed_rows({"format_version": 1, "rows": inventory}))
    freeze(
        db,
        json.dumps(
            {
                "environment": "owned-local-stack",
                "producer_stop_verified": True,
                "queue_drain_verified": True,
                "old_consumers_exited": True,
                "records": [
                    {
                        "processes": stops,
                        "queue_scan": drain,
                        "webhook": "fixture auto_import=false; no external callback configured",
                    }
                ],
            }
        ).encode(),
        restore_point_ref=str(backup),
        approved_by="owned-local-rehearsal",
    )
    runner.run(MIGRATION)
    runner.run(MIGRATION)
    assert facts(db, "connections") == original["connections"]
    assert facts(db, "sync_runs") == original["sync_runs"]
    migrate(stack, "reviewed-installer-contract")
    migrate(stack, "reviewed-installer-replay")
    sources = {row["id"]: row for row in facts(db, "import_database_sources")}
    assert set(sources) == {source_id, "explicit-dual-source"}
    for key in (
        "id",
        "project_id",
        "org_id",
        "created_by",
        "config",
        "created_at",
        "updated_at",
    ):
        assert sources[source_id][key] == source_row[key], key
    final_runs = facts(db, "synchronize_runs")
    for old, current in zip(original["sync_runs"], final_runs, strict=True):
        expected = {**old, "synchronize_binding_id": old["connection_id"]}
        del expected["connection_id"]
        assert current == expected
    stack.check(
        "reviewed portable data job + gated installer Contract/replay preserve exact ciphertext and history"
    )
    for role in ROLES:
        stack.start(role)
    stack.ready()
    bindings = stack.api(
        "GET", "/synchronize/bindings", params={"project_id": stack.project}
    )
    assert {row["id"] for row in bindings} == {binding_id, "retained-history"}
    assert "opaque-private-history" not in json.dumps(bindings)
    assert "db_config" not in json.dumps(bindings)
    listed = stack.api(
        "GET", "/imports/database/sources", params={"project_id": stack.project}
    )
    assert {row["id"] for row in listed} == sources.keys()
    assert source_body["api_key"] not in json.dumps(listed)
    for action in ("pause", "resume", "refresh"):
        rejected(stack, "POST", "/synchronize/bindings/retained-history/" + action, 409)
    rejected(stack, "DELETE", "/synchronize/bindings/retained-history", 409)
    rejected(
        stack,
        "PATCH",
        "/synchronize/bindings/retained-history",
        409,
        json={"target_path": "changed"},
    )
    assert (
        stack.api("GET", "/synchronize/bindings/retained-history/runs")[0]["id"]
        == "retained-run"
    )
    for old in (
        "/integrations/status",
        "/db-connector/access",
        "/access/",
        f"/projects/{stack.project}/connectors",
        f"/projects/{stack.project}/github/status",
        f"/projects/{stack.project}/dashboard",
        "/activity",
    ):
        rejected(stack, "GET", old, 404)
    github = stack.api("GET", f"/projects/{stack.project}/synchronize/github/binding")
    assert (
        github["last_pulled_sha"] == "external-pull"
        and github["last_pushed_sha"] == "external-push"
    )
    logs = stack.api("GET", f"/projects/{stack.project}/synchronize/github/logs")
    assert {row["direction"] for row in logs["entries"]} == {"inbound", "outbound"}
    assert {row["version_commit_id"] for row in logs["entries"]} == {
        "internal-pull",
        "internal-push",
    }
    dashboard = stack.cli("status")["dashboard"]
    assert {
        row["resource_id"]
        for row in dashboard["resources"]
        if row["resource_kind"] == "synchronize"
    } == {binding_id, "retained-history"}
    assert any(
        row["resource_kind"] == "access" and row["resource_id"] == access["id"]
        for row in dashboard["resources"]
    )
    stranger = stack.signup("upgrade-stranger")
    denied = stack.http.get(
        stack.base + "/api/v1/imports/database/sources/" + source_id,
        headers={"Authorization": "Bearer " + stranger},
    )
    assert denied.status_code in {403, 404}
    stack.check(
        "real final consumers: separated inventories, private read-only history, GitHub mapping, CLI Dashboard, authorization and retired URLs"
    )
    client_env = {
        **stack.env,
        "ENTRYPOINT_CLIENT_TOKEN": stack.token,
        "ENTRYPOINT_CLIENT_CONTEXT": json.dumps(
            {
                "origin": stack.base,
                "project": stack.project,
                "binding": binding_id,
                "source": source_id,
                "access": access["id"],
                "user": source_row["created_by"],
                "cloudRoot": str(ROOT),
                "desktopRoot": str(desktop_source.resolve()),
            }
        ),
    }
    stack.run(
        "actual-resource-clients",
        "node",
        str(ROOT / "scripts/test_entrypoint_storage_clients.mjs"),
        env=client_env,
    )
    stack.check(
        "actual shared and Desktop client libraries read/write migrated storage over real authenticated HTTP (not a window test)"
    )
    preview = stack.api(
        "GET",
        f"/imports/database/sources/{source_id}/tables/entrypoint_upgrade_rows/preview",
    )
    assert preview["rows"] == [
        {"id": 1, "payload": {"connection_id": "opaque-user-data", "value": "survives"}}
    ]
    imports_before = db.scalar("SELECT count(*) FROM public.import_jobs")
    saved = stack.api(
        "POST",
        f"/imports/database/sources/{source_id}/save",
        params={"project_id": stack.project},
        json={"name": "database-after-upgrade", "table": "entrypoint_upgrade_rows"},
    )
    assert saved["import_database_source_id"] == source_id and saved["row_count"] == 1
    assert db.scalar("SELECT count(*) FROM public.import_jobs") == imports_before
    created_source = stack.api(
        "POST",
        "/imports/database/sources",
        expected=201,
        params={"project_id": stack.project},
        json={**source_body, "name": "New final-schema source"},
    )["source"]["id"]
    refresh = stack.cli("synchronize", "refresh", binding_id)["result"]["results"][0]
    stack.wait(
        "/synchronize/runs/" + refresh["synchronize_run_id"], {"success", "skipped"}
    )
    assert refresh["synchronize_run_id"] != run["id"]
    runner.run(MIGRATION)
    stack.check(
        "migrated encrypted source queries real Provider; synchronous save + new source + durable binding retry + live verifier replay"
    )
    # Restore the actual application database after accepting final-schema writes.
    stack.scan("post-upgrade-drain", 0)
    recovery_stops = stopped(stack)
    stack.run(
        "stop-db-consumers",
        *stack.compose,
        "stop",
        "rest",
        "auth",
        "kong",
        env=dict(os.environ),
    )
    backup = stack.directory / "accepted-final-writes.dump"
    tables = (
        "import_database_sources",
        "synchronize_bindings",
        "synchronize_runs",
        "synchronize_github_bindings",
        "synchronize_github_logs",
        "access_surfaces",
    )
    accepted = {table: facts(db, table) for table in tables}
    stack.run(
        "backup-final-writes",
        str(binary.with_name("pg_dump")),
        "-Fc",
        "-f",
        str(backup),
        env=db.environment,
    )
    backup.chmod(0o600)
    db.scalar(
        "UPDATE public.import_database_sources SET name='injected-recovery-fault' WHERE id=:'id'",
        variables={"id": created_source},
    )
    # Supabase platform extensions/namespaces are initialized separately from
    # application DDL. Recreating platform objects from CREATE EXTENSION alone
    # loses platform-installed wrappers/initial grants. This recovery scope is
    # the complete public control plane, retaining real Auth and platform data.
    # Include Auth's application triggers, whose functions live in public.
    # The archive itself is full; the selected recovery scope is explicit.
    # Never skip a failing grant or use --no-owner/--no-acl to make restore pass.
    platform_query = """SELECT jsonb_build_object(
      'extensions',(SELECT jsonb_agg(jsonb_build_array(extname,extversion) ORDER BY extname) FROM pg_extension),
      'schemas',(SELECT jsonb_agg(jsonb_build_array(nspname,pg_get_userbyid(nspowner)) ORDER BY nspname)
        FROM pg_namespace WHERE nspname !~ '^pg_(temp|toast_temp)_'))"""
    platform_before = db.scalar(platform_query)
    catalog = stack.run(
        "restore-catalog", str(binary.with_name("pg_restore")), "--list", str(backup)
    )
    retained = []
    selected = []
    for line in catalog.splitlines():
        if not line or line.startswith(";"):
            selected.append(line)
            continue
        entry = re.fullmatch(r"\d+; \d+ \d+ ([A-Z][A-Z ]*) (\S+) (.*)", line)
        assert entry, "Unrecognized restore catalog entry; do not guess ownership"
        kind, namespace, description = entry.groups()
        application = kind not in {"SCHEMA", "EXTENSION"} and (
            namespace in {"public", "supabase_migrations"}
            or namespace == "auth"
            and (
                kind == "TRIGGER"
                or kind == "COMMENT"
                and description.startswith("TRIGGER ")
            )
        )
        if application:
            selected.append(line)
        else:
            retained.append(line)
            selected.append("; initialized platform retained: " + line)
    assert retained and any(" EXTENSION " in line for line in retained)
    stack.private("restore-platform-retained.json", json.dumps(retained, indent=2))
    stack.private("restore-objects.list", "\n".join(selected) + "\n")
    restore_list = f"/tmp/entrypoint-restore-{uuid.uuid4().hex}.list"
    stack.run(
        "restore-list-copy",
        "docker",
        "cp",
        str(stack.directory / "restore-objects.list"),
        stack.project_name + "-db-1:" + restore_list,
    )
    # A full Supabase restore includes platform-owned event triggers. The normal
    # migration login is deliberately not their owner. Create each object as its
    # original owner (--use-set-session-authorization), so Supabase administrator
    # default grants cannot leak into new application tables before ALTER OWNER.
    # Use the administrator of
    # this generated container through its local socket, not relaxed ACLs or a
    # --no-owner/--no-acl restore. Stream the private archive without copying it
    # to a shared container path or putting any password in argv.
    # pg_dump replays DEFAULT ACLs after object creation and assumes clean
    # defaults. Existing Supabase defaults otherwise grant anon/authenticated
    # access that an object's dump does not explicitly revoke. Clear only
    # future-object default grants while offline; the archive restores them.
    prepare = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            "-e",
            "PGPASSWORD",
            stack.project_name + "-db-1",
            "psql",
            "--username",
            "supabase_admin",
            "--no-password",
            "--dbname",
            "postgres",
            "--set",
            "ON_ERROR_STOP=1",
        ],
        env={**os.environ, "PGPASSWORD": stack.values["POSTGRES_PASSWORD"]},
        input="""DO $$ DECLARE d record; privileges text; BEGIN
          FOR d IN SELECT pg_get_userbyid(a.defaclrole) AS owner,
            n.nspname AS schema, a.defaclobjtype AS kind, x.grantee
            FROM pg_default_acl a LEFT JOIN pg_namespace n ON n.oid=a.defaclnamespace
            CROSS JOIN LATERAL aclexplode(a.defaclacl) x
            WHERE x.grantee<>a.defaclrole AND n.nspname IN ('public','supabase_migrations')
            GROUP BY a.defaclrole,n.nspname,a.defaclobjtype,x.grantee LOOP
            privileges := CASE d.kind WHEN 'r' THEN 'TABLES' WHEN 'S' THEN 'SEQUENCES'
              WHEN 'f' THEN 'FUNCTIONS' WHEN 'T' THEN 'TYPES' WHEN 'n' THEN 'SCHEMAS' END;
            IF privileges IS NULL THEN RAISE EXCEPTION 'Unsupported default ACL object kind'; END IF;
            EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I %s REVOKE ALL ON %s FROM %s',
              d.owner,CASE WHEN d.schema IS NULL THEN '' ELSE format('IN SCHEMA %I',d.schema) END,
              privileges,CASE WHEN d.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(d.grantee)) END);
          END LOOP;
        END $$;""",
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    stack.private(
        "restore-default-acl-preparation.log", prepare.stdout + prepare.stderr
    )
    assert prepare.returncode == 0, (
        "Default ACL preparation failed; keep consumers stopped"
    )
    with backup.open("rb") as archive:
        restored = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                "-e",
                "PGPASSWORD",
                stack.project_name + "-db-1",
                "pg_restore",
                "--username",
                "supabase_admin",
                "--no-password",
                "--use-set-session-authorization",
                "--clean",
                "--if-exists",
                "--exit-on-error",
                "--dbname",
                "postgres",
                "--use-list",
                restore_list,
            ],
            stdin=archive,
            env={**os.environ, "PGPASSWORD": stack.values["POSTGRES_PASSWORD"]},
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    stack.private("restore-final-writes.log", restored.stdout + restored.stderr)
    assert restored.returncode == 0, (
        "Full restore failed; inspect the private restore log"
    )
    assert db.scalar(platform_query) == platform_before
    assert {table: facts(db, table) for table in tables} == accepted
    runner.verify(MIGRATION)
    stack.run(
        "resume-db-consumers",
        *stack.compose,
        "start",
        "auth",
        "rest",
        "kong",
        env=dict(os.environ),
    )
    stack.run(
        "restart-object-queue",
        "docker",
        "restart",
        stack.project_name + "-minio-1",
        stack.project_name + "-redis-1",
    )
    time.sleep(4)
    for role in ROLES:
        stack.start(role)
    stack.ready()
    assert (
        stack.api("GET", "/imports/database/sources/" + created_source)["name"]
        == "New final-schema source"
    )
    content = stack.api(
        "GET", f"/content/{stack.project}/cat", params={"path": saved["content_path"]}
    )["content_text"]
    assert json.loads(content)["rows"] == preview["rows"]
    assert (
        stack.api("GET", "/synchronize/runs/" + refresh["synchronize_run_id"])[
            "synchronize_binding_id"
        ]
        == binding_id
    )
    stack.api("DELETE", "/imports/database/sources/" + created_source)
    assert {
        row["id"]
        for row in stack.api(
            "GET", "/synchronize/bindings", params={"project_id": stack.project}
        )
    } == {binding_id, "retained-history"}
    stack.check(
        "actual public control-plane dump/restore + real Auth/process/object/queue restart retains accepted final writes and Git bytes; source-only delete resumes",
        stops=recovery_stops,
    )
    stack.check(
        "final readiness and queue drain",
        readiness=stack.ready(),
        scan=stack.scan("final-drain", 0),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--release-a-source", type=Path, required=True)
    parser.add_argument("--desktop-source", type=Path, required=True)
    parser.add_argument("--supabase-bin", type=Path, required=True)
    parser.add_argument("--base-port", type=int, default=33000)
    args = parser.parse_args()
    stack = Stack(
        args.artifacts,
        args.base_port,
        args.supabase_bin,
        source_root=args.release_a_source,
    )
    with local_database_route(stack) as database_url:
        try:
            exercise(stack, database_url, args.desktop_source)
        finally:
            stack.cleanup()


if __name__ == "__main__":
    main()
