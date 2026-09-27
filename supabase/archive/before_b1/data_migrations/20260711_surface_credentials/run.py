"""Move legacy agent/sandbox config credentials into the hashed credential table.

Stable keyset pagination keeps memory bounded. Each row is processed in the
safe order: persist/confirm hash state, then remove plaintext config. Re-running
is safe.

This file is an immutable legacy artifact. Run it through `puppyone-db`; direct
execution remains available only for local diagnosis.

Usage:
    python run.py
    python run.py --apply
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sys

from supabase import create_client

HASH_ALG = "hmac_sha256_v1"


def _client():
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_KEY", "").strip()
    if not url or not key:
        raise ValueError("SUPABASE_URL and SUPABASE_KEY are required")
    return create_client(url, key)


def _access_token_hash(raw_token: str) -> str:
    secret = os.environ.get("ACCESS_CREDENTIAL_HASH_SECRET", "")
    if not secret:
        raise ValueError("ACCESS_CREDENTIAL_HASH_SECRET is required")
    if secret == "ContextBase-access-credential-development-secret":
        raise ValueError("the development credential hash secret is forbidden")
    return hmac.new(
        secret.encode(),
        raw_token.strip().encode(),
        hashlib.sha256,
    ).hexdigest()


def _batch_size() -> int:
    value = int(os.environ.get("DATA_MIGRATION_BATCH_SIZE", "500"))
    if not 1 <= value <= 100000:
        raise ValueError("DATA_MIGRATION_BATCH_SIZE must be between 1 and 100000")
    return value


def _metadata(raw_token: str) -> tuple[str, str]:
    token = raw_token.strip()
    prefix = token.split("_", 1)[0] if "_" in token else token[:3]
    return prefix or "key", token[-4:] if len(token) >= 4 else token


def _active_credential(client, surface_id: str) -> dict | None:
    query = (
        client.table("access_surface_credentials")
        .select("id, key_hash, workspace_binding_id")
        .eq("access_surface_id", surface_id)
        .eq("credential_type", "bearer_token")
        .eq("status", "active")
        .is_("workspace_binding_id", "null")
        .order("created_at", desc=True)
        .limit(1)
    )
    try:
        rows = query.execute().data or []
    except Exception as error:
        # The legacy backfill normally runs before the 20260712 binding column
        # exists. Only that exact pre-expand schema response may use the old
        # column set; auth/network/other schema failures remain fatal.
        message = str(error).lower()
        if (
            getattr(error, "code", None) not in {"PGRST204", "42703"}
            or "workspace_binding_id" not in message
            or "access_surface_credentials" not in message
        ):
            raise
        rows = (
            client.table("access_surface_credentials")
            .select("id, key_hash")
            .eq("access_surface_id", surface_id)
            .eq("credential_type", "bearer_token")
            .eq("status", "active")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        ).data or []
    return rows[0] if rows else None


def _surface_page(client, *, after_id: str | None, page_size: int) -> list[dict]:
    query = (
        client.table("access_surfaces")
        .select("id, org_id, project_id, kind, config, created_by")
        .in_("kind", ["agent", "sandbox"])
        .order("id")
        .limit(page_size)
    )
    if after_id is not None:
        query = query.gt("id", after_id)
    return query.execute().data or []


def backfill(*, apply: bool) -> None:
    client = _client()
    scanned = migrated = cleaned = 0
    after_id: str | None = None
    page_size = _batch_size()
    while True:
        page = _surface_page(client, after_id=after_id, page_size=page_size)
        if not page:
            break

        for row in page:
            config = dict(row.get("config") or {})
            candidate_fields = (
                ("mcp_api_key", "api_key")
                if row.get("kind") == "agent"
                else ("access_key", "api_key")
            )
            tokens = {str(config[field]).strip() for field in candidate_fields if config.get(field)}
            if not tokens:
                continue
            if len(tokens) != 1:
                raise ValueError(
                    f"surface {row['id']} contains multiple distinct legacy credentials"
                )
            raw_token = tokens.pop()
            scanned += 1

            active = _active_credential(client, row["id"])
            if active is None:
                if apply:
                    prefix, last4 = _metadata(raw_token)
                    client.table("access_surface_credentials").insert(
                        {
                            "org_id": row.get("org_id"),
                            "project_id": row["project_id"],
                            "access_surface_id": row["id"],
                            "credential_type": "bearer_token",
                            "key_prefix": prefix,
                            "key_last4": last4,
                            "key_hash": _access_token_hash(raw_token),
                            "hash_alg": HASH_ALG,
                            "status": "active",
                            "created_by": row.get("created_by"),
                        }
                    ).execute()
                migrated += 1
            elif active.get("key_hash") != _access_token_hash(raw_token):
                # A newer credential already exists. The config token is stale and
                # must not replace or reactivate it; only remove the plaintext.
                pass

            for field in ("access_key", "mcp_api_key", "api_key"):
                config.pop(field, None)
            if apply:
                client.table("access_surfaces").update({"config": config}).eq(
                    "id", row["id"]
                ).execute()
            cleaned += 1

        after_id = str(page[-1]["id"])

    verb = "migrated" if apply else "would migrate"
    print(f"[backfill] scanned={scanned} {verb}={migrated} cleaned={cleaned}")
    if not apply:
        print("[backfill] dry run — re-run with --apply to persist hashes and remove plaintext")


if __name__ == "__main__":
    backfill(apply="--apply" in sys.argv[1:])
