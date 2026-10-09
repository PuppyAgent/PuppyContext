#!/usr/bin/env python3
"""Run the documented installer on isolated volumes with synthetic credentials.

Only volumes belonging to the generated puppyone-install-* project are removed.
No hosted credentials, user's .env, Pay service or pre-existing session is read.
"""

import argparse
import base64
import hashlib
import hmac
import io
import json
import os
import secrets
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Pin the deployed native release for supported in-place application upgrades.
# Older storage formats require the separately tested operator migration; the
# legacy variant proves ordinary startup cannot bypass that cutover boundary.
UPGRADE_BASE = "a5dc0ea53f7a61e6df00d66751884be5ba3f6a14"
LEGACY_BASE = "c28e38a3e44f1106f58ce6ae6cfdf6255a51f495"


def jwt(role, secret):
    def encode(value):
        return (
            base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode())
            .decode()
            .rstrip("=")
        )

    message = f"{encode({'alg': 'HS256', 'typ': 'JWT'})}.{encode({'role': role, 'iss': 'supabase-demo', 'exp': int(time.time()) + 86400})}"
    signature = (
        base64.urlsafe_b64encode(
            hmac.new(secret.encode(), message.encode(), hashlib.sha256).digest()
        )
        .decode()
        .rstrip("=")
    )
    return f"{message}.{signature}"


def assert_legacy_upgrade_blocked(compose, artifacts):
    """An old populated format cannot silently cross the operator cutover."""
    result = subprocess.run(
        [*compose, "up", "--detach", "--wait", "--wait-timeout", "420"],
        check=False,
        cwd=ROOT,
        timeout=600,
    )
    if result.returncode == 0:
        raise RuntimeError(
            "Legacy storage incorrectly upgraded without an operator cutover"
        )
    log = subprocess.check_output(
        [*compose, "logs", "--no-color", "migrate"], text=True, timeout=30
    )
    (artifacts / "legacy-cutover-refusal.log").write_text(log)
    if "20261003230100_contract_final_entrypoint_storage.sql" not in log:
        raise RuntimeError("Legacy startup failed outside the intended Contract gate")
    running = subprocess.check_output(
        [*compose, "ps", "--status", "running", "--services"],
        text=True,
        timeout=30,
    ).splitlines()
    if {"api", "web"} & set(running):
        raise RuntimeError("Application started before legacy data was migrated")
    proof = subprocess.check_output(
        [
            *compose,
            "exec",
            "-T",
            "db",
            "psql",
            "-U",
            "postgres",
            "-d",
            "postgres",
            "-XAt",
            "-c",
            (
                "SELECT EXISTS(SELECT FROM projects WHERE id='issue049-project') "
                "AND EXISTS(SELECT FROM github_sync_bindings WHERE id='issue049-binding') "
                "AND EXISTS(SELECT FROM github_sync_log WHERE id='issue049-log-success') "
                "AND EXISTS(SELECT FROM entrypoint_cutover_control WHERE NOT writes_frozen) "
                "AND NOT EXISTS(SELECT FROM supabase_migrations.schema_migrations WHERE version='20261003230100');"
            ),
        ],
        text=True,
        timeout=30,
    ).strip()
    if proof != "t":
        raise RuntimeError(
            "Refused cutover changed fixture data or fabricated release evidence"
        )
    (artifacts / "result.json").write_text(
        json.dumps(
            {
                "variant": "legacy",
                "result": "passed",
                "upgrade_from": LEGACY_BASE,
                "operator_cutover_required": True,
                "application_blocked": True,
                "legacy_records_preserved": True,
            }
        )
        + "\n"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--variant",
        choices=["default", "custom", "upgrade", "legacy", "release"],
        default="default",
    )
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument(
        "--upgrade-base", help="Exact deployed/base commit for the release rehearsal"
    )
    args = parser.parse_args()
    if args.variant == "release" and not args.upgrade_base:
        parser.error("release rehearsal requires --upgrade-base")
    args.artifacts.mkdir(parents=True, exist_ok=True)
    project = "puppyone-install-" + secrets.token_hex(5)
    with tempfile.TemporaryDirectory(prefix=project) as temporary:
        directory = Path(temporary)
        values = dict(
            line.split("=", 1)
            for line in (ROOT / "docker/.env.example").read_text().splitlines()
            if line and not line.startswith("#")
        )
        # Both variants use isolated ports; custom also proves rotated keys,
        # PostgreSQL credentials, and a non-default bucket/access key work.
        values.update(
            SUPABASE_API_PORT="18000",
            BACKEND_PORT="19090",
            FRONTEND_PORT="13000",
            DATABASE_PORT="15432",
            MINIO_CONSOLE_PORT="19001",
            MAIL_PORT="18025",
        )
        if args.variant in {"custom", "upgrade", "legacy", "release"}:
            secret = secrets.token_hex(32)
            values.update(
                JWT_SECRET=secret,
                ANON_KEY=jwt("anon", secret),
                SERVICE_ROLE_KEY=jwt("service_role", secret),
                POSTGRES_PASSWORD=secrets.token_hex(16),
                S3_ACCESS_KEY="install-access",
                S3_SECRET_KEY=secrets.token_hex(16),
                S3_BUCKET="install-custom",
            )
        env_file = directory / "compose.env"
        env_file.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
        env_file.chmod(0o600)

        def compose_for(root):
            return [
                "docker",
                "compose",
                "--project-name",
                project,
                "--env-file",
                str(env_file),
                "-f",
                str(root / "docker/docker-compose.yml"),
                "-f",
                str(root / "docker/compose.install-test.yml"),
            ]

        previous_root = ROOT
        if args.variant in {"upgrade", "legacy", "release"}:
            previous_root = directory / "previous-release"
            previous_root.mkdir()
            archive = subprocess.check_output(
                [
                    "git",
                    "archive",
                    "--format=tar",
                    LEGACY_BASE
                    if args.variant == "legacy"
                    else (args.upgrade_base or UPGRADE_BASE),
                ],
                cwd=ROOT,
                timeout=60,
            )
            with tarfile.open(fileobj=io.BytesIO(archive)) as source:
                source.extractall(previous_root, filter="data")
        compose = compose_for(previous_root)

        def run(*command, **kwargs):
            subprocess.run(command, check=True, cwd=ROOT, timeout=1800, **kwargs)

        try:
            run(*compose, "build")
            run(*compose, "up", "--detach", "--wait", "--wait-timeout", "420")
            run(*compose, "run", "--rm", "migrate")
            test_env = {
                **os.environ,
                "INSTALL_API": "http://127.0.0.1:19090",
                "INSTALL_AUTH": "http://127.0.0.1:18000",
                "INSTALL_WEB": "http://127.0.0.1:13000",
                "INSTALL_MAIL": "http://127.0.0.1:18025",
                "INSTALL_ANON_KEY": values["ANON_KEY"],
                "INSTALL_SERVICE_ROLE_KEY": values["SERVICE_ROLE_KEY"],
                "INSTALL_STATE": str(directory / "state.json"),
            }
            for phase in ("seed", "verify"):
                if phase == "verify":
                    if args.variant == "legacy":
                        # Populate the old names while the old application is live.
                        # The same database, object bucket, users and sessions survive.
                        run(
                            *compose,
                            "exec",
                            "-T",
                            "db",
                            "psql",
                            "-U",
                            "postgres",
                            "-d",
                            "postgres",
                            "-X",
                            "-v",
                            "ON_ERROR_STOP=1",
                            input=(
                                ROOT / "supabase/test_fixtures/entrypoint_storage.sql"
                            ).read_bytes(),
                        )
                    # Preserve volumes, recreate every service, and rerun the
                    # same installer. Auth, DB rows and object bytes must survive.
                    if args.variant == "release":
                        sys.path.insert(0, str(ROOT / "backend"))
                        from urllib.parse import quote

                        from src.infra.data_migrations.database import PsqlClient
                        from src.infra.data_migrations.release.docker import Docker
                        from src.infra.data_migrations.release.engine import Release
                        from src.infra.data_migrations.release.plan import ReleasePlan
                        from src.infra.data_migrations.release.target import (
                            DatabaseTarget,
                        )
                        from testing import release_fixtures

                        # Expose only this owned object service on a free loopback port.
                        with socket.socket() as listener:
                            listener.bind(("127.0.0.1", 0))
                            object_port = listener.getsockname()[1]
                        overlay = directory / "release-ports.json"
                        overlay.write_text(
                            json.dumps(
                                {
                                    "services": {
                                        "minio": {
                                            "ports": [f"127.0.0.1:{object_port}:9000"]
                                        }
                                    }
                                }
                            )
                        )
                        compose = [*compose_for(ROOT), "-f", str(overlay)]
                        run(*compose, "up", "--detach", "--no-deps", "minio")
                        url = f"postgresql://postgres:{quote(values['POSTGRES_PASSWORD'], safe='')}@127.0.0.1:15432/postgres?sslmode=disable"
                        environment = {
                            **os.environ,
                            "DATA_MIGRATION_DATABASE_URL": url,
                            "RELEASE_TARGET": "docker",
                            "NO_PROXY": "127.0.0.1,localhost,::1",
                            "no_proxy": "127.0.0.1,localhost,::1",
                            "RELEASE_PRIVATE_ARTIFACTS": str(args.artifacts.resolve()),
                            "S3_ENDPOINT_URL": f"http://127.0.0.1:{object_port}",
                            "S3_BUCKET_NAME": values["S3_BUCKET"],
                            "S3_REGION": "us-east-1",
                            "S3_ACCESS_KEY_ID": values["S3_ACCESS_KEY"],
                            "S3_SECRET_ACCESS_KEY": values["S3_SECRET_KEY"],
                        }
                        db = PsqlClient(url, base_environment=environment)
                        fixture_seeded = release_fixtures.seed(
                            db,
                            json.loads((directory / "state.json").read_text())[
                                "projectId"
                            ],
                        )

                        def accept_release(db=db, fixture_seeded=fixture_seeded):
                            if fixture_seeded:
                                release_fixtures.verify(db)
                            run(
                                "npx",
                                "--prefix",
                                "e2e",
                                "playwright",
                                "test",
                                "--config=e2e/install/playwright.config.mjs",
                                env={
                                    **test_env,
                                    "INSTALL_PHASE": "verify",
                                    "INSTALL_UPGRADED": "true",
                                    "INSTALL_REPORT": str(
                                        args.artifacts.resolve() / "verify.json"
                                    ),
                                },
                            )
                            return {
                                "authenticated_read_write": True,
                                "browser_session_preserved": True,
                                "permissions_verified": True,
                            }

                        platform = Docker(
                            ROOT,
                            compose,
                            project=project,
                            directory=args.artifacts.resolve(),
                            accept=accept_release,
                            decisions=release_fixtures.decisions,
                        )
                        target = DatabaseTarget(ROOT, db, platform, environment)
                        source_sha = subprocess.check_output(
                            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
                        ).strip()
                        result = Release(ReleasePlan.load(ROOT), target).run(source_sha)
                        repeated = Release(ReleasePlan.load(ROOT), target).run(
                            source_sha
                        )
                        assert (
                            result["state"] == "accepted"
                            and repeated["state"] == "already_accepted"
                        )
                        (args.artifacts / "release.json").write_text(
                            json.dumps(
                                {
                                    "upgrade_from": args.upgrade_base,
                                    "source_sha": source_sha,
                                    "release": result,
                                    "repeated": repeated,
                                }
                            )
                        )
                    else:
                        run(*compose, "down")
                    if args.variant in {"upgrade", "legacy"}:
                        compose = compose_for(ROOT)
                        run(*compose, "build")
                        if args.variant == "legacy":
                            assert_legacy_upgrade_blocked(compose, args.artifacts)
                            return
                        run(
                            *compose,
                            "up",
                            "--detach",
                            "--wait",
                            "--wait-timeout",
                            "420",
                        )
                        # Exercise repeat execution after a populated upgrade.
                        run(*compose, "run", "--rm", "migrate")
                        run(*compose, "down")
                    # A broken pending migration must roll back and block the
                    # new application, while preserving existing user data.
                    if args.variant == "release":
                        break
                    broken = directory / "29991231000000_install_failure_probe.sql"
                    broken.write_text(
                        "CREATE TABLE public.install_failure_probe(id integer);\nSELECT 1/0;\n"
                    )
                    override = directory / "broken.json"
                    override.write_text(
                        json.dumps(
                            {
                                "services": {
                                    "migrate": {
                                        "volumes": [
                                            f"{broken}:/app/supabase/migrations/{broken.name}:ro"
                                        ]
                                    }
                                }
                            }
                        )
                    )
                    rejected = subprocess.run(
                        [
                            *compose,
                            "-f",
                            str(override),
                            "up",
                            "--detach",
                            "--wait",
                            "--wait-timeout",
                            "120",
                            "api",
                            "web",
                        ],
                        check=False,
                        cwd=ROOT,
                        timeout=240,
                    )
                    if rejected.returncode == 0:
                        raise RuntimeError(
                            "Broken migration incorrectly allowed startup"
                        )
                    failure_log = subprocess.check_output(
                        [*compose, "logs", "--no-color", "migrate"],
                        text=True,
                        timeout=30,
                    )
                    (args.artifacts / "rejected-migration.log").write_text(failure_log)
                    if (
                        broken.name not in failure_log
                        or "SQLSTATE 22012" not in failure_log
                    ):
                        raise RuntimeError(
                            "Startup failed before executing the intended migration probe"
                        )
                    running = subprocess.check_output(
                        [*compose, "ps", "--status", "running", "--services"],
                        text=True,
                        timeout=30,
                    ).splitlines()
                    if {"api", "web"} & set(running):
                        raise RuntimeError(
                            "Application started after migration failure"
                        )
                    proof = subprocess.check_output(
                        [
                            *compose,
                            "exec",
                            "-T",
                            "db",
                            "psql",
                            "-U",
                            "postgres",
                            "-d",
                            "postgres",
                            "-XAt",
                            "-c",
                            "SELECT to_regclass('public.install_failure_probe') IS NULL AND NOT EXISTS (SELECT FROM supabase_migrations.schema_migrations WHERE version='29991231000000');",
                        ],
                        text=True,
                        timeout=30,
                    ).strip()
                    if proof != "t":
                        raise RuntimeError(
                            "Failed migration left DDL or a success receipt"
                        )
                    run(*compose, "down")
                    run(*compose, "up", "--detach", "--wait", "--wait-timeout", "420")
                run(
                    "npx",
                    "--prefix",
                    "e2e",
                    "playwright",
                    "test",
                    "--config=e2e/install/playwright.config.mjs",
                    env={
                        **test_env,
                        "INSTALL_PHASE": phase,
                        "INSTALL_UPGRADED": "true"
                        if args.variant == "upgrade"
                        else "false",
                        "INSTALL_REPORT": str(
                            args.artifacts.resolve() / f"{phase}.json"
                        ),
                    },
                )
            (args.artifacts / "result.json").write_text(
                json.dumps(
                    {
                        "variant": args.variant,
                        "result": "passed",
                        "schema_source": "supabase/migrations",
                        "restart_verified": True,
                        "upgrade_from": args.upgrade_base or UPGRADE_BASE
                        if args.variant in {"upgrade", "release"}
                        else None,
                    }
                )
                + "\n"
            )
        finally:
            with (args.artifacts / "compose.log").open("w") as log:
                subprocess.run(
                    [*compose, "logs", "--no-color", "--tail", "150"],
                    stdout=log,
                    stderr=log,
                    check=False,
                    timeout=60,
                )
            subprocess.run(
                [*compose, "down", "--volumes", "--remove-orphans"],
                check=True,
                timeout=120,
            )


if __name__ == "__main__":
    main()
