#!/usr/bin/env python3
"""ISSUE-053: fresh/upgrade rehearsal in an owned Docker Supabase only.

Run with the locked backend Python and Supabase CLI 2.107.0 on PATH. No DSN,
linked project, production .env, or shared database is accepted. Ports 26390-94
are below the usual ephemeral client-port range to avoid TIME_WAIT collisions;
--port-base selects another isolated range.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
import jwt

from database_baseline import ROOT, LocalStack, run

FIX = ROOT / "supabase/migrations/20261003000000_contain_internal_data_api_access.sql"
# Supabase's pg_prove container mounts tests/, not sibling test_fixtures/.
FIXTURE = ROOT / "supabase/tests/_support/data_api_containment_fixture.inc"
CONTRACT = ROOT / "supabase/tests/_support/data_api_containment.inc"
TABLES = ("audit_logs", "bookmarks", "connector_runs", "scope_sync_events", "scope_sync_settings", "tables")
USERS = ("00000000-0000-4000-8000-000000000053", "00000000-0000-4000-8000-000000000054")
EXCLUDE = "studio,imgproxy,mailpit,edge-runtime,logflare,vector,supavisor,realtime,storage-api"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def snapshot(stack: LocalStack) -> str:
    parts = ",".join(
        f"'{name}', (SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),'[]') FROM public.{name} t)"
        for name in (*TABLES, "version_transactions", "version_conflicts", "projects", "project_members")
    )
    return stack.sql(f"SELECT jsonb_build_object({parts})")


def expect_guard_failure(stack: LocalStack, sql: str) -> None:
    result = subprocess.run(
        ["docker", "exec", "-i", stack.container, "psql", "-U", "postgres", "-d", "postgres",
         "-X", "-qAt", "-v", "ON_ERROR_STOP=1"],
        input=sql, text=True, capture_output=True, timeout=60,
    )
    check(result.returncode != 0 and re.search(r"(?:^|\n)ERROR:\s+ISSUE-053:", result.stderr),
          "Expected security rejection, not success, syntax error or infrastructure failure: " + result.stderr)


def rest_matrix(status: dict, *, before: bool) -> int:
    url = status["API_URL"]
    check(urlparse(url).hostname in {"127.0.0.1", "localhost"}, "Nonlocal API refused")
    tokens = [status["ANON_KEY"]] + [
        jwt.encode({"sub": user, "role": "authenticated", "aud": "authenticated", "exp": int(time.time()) + 3600},
                   status["JWT_SECRET"], algorithm="HS256") for user in USERS
    ]
    count = 0
    with httpx.Client(base_url=url + "/rest/v1/", trust_env=False, timeout=20) as client:
        for token in tokens:
            headers = {"apikey": status["ANON_KEY"], "Authorization": "Bearer " + token}
            for table in (*TABLES, "version_activity_feed"):
                result = client.get(table, headers=headers, params={"select": "*"})
                if before:
                    check(result.status_code == 200 and len(result.json()) >= 2,
                          f"Baseline leak not reproduced: {table}, HTTP {result.status_code}")
                else:
                    check(result.status_code in (401, 403) and result.json().get("code") == "42501",
                          f"Read denial is not a PostgreSQL permission failure: {table}, {result.status_code}")
                count += 1
            if before:
                continue
            for table in TABLES:
                column = "connector_id" if table == "connector_runs" else "project_id"
                for method, body in (("POST", {}), ("PATCH", {column: "issue053-project-a"}), ("DELETE", None)):
                    # Supabase's pg_safeupdate rejects unfiltered PATCH/DELETE
                    # before ACL checks. Use a valid tenant filter, never accept
                    # that query-safety 400 as proof of authorization denial.
                    value = "issue053-legacy-a" if table == "connector_runs" else "issue053-project-a"
                    params = {column: "eq." + value} if method != "POST" else None
                    result = client.request(method, table, headers=headers, json=body, params=params)
                    code = result.json().get("code")
                    check(result.status_code in (401, 403) and code == "42501",
                          f"Write denial is not a PostgreSQL permission failure: {method} {table}, {result.status_code}, {code}")
                    count += 1
            for name, body in (
                ("repository_target_integrity_report", {}),
                ("join_project_via_share_token", {"p_share_token": "issue053-share-b", "p_user_id": USERS[0]}),
            ):
                result = client.post("rpc/" + name, headers=headers, json=body)
                check(result.status_code in (401, 403) and result.json().get("code") == "42501",
                      f"RPC denial is not a PostgreSQL permission failure: {name}, {result.status_code}")
                count += 1
        headers = {"apikey": status["SERVICE_ROLE_KEY"], "Authorization": "Bearer " + status["SERVICE_ROLE_KEY"]}
        for table in (*TABLES, "version_activity_feed"):
            result = client.get(table, headers=headers, params={"select": "*"})
            check(result.status_code == 200 and len(result.json()) >= 2, "Backend read failed: " + table)
            count += 1
    return count


def backend_consumers(status: dict) -> None:
    # Actual repositories over PostgREST plus the canonical authorization policy
    # over real tenant facts. No in-memory database/authentication substitute.
    os.environ.update(SUPABASE_URL=status["API_URL"], SUPABASE_KEY=status["SERVICE_ROLE_KEY"], SKIP_AUTH="false")
    # Bootstrap the normal facade before domain repositories: its existing
    # re-exports otherwise form an import cycle in a fresh standalone process.
    from src.infra.supabase import SupabaseClient, TableRepository
    from src.content.table.supabase_schemas import TableCreate, TableUpdate
    from src.exceptions import NotFoundException
    from src.platform.authorization.models import ProjectAction
    from src.platform.authorization.repository import AuthorizationRepository
    from src.platform.authorization.service import AuthorizationService
    from src.version_engine.infrastructure.supabase.audit_repository import AuditRepository

    db = SupabaseClient()
    auth = AuthorizationService(AuthorizationRepository(db.client))
    for suffix, user in zip(("a", "b"), USERS, strict=True):
        project = "issue053-project-" + suffix
        other = "issue053-project-" + ("b" if suffix == "a" else "a")
        auth.authorize(project, user, ProjectAction.CONTENT_WRITE)
        try:
            auth.authorize(other, user, ProjectAction.CONTENT_READ)
        except NotFoundException:
            pass
        else:
            raise AssertionError("Cross-tenant grant accepted")
        audit = AuditRepository(db)
        audit.insert("containment-probe", "docs/probe", project_id=project, operator_id=user)
        check(all(row["project_id"] == project for row in audit.list_by_project(project)), "Audit tenant filter failed")
        # These Scope consumers have been retired. Exercise their historical
        # storage privileges directly without restoring the removed runtime.
        scope = "issue053-scope-" + suffix
        db.client.table("scope_sync_events").insert({
            "project_id": project, "scope_id": scope, "head_version": "c" * 40,
            "affected_paths": ["docs/probe"], "source": "publish", "origin_user": user,
        }).execute()
        events = db.client.table("scope_sync_events").select("id").eq("project_id", project).eq("scope_id", scope).execute()
        check(bool(events.data), "Historical Scope event read failed")
        db.client.table("scope_sync_settings").upsert({
            "project_id": project, "scope_id": scope, "persona": "reviewer", "auto_sync": False,
        }, on_conflict="project_id,scope_id").execute()
        settings = db.client.table("scope_sync_settings").select("auto_sync").eq("project_id", project).eq("scope_id", scope).execute()
        check(settings.data and settings.data[0]["auto_sync"] is False, "Historical Scope settings update failed")
        tables = TableRepository(db.client)
        table_id = "issue053-backend-" + suffix
        tables.create(TableCreate(id=table_id, project_id=project, data={"probe": 1}))
        tables.update(table_id, TableUpdate(data={"probe": 2}))
        check(tables.get_by_id(table_id).data == {"probe": 2}, "Structured table update failed")
        check(tables.delete(table_id), "Structured table delete failed")

    # Exercise the production invoker publisher, including audit + sequence use,
    # in one real database transaction. This is SQL-contract proof, not S3 proof.
    outcome = db.client.rpc("publish_version_project_update", {
        "p_project_id": "issue053-project-a", "p_old_root_hash": "", "p_new_root_hash": "d" * 40,
        "p_head_commit_id": "e" * 40, "p_who": USERS[0], "p_message": "Security regression",
        "p_event_type": "commit", "p_changes": [], "p_conflicts": None,
        "p_created_at": datetime.now(UTC).isoformat(), "p_audit_agent_id": "", "p_audit_detail": {},
    }).execute()
    check(bool(outcome.data and outcome.data[0]["published"]), "Backend atomic publisher failed")


def rehearse(stack: LocalStack, migrations: list[Path]) -> dict:
    stack.sql(FIXTURE.read_text())
    before = snapshot(stack)
    status = json.loads(stack.cli("status", "--output", "json", capture=True))
    exposed = rest_matrix(status, before=True)
    expect_guard_failure(stack, CONTRACT.read_text())
    print("PASS: nonempty pre-fix leak reproduced and new contract rejects old schema", flush=True)

    # Exercise standalone column ACLs too: table REVOKE alone would miss these.
    stack.sql("GRANT SELECT (metadata) ON public.audit_logs TO authenticated; GRANT SELECT (label) ON public.bookmarks TO PUBLIC;")
    # Test this security upgrade against its actual source schema; later
    # destructive Contracts require separate reviewed data artifacts.
    stack.replace_migrations([path for path in migrations if path.name <= FIX.name])
    stack.cli("migration", "up", "--local", capture=True)
    stack.sql(CONTRACT.read_text())
    check(snapshot(stack) == before, "Security migration changed stored data")
    # CLI rerun is a no-op; direct SQL rerun must also be safe.
    stack.cli("migration", "up", "--local", capture=True)
    stack.sql(FIX.read_text())
    check(snapshot(stack) == before, "Migration retry changed stored data")
    denied = rest_matrix(status, before=False)
    backend_consumers(status)
    print("PASS: populated upgrade, retry, real REST/JWT denials and backend consumers", flush=True)

    guard = CONTRACT.read_text()
    body = guard[guard.index("DO $$"):guard.rindex("ROLLBACK;")]
    mutations = [
        "GRANT SELECT ON public.audit_logs TO anon;",
        "GRANT SELECT (metadata) ON public.audit_logs TO authenticated;",
        "GRANT USAGE ON public.scope_sync_events_id_seq TO PUBLIC;",
        "ALTER TABLE public.tables DISABLE ROW LEVEL SECURITY;",
        "ALTER VIEW public.version_activity_feed SET (security_invoker = false);",
        "GRANT EXECUTE ON FUNCTION public.repository_target_integrity_report() TO PUBLIC;",
        "GRANT EXECUTE ON FUNCTION public.issue_user_git_http_credential(text,text,text,text,text,uuid,text,text,text,text,text) TO service_role;",
        "CREATE ROLE issue053_bridge; GRANT SELECT ON public.audit_logs TO issue053_bridge; GRANT issue053_bridge TO authenticated WITH INHERIT FALSE;",
        "CREATE VIEW public.issue053_leaky_view AS SELECT * FROM public.audit_logs; GRANT SELECT ON public.issue053_leaky_view TO anon;",
        "CREATE FUNCTION public.issue053_leaky_rpc() RETURNS jsonb LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS 'SELECT public.repository_target_integrity_report()'; GRANT EXECUTE ON FUNCTION public.issue053_leaky_rpc() TO PUBLIC;",
    ]
    for mutation in mutations:
        expect_guard_failure(stack, "BEGIN;\n" + mutation + "\n" + body + "\nROLLBACK;")
        stack.sql(CONTRACT.read_text())
    print(f"PASS: {len(mutations)} deliberately unsafe ACL/RLS/view/RPC mutations are rejected", flush=True)

    # Full B1 + all forward migrations, using the same CLI and Docker image as CI.
    stack.replace_migrations(migrations)
    stack.cli("db", "reset", "--local", "--no-seed", capture=True)
    stack.sql(CONTRACT.read_text())
    stack.cli("test", "db", capture=True)
    print("PASS: fresh installation and complete pgTAP suite", flush=True)
    return {"pre_fix_http_checks": exposed, "post_fix_http_checks": denied, "negative_mutations": len(mutations),
            "populated_upgrade": "PASS", "data_unchanged": "PASS", "retry": "PASS", "backend_consumers": "PASS",
            "fresh_install": "PASS", "full_pgtap": "PASS"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port-base", type=int, default=26390)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    check(1024 <= args.port_base <= 65530, "Invalid isolated port range")
    migrations = sorted((ROOT / "supabase/migrations").glob("*.sql"))
    baseline = [p for p in migrations if p.name < FIX.name]
    hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in migrations}
    os.environ.setdefault("SUPABASE_INTERNAL_IMAGE_REGISTRY", "docker.io")
    with tempfile.TemporaryDirectory(prefix="issue053-db-") as directory:
        stack = LocalStack(Path(directory), baseline)
        config = stack.supabase / "config.toml"
        text = config.read_text()
        for offset in (0, 1, 2, 4):
            text = text.replace(str(55390 + offset), str(args.port_base + offset))
        config.write_text(text)
        try:
            stack.cli("start", "--exclude", EXCLUDE, capture=True)
            results = rehearse(stack, migrations)
            check(hashes == {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in migrations},
                  "Rehearsal changed migration artifacts")
            if args.report:
                args.report.parent.mkdir(parents=True, exist_ok=True)
                args.report.write_text(json.dumps({
                    "issue": "ISSUE-053", "time": datetime.now(UTC).isoformat(),
                    "source_sha": run("git", "-C", str(ROOT), "rev-parse", "HEAD").strip(),
                    "supabase_cli": run("supabase", "--version").strip(),
                    "postgres_version": stack.sql("SHOW server_version"),
                    "migration_sha256": hashes, "environment": "owned-local-docker", "results": results,
                }, indent=2) + "\n")
        finally:
            stack.cli("stop", "--no-backup", capture=True)


if __name__ == "__main__":
    main()
