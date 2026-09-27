"""One checksum-pinned catch-up of the deployed June history to the July history.

Runs unchanged Supabase SQL with the official CLI and existing immutable data
artifacts. An already upgraded database is a no-op. No storage operations.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .catalog import DataMigrationCatalog
from .database import PsqlClient
from .policy import HISTORICAL_RELEASE, historical_compatibility
from .runner import DataMigrationRunner
from .schema_history import baseline_version, historical_source, load_baseline


def load_plan(root: Path) -> dict:
    historical_compatibility(root)
    plan = json.loads((root / HISTORICAL_RELEASE).read_text())
    catalog = DataMigrationCatalog(root)
    for step in plan["phases"]:
        if "data" in step:
            actual = catalog.get(step["data"]).checksum
            if actual != plan["data_sha256"][step["data"]]:
                raise ValueError("Historical data artifact checksum changed")
        elif step.get("schema_through") not in {name[:14] for name in plan["schema_sha256"]}:
            raise ValueError("Unreviewed schema phase")
    return plan


def assert_authorized(plan: dict, ref: str, source: dict[str, str]) -> None:
    evidence = json.loads(source.get("LEGACY_UPGRADE_AUTHORIZATION", "{}"))
    if (
        evidence.get("project_ref") != ref
        or evidence.get("plan_id") != plan["id"]
        or evidence.get("write_freeze_verified") is not True
        or not evidence.get("restore_point")
        or not time.time() < evidence.get("expires_at", 0) <= time.time() + 14400
    ):
        raise ValueError(
            "Historical upgrade needs current target-bound backup and write-freeze evidence"
        )


def bind(root: Path, source: dict[str, str]) -> tuple[PsqlClient, dict[str, str]]:
    spec = importlib.util.spec_from_file_location(
        "release_connection", root / "supabase/releases/connection.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    values = module.connection_environment(source)
    source.update(values)
    env = {**source, **values}
    db = PsqlClient(values["DATABASE_URL"], base_environment=env)
    db.assert_supabase_target(
        project_ref=source["SUPABASE_PROJECT_ID"], api_url=values["SUPABASE_URL"]
    )
    return db, env


def snapshot(db: PsqlClient, secret: str) -> dict:
    # Raw legacy tokens exist only in process memory long enough to derive the
    # expected HMAC. They are never written to an artifact or diagnostic log.
    report = json.loads(
        db.scalar("""
BEGIN READ ONLY;
SELECT jsonb_build_object(
 'projects',(SELECT coalesce(jsonb_agg(id ORDER BY id),'[]') FROM public.projects),
 'users',(SELECT coalesce(jsonb_agg(md5(id::text || ':' || coalesce(email,'')) ORDER BY id),'[]') FROM auth.users),
 'profiles',(SELECT coalesce(jsonb_agg(md5((to_jsonb(p)-'updated_at')::text) ORDER BY user_id),'[]') FROM public.profiles p),
 'org_members',(SELECT coalesce(jsonb_agg(to_jsonb(m)-'joined_at' ORDER BY id),'[]') FROM public.org_members m),
 'commits',(SELECT coalesce(jsonb_agg(id ORDER BY id),'[]') FROM public.version_commits),
 'credentials',(SELECT coalesce(jsonb_agg(jsonb_build_object('id',id,'key_hash',key_hash,'access_surface_id',access_surface_id,'status',status)),'[]') FROM public.access_surface_credentials),
 'members',(SELECT coalesce(jsonb_agg(jsonb_build_object('project_id',project_id,'user_id',user_id,'role',role)),'[]') FROM public.project_members)
);
ROLLBACK;
""")
    )
    legacy = db.scalar(
        "SELECT EXISTS (SELECT FROM information_schema.columns WHERE table_schema='public' AND table_name='repo_scopes' AND column_name='access_key')"
    )
    report["expected_hashes"] = []
    if legacy == "t":
        if not secret:
            raise ValueError("Original effective credential HMAC secret is required")
        raw = json.loads(
            db.scalar(
                "SELECT coalesce(jsonb_agg(access_key),'[]') FROM public.repo_scopes WHERE access_key IS NOT NULL AND access_key_revoked_at IS NULL"
            )
        )
        report["expected_hashes"] = [
            hmac.new(secret.encode(), value.strip().encode(), hashlib.sha256).hexdigest()
            for value in raw
        ]
    return report


def verify_continuity(before: dict, after: dict) -> dict:
    for name in ("projects", "users", "profiles", "commits"):
        if not set(before[name]) <= set(after[name]):
            raise ValueError(f"Historical upgrade did not preserve {name}")
    roles = {"viewer": 0, "reader": 0, "editor": 1, "admin": 2, "member": 1, "owner": 3}
    members = {(m["project_id"], m["user_id"]): m["role"] for m in after["members"]}
    if any(
        roles.get(members.get((m["project_id"], m["user_id"])), -1) < roles.get(m["role"], 99)
        for m in before["members"]
    ):
        raise ValueError("Project membership was removed or downgraded")
    orgs = {json.dumps(row, sort_keys=True) for row in after["org_members"]}
    if any(json.dumps(row, sort_keys=True) not in orgs for row in before["org_members"]):
        raise ValueError("Organization membership changed")
    credentials = {row["id"]: row for row in after["credentials"]}
    if any(credentials.get(row["id"]) != row for row in before["credentials"]):
        raise ValueError("An existing credential changed")
    active = {row["key_hash"] for row in after["credentials"] if row["status"] == "active"}
    if not set(before["expected_hashes"]) <= active:
        raise ValueError("Legacy credential continuity failed")
    return {
        "projects_preserved": len(before["projects"]),
        "users_preserved": len(before["users"]),
        "profiles_preserved": len(before["profiles"]),
        "commits_preserved": len(before["commits"]),
        "legacy_credentials_preserved": len(before["expected_hashes"]),
        "existing_credentials_preserved": len(before["credentials"]),
        "memberships_preserved": True,
    }


def checkpoint_cipher(secret: str, ref: str, plan_id: str) -> tuple[AESGCM, bytes]:
    if not secret:
        raise ValueError("Credential continuity secret is required")
    context = ("puppyone-release-checkpoint-v1:" + ref + ":" + plan_id).encode()
    return AESGCM(hmac.new(secret.encode(), context, hashlib.sha256).digest()), context


def original_snapshot(db: PsqlClient, plan: dict, source: dict[str, str]) -> dict:
    name = plan["id"] + "_checkpoint"
    cipher, context = checkpoint_cipher(
        source.get("ACCESS_CREDENTIAL_HASH_SECRET", ""), source["SUPABASE_PROJECT_ID"], plan["id"]
    )
    saved = db.receipt(name)
    if saved:
        encoded = base64.b64decode(saved["encrypted_snapshot"])
        return json.loads(cipher.decrypt(encoded[:12], encoded[12:], context))
    before = snapshot(db, source["ACCESS_CREDENTIAL_HASH_SECRET"])
    nonce = os.urandom(12)
    encrypted = nonce + cipher.encrypt(nonce, json.dumps(before).encode(), context)
    db.scalar(
        "INSERT INTO public.migration_log(name, applied_at, summary) VALUES (:'name', now(), :'summary'::jsonb)",
        variables={
            "name": name,
            "summary": json.dumps(
                {
                    "encrypted_snapshot": base64.b64encode(encrypted).decode(),
                    "source_sha": source.get("GITHUB_SHA", "local-rehearsal"),
                }
            ),
        },
    )
    return before


def main() -> None:
    root = Path(__file__).resolve().parents[4]
    plan = load_plan(root)
    source = dict(os.environ)
    db, env = bind(root, source)
    versions = db.applied_schema_versions()
    baseline = load_baseline(root)
    if baseline and baseline_version(baseline) in versions:
        print("B1 schema history is active; historical catch-up is not applicable.")
        return
    checkpoint_exists = db.receipt(plan["id"] + "_checkpoint") is not None
    completed = db.receipt(plan["id"])
    if plan["through_version"] in versions and (completed or not checkpoint_exists):
        print("Historical production catch-up already complete; no writes.")
        return
    if plan["from_version"] not in versions:
        raise ValueError("Unsupported starting schema; historical upgrade refused")
    assert_authorized(plan, source["SUPABASE_PROJECT_ID"], source)
    catalog = DataMigrationCatalog(root)
    # Prevent a second runner from interleaving another historical upgrade.
    with (
        db.advisory_lock(plan["id"]),
        tempfile.TemporaryDirectory(prefix="puppyone-history-") as work,
    ):
        before = original_snapshot(db, plan, source)
        for step in plan["phases"]:
            db, env = bind(root, source)
            versions = db.applied_schema_versions()
            if "schema_through" in step:
                through = step["schema_through"]
                if through in versions:
                    continue
                stage = Path(work) / through
                target = stage / "supabase/migrations"
                target.mkdir(parents=True)
                shutil.copyfile(root / "supabase/config.toml", stage / "supabase/config.toml")
                for name in plan["schema_sha256"]:
                    if name[:14] <= through:
                        shutil.copyfile(historical_source(root, name), target / name)
                command = [
                    "supabase",
                    "db",
                    "push",
                    "--workdir",
                    str(stage),
                    "--db-url",
                    env["SUPABASE_DATABASE_URL"],
                    "--yes",
                ]
                result = subprocess.run(
                    command, env=env, capture_output=True, text=True, timeout=300
                )
                if result.returncode:
                    raise RuntimeError(
                        f"Historical schema phase {through} failed; stopped before subsequent phases"
                    )
                print(f"Historical schema phase verified: {through}", flush=True)
            elif step["skip_if_schema"] not in versions:
                os.environ.update(env)
                runner = DataMigrationRunner(catalog, db)
                runner.run(step["data"])
                runner.verify(step["data"])
                print(f"Historical data phase verified: {step['data']}", flush=True)
        db, env = bind(root, source)
        after = snapshot(db, "")
        result = verify_continuity(before, after)
        db.command(
            [
                "-q",
                "-v",
                "ON_ERROR_STOP=1",
                "-f",
                str(root / "supabase/tests/_support/schema_contracts.inc"),
            ],
            timeout=120,
        )
        db.record_receipt(
            migration_id=plan["id"],
            checksum=hashlib.sha256((root / HISTORICAL_RELEASE).read_bytes()).hexdigest(),
            source_sha=source.get("GITHUB_SHA", "local-rehearsal"),
            legacy=True,
        )
        result.update(
            plan_id=plan["id"],
            source_sha=source.get("GITHUB_SHA", "local-rehearsal"),
            verified=True,
        )
        print(json.dumps(result, sort_keys=True))
        if source.get("GITHUB_STEP_SUMMARY"):
            with open(source["GITHUB_STEP_SUMMARY"], "a") as output:
                output.write(
                    "\nHistorical upgrade continuity verified:\n```json\n"
                    + json.dumps(result, indent=2)
                    + "\n```\n"
                )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # SQL errors can contain customer values. CI output must stay redacted.
        raise SystemExit(
            f"Historical upgrade stopped ({type(error).__name__}); no customer values printed"
        ) from None
