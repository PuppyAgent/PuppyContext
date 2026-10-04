"""Supplementary real PostgreSQL 17; NOT Supabase or S3 acceptance.

Owns a new loopback cluster and never accepts an existing database URL. Like
scripts/test_entrypoint_migration_native.py, supplies only auth schema stubs and
omits exactly the three Supabase-only extension declarations. Product schema
and subsequent migrations are otherwise applied unchanged.
"""

from __future__ import annotations

import re
import shutil
import socket
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def run(*args, input=None):
    return subprocess.run(
        list(map(str, args)), input=input, check=True, text=True,
        capture_output=True, timeout=120,
    ).stdout


def product_migration_sql(migration: Path) -> str:
    """Only omit the three unavailable Supabase extension declarations."""
    body = migration.read_text()
    if migration.name == "20260926000000_baseline_b1.sql":
        body, count = re.subn(
            r"^CREATE EXTENSION IF NOT EXISTS (pg_graphql|pg_net|supabase_vault).*?;\n",
            "", body, flags=re.MULTILINE,
        )
        if count != 3:
            raise RuntimeError("Supabase extension inventory changed; review native test setup")
    return body


@contextmanager
def native_postgres():
    executable = shutil.which("postgres") or "/opt/homebrew/opt/postgresql@17/bin/postgres"
    binaries = Path(executable).parent
    version = run(binaries / "postgres", "--version").strip()
    if not re.search(r"\b17\.", version):
        raise RuntimeError("Native hosting tests require PostgreSQL 17")
    with tempfile.TemporaryDirectory(prefix="hosting-native-") as temporary:
        root = Path(temporary)
        data = root / "data"
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        run(binaries / "initdb", "-D", data, "-U", "postgres",
            "--auth=trust", "--encoding=UTF8", "--no-locale")
        url = f"postgresql://postgres@127.0.0.1:{port}/postgres"

        def sql(statement):
            return run(binaries / "psql", url, "-X", "-qAt", "-v", "ON_ERROR_STOP=1", input=statement)

        try:
            run(binaries / "pg_ctl", "-D", data, "-l", root / "server.log", "-w",
                "-o", f"-p {port} -h 127.0.0.1 -k {root}", "start")
            sql("CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;")
            sql((ROOT / "scripts/testing/postgres_auth_stub.sql").read_text())
            migrations = sorted((ROOT / "supabase/migrations").glob("*.sql"))
            for migration in migrations:
                sql(product_migration_sql(migration))
            yield {
                "url": url,
                "stack": "puppy-baseline-native-" + root.name,
                "server_version": version,
                "migrations": [path.name for path in migrations],
            }
        finally:
            # A startup timeout can leave our postmaster alive. Its PID file,
            # not a shared port/name, determines whether cleanup is necessary.
            if (data / "postmaster.pid").exists():
                run(binaries / "pg_ctl", "-D", data, "-m", "fast", "-w", "stop")
