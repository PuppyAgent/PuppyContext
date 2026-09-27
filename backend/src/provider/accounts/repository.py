"""Gateway repository — Supabase CRUD for the gateways table."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from src.infra.supabase.client import SupabaseClient


class GatewayRepository:
    TABLE = "gateways"

    def __init__(self, client: SupabaseClient | None = None):
        self._sb = client or SupabaseClient()

    @property
    def _client(self):
        return self._sb.client

    # ── Create ──

    def create(self, *, org_id: str, user_id: str, provider: str,
               name: str | None = None, credentials: dict | None = None,
               metadata: dict | None = None) -> dict:
        data = {
            "org_id": org_id,
            "user_id": user_id,
            "provider": provider,
            "name": name or provider,
            "credentials": credentials or {},
            "metadata": metadata or {},
        }
        resp = self._client.table(self.TABLE).insert(data).execute()
        return resp.data[0] if resp.data else {}

    # ── Read ──

    def get_by_id(self, gateway_id: str) -> dict | None:
        resp = (
            self._client.table(self.TABLE)
            .select("*")
            .eq("id", gateway_id)
            .limit(1)
            .execute()
        )
        return resp.data[0] if resp.data else None

    def list_by_org(self, org_id: str, *, provider: str | None = None) -> list[dict]:
        q = self._client.table(self.TABLE).select("*").eq("org_id", org_id)
        if provider:
            q = q.eq("provider", provider)
        resp = q.order("created_at", desc=True).execute()
        return resp.data or []

    def list_by_user(self, user_id: str, *, provider: str | None = None) -> list[dict]:
        q = self._client.table(self.TABLE).select("*").eq("user_id", user_id)
        if provider:
            q = q.eq("provider", provider)
        resp = q.order("created_at", desc=True).execute()
        return resp.data or []

    # ── Update ──

    def update(self, gateway_id: str, fields: dict[str, Any]) -> dict | None:
        fields["updated_at"] = datetime.now(UTC).isoformat()
        resp = (
            self._client.table(self.TABLE)
            .update(fields)
            .eq("id", gateway_id)
            .execute()
        )
        return resp.data[0] if resp.data else None

    def update_credentials(self, gateway_id: str, credentials: dict) -> dict | None:
        return self.update(gateway_id, {"credentials": credentials})

    # ── Delete ──

    def delete(self, gateway_id: str) -> bool:
        resp = (
            self._client.table(self.TABLE)
            .delete()
            .eq("id", gateway_id)
            .execute()
        )
        return bool(resp.data)

    # ── Helpers ──

    def count_connectors(self, gateway_id: str) -> int:
        """Count how many durable connections reference this gateway id."""
        resp = (
            self._client.table("connections")
            .select("id", count="exact")
            .filter("config->>gateway_id", "eq", gateway_id)
            .execute()
        )
        return resp.count or 0
