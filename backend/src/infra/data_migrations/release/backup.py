"""Create and restore-test a logical backup in an owned disposable container."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import time
from pathlib import Path

from ..database import PsqlClient

POSTGRES_IMAGE = "supabase/postgres:17.6.1.064"


def create_backup(db: PsqlClient, directory: Path) -> dict:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = directory / ("before-upgrade-" + secrets.token_hex(8) + ".dump")
    # pg_dump --schema does not include CREATE EXTENSION automatically. Include
    # the actual extensions used in the backed-up namespaces, so the archive
    # can restore functions/types/defaults into a new database.
    extensions = db.scalar(
        "SELECT e.extname FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace "
        "WHERE n.nspname IN ('public','auth','extensions') ORDER BY e.extname"
    ).splitlines()
    extension_args = [arg for name in extensions for arg in ("--extension", name)]
    # Object ACLs can reference roles created by Supabase after image startup.
    # Preserve names only: the isolated restore receives NOLOGIN placeholders,
    # never passwords, login privileges or another service's role definitions.
    roles = json.loads(
        db.scalar(
            "SELECT coalesce(jsonb_agg(rolname),'[]') FROM pg_roles "
            "WHERE rolname !~ '^pg_' AND rolname<>'postgres'"
        )
    )
    roles_path = destination.with_suffix(".roles.json")
    roles_path.touch(mode=0o600)
    roles_path.write_text(json.dumps(roles))
    # Do not include transient Supabase/PuppyPay-owned schemas in PuppyOne's
    # application recovery point. Auth identity and public data are preserved.
    with destination.open("xb") as output:
        os.chmod(destination, 0o600)
        result = subprocess.run(
            [
                "pg_dump",
                "--format=custom",
                "--schema=public",
                "--schema=auth",
                "--schema=extensions",
                "--schema=supabase_migrations",
                "--schema=puppyone_release",
                "--no-owner",
                "--no-publications",
                "--no-subscriptions",
                *extension_args,
            ],
            env=db.environment,
            stdout=output,
            stderr=subprocess.PIPE,
            timeout=1800,
        )
    if result.returncode:
        raise RuntimeError("Logical backup failed")
    with destination.open("rb") as source:
        checksum = hashlib.file_digest(source, "sha256").hexdigest()
    container = "puppyone-release-restore-" + secrets.token_hex(8)
    restore_password = secrets.token_hex(32)
    subprocess.run(
        [
            "docker",
            "run",
            "--detach",
            "--name",
            container,
            "--network=none",
            "-e",
            "POSTGRES_HOST_AUTH_METHOD=trust",
            "-e",
            "POSTGRES_PASSWORD",
            "-e",
            "PGPASSWORD",
            POSTGRES_IMAGE,
        ],
        env={**os.environ, "POSTGRES_PASSWORD": restore_password, "PGPASSWORD": restore_password},
        check=True,
        capture_output=True,
        timeout=180,
    )
    try:
        for _ in range(90):
            ready = subprocess.run(
                ["docker", "exec", container, "pg_isready", "-U", "supabase_admin"],
                capture_output=True,
            )
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            diagnostic = directory / "restore-startup.log"
            diagnostic.touch(mode=0o600, exist_ok=True)
            diagnostic.write_bytes(
                subprocess.run(["docker", "logs", container], capture_output=True).stderr
            )
            raise RuntimeError("Backup verification database did not start")
        subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                container,
                "psql",
                "-w",
                "-U",
                "supabase_admin",
                "-d",
                "postgres",
                "-v",
                "ON_ERROR_STOP=1",
                "-v",
                "roles=" + json.dumps(roles),
            ],
            input="SELECT format('CREATE ROLE %I NOLOGIN', role) FROM jsonb_array_elements_text(:'roles'::jsonb) role "
            "WHERE NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=role)\\gexec\n",
            text=True,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "docker",
                "exec",
                container,
                "createdb",
                "-w",
                "-U",
                "supabase_admin",
                "-T",
                "template0",
                "release_restore",
            ],
            check=True,
            capture_output=True,
        )
        # template0 contains the default empty public schema; the archive owns
        # its definition. This command only targets our newly created database.
        subprocess.run(
            [
                "docker",
                "exec",
                container,
                "psql",
                "-w",
                "-U",
                "supabase_admin",
                "-d",
                "release_restore",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                "DROP SCHEMA public",
            ],
            check=True,
            capture_output=True,
        )
        with destination.open("rb") as source:
            restored = subprocess.run(
                [
                    "docker",
                    "exec",
                    "-i",
                    container,
                    "pg_restore",
                    "-w",
                    "-U",
                    "supabase_admin",
                    "-d",
                    "release_restore",
                    "--exit-on-error",
                ],
                stdin=source,
                capture_output=True,
                timeout=1800,
            )
        if restored.returncode:
            diagnostic = directory / "restore-error.log"
            diagnostic.touch(mode=0o600, exist_ok=True)
            diagnostic.write_bytes(restored.stderr)
            raise RuntimeError("Logical backup could not be restored")
    finally:
        subprocess.run(
            ["docker", "rm", "--force", "--volumes", container],
            check=True,
            capture_output=True,
            timeout=60,
        )
    return {
        "restore_roles_ref": str(roles_path),
        "restore_point_ref": str(destination),
        "sha256": checksum,
        "bytes": destination.stat().st_size,
        "restore_verified": True,
    }
