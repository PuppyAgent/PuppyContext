#!/usr/bin/env python3
"""Run actual hosting tests; optionally provision a disposable Supabase stack.

No developer/production environment is used. --live starts its own stack,
applies the real migrations, runs SQL tests, and always stops only that stack.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def free_ports():
    sockets = [socket.socket() for _ in range(4)]
    try:
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


def record_tests(path, result):
    result["layers"] = {}
    result["gaps"] = []
    if not path.exists():
        result["evidence_error"] = "JUnit report missing"
        return
    try:
        cases = list(ET.parse(path).iter("testcase"))
    except (ET.ParseError, OSError):
        result["evidence_error"] = "JUnit report unreadable"
        return
    if not cases:
        result["evidence_error"] = "JUnit report contains no test cases"
        return
    layers = {}
    gaps = []
    for case in cases:
        props = {
            p.get("name"): p.get("value") for p in case.findall("./properties/property")
        }
        layer = props.get("execution_layer", "existing_regression")
        counts = layers.setdefault(layer, Counter())
        skip = case.find("skipped")
        status = "passed"
        if case.find("failure") is not None or case.find("error") is not None:
            status = "failed"
        elif skip is not None:
            status = "known_gap" if skip.get("type") == "pytest.xfail" else "skipped"
        counts[status] += 1
        if props.get("known_gap"):
            gaps.append(
                {
                    "test": case.get("name"),
                    "reason": props["known_gap"],
                    "status": status,
                }
            )
    result["layers"] = layers
    result["gaps"] = gaps


def result_exit_code(result):
    """Strict mode cannot turn a missing service or xfail into acceptance."""
    code = max(result.get("pytest_exit", 1), result.get("sql_exit", 0))
    if result.get("infrastructure_error") or result.get("evidence_error"):
        return max(1, code)
    layers = result.get("layers", {})
    if not layers or any(counts.get("failed", 0) for counts in layers.values()):
        return max(1, code)
    if result.get("target") and any(
        counts.get("skipped", 0) or counts.get("known_gap", 0)
        for counts in layers.values()
    ):
        return max(1, code)
    return code


def main():
    parser = argparse.ArgumentParser()
    services = parser.add_mutually_exclusive_group()
    services.add_argument("--live", action="store_true")
    services.add_argument(
        "--native-pg", action="store_true",
        help="supplementary PostgreSQL 17 with auth stubs, NOT Supabase/S3 acceptance",
    )
    parser.add_argument("--target", action="store_true", help="known gaps must fail")
    parser.add_argument(
        "--output", type=Path, default=ROOT / "backend/.hosting-test-results"
    )
    args, pytest_args = parser.parse_known_args()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    # Do not accidentally attribute an older successful artifact to a failed startup.
    (args.output / "junit.xml").unlink(missing_ok=True)
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("SUPABASE_", "AWS_", "S3_", "GIT_", "HOSTING_TEST_"))
    }
    env.update(
        APP_ENV="test",
        MANAGED_AI_ENABLED="false",
        SKIP_AUTH="false",
        SUPABASE_URL="http://127.0.0.1:1",
        SUPABASE_KEY="isolated-test-only",
    )
    command = [
        str(ROOT / "backend/.venv/bin/python"),
        "-m",
        "pytest",
        "tests/repository_hosting",
        "-ra",
        "--tb=short",
        f"--junitxml={args.output / 'junit.xml'}",
    ]
    if args.target:
        command.append("--hosting-target")
    command.extend(pytest_args)
    result = {
        "live": args.live,
        "native_pg": args.native_pg,
        "database_environment": "supabase" if args.live else (
            "native_pg17_auth_stub" if args.native_pg else "not_started"
        ),
        "target": args.target,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "pytest_arguments": pytest_args,
        "git_version": subprocess.check_output(["git", "--version"], text=True).strip(),
        "python_version": sys.version,
        "product_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "worktree_dirty": bool(subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=ROOT, text=True
        ).strip()),
        "schema_sha256": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((ROOT / "supabase/migrations").glob("*.sql"))
        },
    }
    try:
        if args.native_pg:
            from native_postgres import native_postgres

            with native_postgres() as database:
                result["postgres_version"] = database["server_version"]
                result["supabase_sql_suite_executed"] = False
                env.update(
                    HOSTING_TEST_STACK=database["stack"],
                    HOSTING_TEST_DB_URL=database["url"],
                )
                command.append("--hosting-live")
                result["pytest_exit"] = subprocess.call(command, cwd=ROOT / "backend", env=env)
        elif not args.live:
            result["pytest_exit"] = subprocess.call(
                command, cwd=ROOT / "backend", env=env
            )
        else:
            spec = importlib.util.spec_from_file_location(
                "database_baseline", ROOT / "scripts/database_baseline.py"
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            with tempfile.TemporaryDirectory(prefix="hosting-tests-") as temp:
                stack = module.LocalStack(Path(temp), [])
                db, shadow, api, mail = free_ports()

                def cli(*parts, capture=False):
                    completed = subprocess.run(
                        ["supabase", *parts, "--workdir", temp],
                        text=True,
                        stdout=subprocess.PIPE,
                        check=True,
                        timeout=300,
                    )
                    # Suppress the local API keys printed by `supabase start/status`.
                    if not capture and parts[0] not in {"start", "status"}:
                        print(completed.stdout)
                    return completed.stdout

                stack.cli = cli
                config = (stack.supabase / "config.toml").read_text()
                for old, new in [
                    (55392, db),
                    (55390, shadow),
                    (55391, api),
                    (55394, mail),
                ]:
                    config = config.replace(str(old), str(new))
                (stack.supabase / "config.toml").write_text(config)
                try:
                    stack.cli(
                        "start",
                        "--exclude",
                        "studio,realtime,storage-api,imgproxy,edge-runtime,logflare,vector,supavisor",
                    )
                    stack.replace_migrations(
                        sorted((ROOT / "supabase/migrations").glob("*.sql"))
                    )
                    stack.cli("migration", "up", "--local")
                    status = json.loads(
                        stack.cli("status", "--output", "json", capture=True)
                    )
                    env.update(
                        HOSTING_TEST_STACK=stack.project,
                        HOSTING_TEST_DB_URL=f"postgresql://postgres:postgres@127.0.0.1:{db}/postgres",
                        SUPABASE_URL=status["API_URL"],
                        SUPABASE_KEY=status["SERVICE_ROLE_KEY"],
                        SUPABASE_SERVICE_ROLE_KEY=status["SERVICE_ROLE_KEY"],
                    )
                    command.append("--hosting-live")
                    result["pytest_exit"] = subprocess.call(
                        command, cwd=ROOT / "backend", env=env
                    )
                    result["sql_exit"] = subprocess.call(
                        ["supabase", "test", "db", "--workdir", temp], cwd=ROOT, env=env
                    )
                finally:
                    stack.cli("stop", "--no-backup")
    except Exception as exc:
        result["infrastructure_error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        record_tests(args.output / "junit.xml", result)
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        result["exit_code"] = result_exit_code(result)
        (args.output / "run.json").write_text(json.dumps(result, indent=2) + "\n")
    return result["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
