"""Migrate legacy Access secrets/policies out of access_surfaces.config.

Run after applying:
  supabase/archive/before_b1/migrations/20260616003000_access_surface_credentials_policies.sql

This script intentionally does not preserve runtime compatibility with legacy
config keys. After it runs, runtime code should read credentials/policies from
the new tables only.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.infra.supabase.dependencies import get_supabase_client
from src.repo.access_credentials import access_token_hash, access_token_metadata, HASH_ALG


CREDENTIALS_TABLE = "access_surface_credentials"
POLICIES_TABLE = "access_surface_policies"


def _legacy_key_for(surface: dict[str, Any]) -> tuple[str, str] | None:
    config = surface.get("config") or {}
    kind = surface.get("kind")
    if kind == "mcp":
        token = config.get("api_key")
        return ("bearer_token", token) if token else None
    if kind == "agent":
        token = config.get("mcp_api_key") or config.get("access_key")
        return ("bearer_token", token) if token else None
    if kind == "sandbox":
        token = config.get("access_key")
        return ("bearer_token", token) if token else None
    return None


def _policy_for(surface: dict[str, Any]) -> dict[str, Any]:
    config = surface.get("config") or {}
    kind = surface.get("kind")
    if kind == "mcp":
        return {
            "access_surface_id": surface["id"],
            "version": 1,
            "fs_policy": {"accesses": config.get("accesses") or []},
            "tools_policy": config.get("tools_config") or {},
            "shell_policy": {"enabled": False},
            "network_policy": {},
        }
    return {
        "access_surface_id": surface["id"],
        "version": 1,
        "fs_policy": config.get("fs_policy") or {},
        "tools_policy": config.get("tools_config") or {},
        "shell_policy": config.get("shell_policy") or {},
        "network_policy": config.get("network_policy") or {},
    }


def _clean_config(config: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(config)
    for key in (
        "api_key",
        "mcp_api_key",
        "access_key",
        "tools_config",
        "accesses",
        "fs_policy",
        "shell_policy",
        "network_policy",
    ):
        cleaned.pop(key, None)
    return cleaned


def main() -> None:
    sb = get_supabase_client()
    surfaces = (
        sb.table("access_surfaces")
        .select("*")
        .in_("kind", ["mcp", "agent", "sandbox"])
        .execute()
        .data
        or []
    )

    migrated_credentials = 0
    migrated_policies = 0
    cleaned_surfaces = 0

    for surface in surfaces:
        legacy_key = _legacy_key_for(surface)
        if legacy_key:
            credential_type, token = legacy_key
            key_prefix, key_last4 = access_token_metadata(token)
            key_hash = access_token_hash(token)
            existing = (
                sb.table(CREDENTIALS_TABLE)
                .select("id")
                .eq("key_hash", key_hash)
                .limit(1)
                .execute()
                .data
                or []
            )
            if not existing:
                sb.table(CREDENTIALS_TABLE).insert({
                    "org_id": surface.get("org_id"),
                    "project_id": surface["project_id"],
                    "access_surface_id": surface["id"],
                    "credential_type": credential_type,
                    "key_prefix": key_prefix,
                    "key_last4": key_last4,
                    "key_hash": key_hash,
                    "hash_alg": HASH_ALG,
                    "status": "active",
                    "created_by": surface.get("created_by"),
                }).execute()
                migrated_credentials += 1

        sb.table(POLICIES_TABLE).upsert(
            _policy_for(surface),
            on_conflict="access_surface_id",
        ).execute()
        migrated_policies += 1

        config = surface.get("config") or {}
        cleaned = _clean_config(config)
        if cleaned != config:
            sb.table("access_surfaces").update({
                "config": cleaned,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", surface["id"]).execute()
            cleaned_surfaces += 1

    print(
        "migrated access surfaces:",
        {
            "credentials": migrated_credentials,
            "policies": migrated_policies,
            "cleaned_configs": cleaned_surfaces,
        },
    )


if __name__ == "__main__":
    main()
