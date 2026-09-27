"""Backfill repo_scopes.access_key_hash from plaintext access_key (ISSUE-003).

Idempotent: only touches rows that have an access_key but no access_key_hash.
Stable keyset pagination keeps memory bounded and cannot skip rows as the
filtered candidate set shrinks.
Run AFTER migration 20260704000000_repo_scopes_access_key_hash.sql. Once
20260711070000 has dropped those legacy columns, it exits successfully as a
no-op so later migration deployments remain repeatable.

This file is an immutable legacy artifact. Run it through `puppyone-db`; direct
execution remains available only for local diagnosis.

Usage:
    python run.py            # dry run
    python run.py --apply    # write hashes
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sys

from supabase import create_client


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


def _legacy_columns_have_been_retired(error: Exception) -> bool:
    """Only treat the expected PostgREST missing-column response as a no-op.

    After the destructive migration has succeeded, a legacy reconciliation run
    can query its dropped columns and receive PostgREST's PGRST204 or
    PostgreSQL's 42703 (depending on the API path). Do not hide network, auth,
    or other database errors: they must still fail the deployment.
    """
    message = str(error).lower()
    return (
        getattr(error, "code", None) in {"PGRST204", "42703"}
        and "repo_scopes" in message
        and ("access_key" in message or "access_key_hash" in message)
    )


def _pending_page(
    client,
    *,
    after_id: str | None,
    page_size: int,
) -> list[dict] | None:
    """Return one stable page, or ``None`` after the columns are retired."""
    try:
        query = (
            client.table("repo_scopes")
            .select("id, access_key, access_key_hash")
            .not_.is_("access_key", "null")
            .is_("access_key_hash", "null")
            .order("id")
            .limit(page_size)
        )
        if after_id is not None:
            query = query.gt("id", after_id)
        return query.execute().data or []
    except Exception as error:
        if _legacy_columns_have_been_retired(error):
            return None
        raise


def backfill(apply: bool) -> None:
    client = _client()
    scanned = updated = 0
    after_id: str | None = None
    page_size = _batch_size()
    while True:
        pending = _pending_page(
            client,
            after_id=after_id,
            page_size=page_size,
        )
        if pending is None:
            print("[backfill] legacy repo_scopes credential columns already retired; skipping")
            return
        if not pending:
            break

        for row in pending:
            scanned += 1
            key = row.get("access_key")
            if not key:
                continue
            digest = _access_token_hash(key)
            if apply:
                client.table("repo_scopes").update({"access_key_hash": digest}).eq(
                    "id", row["id"]
                ).execute()
            updated += 1
        after_id = str(pending[-1]["id"])

    verb = "updated" if apply else "would update"
    print(f"[backfill] scanned={scanned} {verb}={updated}")
    if not apply:
        print("[backfill] dry run — re-run with --apply to write hashes")


if __name__ == "__main__":
    backfill(apply="--apply" in sys.argv[1:])
