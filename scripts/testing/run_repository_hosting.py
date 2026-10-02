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
import re
import socket
import subprocess
import sys
import tempfile
import time
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
    if result.get("live") and (
        result.get("supabase_sql_suite_executed") is not True
        or result.get("supabase_sql_complete") is not True or "sql_exit" not in result
    ):
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


class SupabaseStartupError(RuntimeError):
    def __init__(self, message, diagnostics):
        super().__init__(message)
        self.diagnostics = diagnostics


def supabase_database_state(container, *, env):
    """Inspect only our DB's safe state fields, never Env, logs, or health output."""
    if not re.fullmatch(r"supabase_db_puppy-baseline-[a-z0-9]{8}", container):
        raise ValueError("Startup diagnostics require an owned Supabase container")
    template = (
        '{"status":"{{.State.Status}}","running":{{.State.Running}},'
        '"exit_code":{{.State.ExitCode}},"oom_killed":{{.State.OOMKilled}},'
        '"health":"{{if .State.Health}}{{.State.Health.Status}}{{end}}"}'
    )
    try:
        response = subprocess.run(
            ["docker", "inspect", "--format", template, container],
            env=env, text=True, capture_output=True, timeout=5, check=False,
        )
        if response.returncode:
            return {"status": "unavailable"}
        raw = json.loads(response.stdout)
        if not isinstance(raw, dict) or raw.get("status") not in {
            "created", "running", "paused", "restarting", "removing", "exited", "dead",
        }:
            return {"status": "unavailable"}
        if (
            type(raw.get("running")) is not bool
            or type(raw.get("exit_code")) is not int
            or type(raw.get("oom_killed")) is not bool
            or raw.get("health") not in {"", "starting", "healthy", "unhealthy"}
        ):
            return {"status": "unavailable"}
        return {key: raw[key] for key in ("status", "running", "exit_code", "oom_killed", "health")}
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
        return {"status": "unavailable"}


def start_local_supabase(
    command, container, *, env, timeout=300, created_timeout=60, poll_interval=5,
):
    """Bound startup and distinguish a Docker start stall from service readiness.

    Inspect before reaping our CLI child, then let the caller stop its owned
    stack. Never restart the shared daemon or remove someone else's resources.
    CLI output can contain local API keys: consume it, but do not put it in
    exceptions, console output, or diagnostic evidence.
    """
    if not re.fullmatch(r"supabase_db_puppy-baseline-[a-z0-9]{8}", container):
        raise ValueError("Startup requires an owned Supabase container")
    started = time.monotonic()
    created_since = None
    diagnostics = {
        "container": container, "timeout_seconds": timeout,
        "created_timeout_seconds": created_timeout,
        "database": {"status": "unavailable"},
    }
    process = subprocess.Popen(
        command, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        while True:
            elapsed = time.monotonic() - started
            diagnostics["elapsed_seconds"] = round(elapsed, 3)
            if elapsed >= timeout:
                diagnostics["reason"] = "supabase_startup_timeout"
                raise SupabaseStartupError("Local Supabase startup timed out", diagnostics)
            try:
                output, _ = process.communicate(timeout=min(poll_interval, timeout - elapsed))
            except subprocess.TimeoutExpired:
                state = supabase_database_state(container, env=env)
                diagnostics["database"] = state
                now = time.monotonic()
                diagnostics["elapsed_seconds"] = round(now - started, 3)
                if state["status"] == "created":
                    if created_since is None:
                        created_since = now
                    elif now - created_since >= created_timeout:
                        diagnostics["reason"] = "docker_container_not_started"
                        raise SupabaseStartupError(
                            "Owned Docker database container never started "
                            f"within {created_timeout}s of observation (state=created); "
                            "verify Docker can execute a container before retrying Supabase",
                            diagnostics,
                        ) from None
                else:
                    created_since = None
                continue
            diagnostics["elapsed_seconds"] = round(time.monotonic() - started, 3)
            if process.returncode:
                diagnostics["database"] = supabase_database_state(container, env=env)
                diagnostics["reason"] = "supabase_cli_failed"
                raise SupabaseStartupError(
                    f"Local Supabase startup failed (exit {process.returncode})", diagnostics,
                )
            diagnostics["reason"] = "started"
            return output, diagnostics
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)


def local_supabase_environment(environ):
    """Same official registry as database CI; never inherit hosted credentials."""
    env = {
        k: v for k, v in environ.items()
        if not k.startswith(("SUPABASE_", "AWS_", "S3_", "GIT_", "HOSTING_TEST_"))
    }
    env["SUPABASE_INTERNAL_IMAGE_REGISTRY"] = "docker.io"
    return env


def prepare_supabase_sql_fixture(stack, cli_env):
    if not re.fullmatch(r"supabase_db_puppy-baseline-[a-z0-9]{8}", stack.container):
        raise ValueError("SQL probe fixture requires an owned Supabase container")
    subprocess.run(
        ["docker", "exec", "-i", stack.container, "psql", "-U", "postgres", "-d", "postgres",
         "-X", "-q", "--single-transaction", "-v", "ON_ERROR_STOP=1"],
        input=(ROOT / "scripts/testing/supabase_sql_fixture.sql").read_text(),
        env=cli_env, text=True, capture_output=True, check=True, timeout=30,
    )


def record_sql_tests(output, result):
    summary = re.search(r"Files=(\d+), Tests=(\d+),", output)
    skipped = bool(re.search(r"SMOKE TEST SKIPPED|#\s*(?:SKIP|TODO)\b|\bSkipped:", output, re.IGNORECASE))
    result["supabase_sql_counts"] = {
        "files": int(summary[1]) if summary else 0,
        "tests": int(summary[2]) if summary else 0,
    }
    result["supabase_sql_skip_or_todo_reported"] = skipped
    result["supabase_sql_complete"] = bool(
        summary and int(summary[1]) > 0 and int(summary[2]) > 0
        and re.search(r"^Result: PASS\s*$", output, re.MULTILINE) and not skipped
    )


def run_supabase_suites(stack, command, env, cli_env, result):
    # Existing pgTAP contracts assume a nearly empty DB (global claim batch=25).
    # Seed one org so the GC smoke probe runs, then test SQL before Python tenants.
    prepare_supabase_sql_fixture(stack, cli_env)
    result["supabase_sql_suite_executed"] = True
    sql = subprocess.run(
        ["supabase", "test", "db", "--workdir", str(stack.directory)], cwd=ROOT, env=cli_env,
        capture_output=True, text=True, timeout=300, check=False,
    )
    output = sql.stdout + "\n" + sql.stderr
    print(output, flush=True)
    result["sql_exit"] = sql.returncode
    record_sql_tests(output, result)
    result["pytest_exit"] = subprocess.call(command, cwd=ROOT / "backend", env=env)


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
    cli_env = local_supabase_environment(os.environ)
    env = cli_env.copy()
    env.pop("SUPABASE_INTERNAL_IMAGE_REGISTRY")
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

                result["supabase_sql_suite_executed"] = False
                result["supabase_image_registry"] = cli_env["SUPABASE_INTERNAL_IMAGE_REGISTRY"]
                result["supabase_cli_version"] = subprocess.check_output(
                    ["supabase", "--version"], env=cli_env, text=True,
                    stderr=subprocess.DEVNULL, timeout=15,
                ).strip()

                def cli(*parts, capture=False):
                    cli_command = ["supabase", *parts, "--workdir", temp]
                    if parts[0] == "start":
                        try:
                            output, diagnostics = start_local_supabase(
                                cli_command, stack.container, env=cli_env,
                            )
                        except SupabaseStartupError as exc:
                            result["supabase_startup"] = exc.diagnostics
                            raise
                        result["supabase_startup"] = diagnostics
                        return output
                    completed = subprocess.run(
                        cli_command,
                        env=cli_env,
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
                        HOSTING_TEST_SUPABASE="1",
                        HOSTING_TEST_ANON_KEY=status["ANON_KEY"],
                        SUPABASE_URL=status["API_URL"],
                        SUPABASE_KEY=status["SERVICE_ROLE_KEY"],
                        SUPABASE_SERVICE_ROLE_KEY=status["SERVICE_ROLE_KEY"],
                    )
                    command.extend(["--hosting-live", "--hosting-supabase"])
                    run_supabase_suites(stack, command, env, cli_env, result)
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
