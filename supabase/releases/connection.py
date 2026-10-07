"""Bind CI to one Supabase project; use expiring official CLI credentials if needed."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from urllib.parse import parse_qs, quote, unquote, urlsplit
from urllib.request import Request, urlopen

# Public deployment identity, not a credential. A mislabeled staging secret
# must never direct Qubits schema or data jobs at the production database.
STAGING_PROJECT_REF = "qextonmjqbhxgokmjbio"


def connection_environment(source: dict[str, str]) -> dict[str, str]:
    ref = source.get("SUPABASE_PROJECT_ID", "")
    if not re.fullmatch(r"[a-z]{20}", ref):
        raise ValueError("A protected Supabase project ref is required")
    if source.get("TARGET_ENVIRONMENT") == "staging" and ref != STAGING_PROJECT_REF:
        raise ValueError("Staging connection does not match the Qubits test project")
    uri = source.get("DATABASE_URL", "")
    temporary = not uri or source.get("DATABASE_TEMPORARY") == "1"
    expires_at = float(source.get("DATABASE_CREDENTIAL_EXPIRES_AT", "0"))
    if not uri or (temporary and expires_at < time.time() + 90):
        token = source.get("SUPABASE_ACCESS_TOKEN", "")
        if not token:
            raise ValueError(
                "Supabase management token is required for temporary login"
            )

        def request(suffix: str, payload: dict | None = None):
            req = Request(
                f"https://api.supabase.com/v1/projects/{ref}/{suffix}",
                data=json.dumps(payload).encode() if payload is not None else None,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
            with urlopen(req, timeout=30) as response:
                return json.load(response)

        pools = request("config/database/pooler")
        pool = next(p for p in pools if p.get("database_type") == "PRIMARY")
        host = (
            pool.get("db_host") or urlsplit(pool.get("connection_string", "")).hostname
        )
        if not host or not host.endswith(".pooler.supabase.com"):
            raise ValueError("Unrecognized official session pooler")
        login = request("cli/login-role", {"read_only": False})
        role = login.get("role", "")
        if not re.fullmatch(r"cli_login_[a-zA-Z0-9_]+", role) or not login.get(
            "password"
        ):
            raise ValueError("Unexpected expiring CLI role")
        expires_at = time.time() + login["ttl_seconds"]
        uri = (
            f"postgresql://{quote(role + '.' + ref, safe='')}:"
            f"{quote(login['password'], safe='')}@{host}:5432/postgres?sslmode=require"
        )
    parsed = urlsplit(uri)
    username = unquote(parsed.username or "")
    direct = parsed.hostname == f"db.{ref}.supabase.co"
    pooler = bool(
        parsed.hostname
        and parsed.hostname.endswith(".pooler.supabase.com")
        and username.endswith("." + ref)
    )
    if (
        parsed.scheme not in {"postgres", "postgresql"}
        or parsed.port not in {None, 5432}
        or not (direct or pooler)
    ):
        raise ValueError("Database connection does not match protected project")
    database = unquote(parsed.path.lstrip("/"))
    if not database:
        raise ValueError("Database name is required")
    query = parse_qs(parsed.query)
    result = {
        "DATABASE_URL": uri,
        "DATA_MIGRATION_DATABASE_URL": uri,
        "DATABASE_TEMPORARY": "1" if temporary else "0",
        "DATABASE_CREDENTIAL_EXPIRES_AT": str(expires_at),
        "SUPABASE_URL": f"https://{ref}.supabase.co",
        "PGHOST": parsed.hostname,
        "PGPORT": "5432",
        "PGUSER": username,
        "PGPASSWORD": unquote(parsed.password or ""),
        "PGDATABASE": database,
        "PGSSLMODE": query.get("sslmode", ["require"])[-1],
        "PGCONNECT_TIMEOUT": "15",
        "PGOPTIONS": "-c role=postgres" if temporary else source.get("PGOPTIONS", ""),
        "SUPABASE_DATABASE_URL": (
            f"postgresql://{quote(username, safe='')}@{parsed.hostname}:5432/"
            f"{quote(database, safe='')}?sslmode=require"
        ),
    }
    if any("\n" in value or "\r" in value for value in result.values()):
        raise ValueError("Invalid connection value")
    return result


def main():
    try:
        values = connection_environment(dict(os.environ))
        target = os.environ["GITHUB_ENV"]
        for name in ("PGPASSWORD", "DATABASE_URL", "DATA_MIGRATION_DATABASE_URL"):
            if values[name]:
                print("::add-mask::" + values[name])
        with open(target, "a") as output:
            for name, value in values.items():
                output.write(f"{name}={value}\n")
        print("Protected Supabase connection prepared; credentials withheld.")
    except Exception as error:  # noqa: BLE001 - prevent credentials in network errors
        print(
            f"Connection preparation failed ({type(error).__name__}); no secrets printed",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
