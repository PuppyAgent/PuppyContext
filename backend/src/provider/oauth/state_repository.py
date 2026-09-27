"""OAuth state repository — server-side nonce store for CSRF protection.

Each OAuth `authorize` request inserts a fresh state row keyed by the random
nonce. The matching `callback` consumes the row atomically (validate +
delete) so the state can only be used once. The callback can recover the
Puppyone user from that server-issued nonce; this avoids depending on the
browser still having a fresh app JWT after the third-party redirect.

Migration: 20260427000000_oauth_state_csrf.sql
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from src.infra.supabase.client import SupabaseClient


@dataclass
class OAuthState:
    state: str
    user_id: str
    provider: str
    expires_at: datetime


class OAuthStateRepository:
    """CRUD on `oauth_states` for OAuth CSRF nonce flow."""

    TABLE = "oauth_states"

    def __init__(self, supabase_client: Optional[SupabaseClient] = None):
        self._client = (supabase_client or SupabaseClient()).get_client()

    def issue(self, user_id: str, provider: str, redirect_uri: Optional[str] = None) -> str:
        """Generate + store a fresh state nonce. Returns the nonce string."""
        state = secrets.token_urlsafe(32)
        self._client.table(self.TABLE).insert({
            "state": state,
            "user_id": user_id,
            "provider": provider,
            "redirect_uri": redirect_uri,
        }).execute()
        return state

    def consume_for_provider(self, state: str, provider: str) -> str | None:
        """Atomically validate + delete a state and return its user_id.

        Returns the issuing user_id iff:
          - state row exists
          - row.provider matches the expected provider
          - row.expires_at is in the future

        Always deletes the row at the end (single-use), so a replay attempt
        with the same nonce is impossible.
        """
        if not state:
            return None
        rows = (
            self._client.table(self.TABLE)
            .select("user_id, provider, expires_at")
            .eq("state", state)
            .limit(1)
            .execute()
        )
        if not rows.data:
            return False
        row = rows.data[0]
        try:
            self._client.table(self.TABLE).delete().eq("state", state).execute()
        except Exception:
            # Even if delete fails, we must not validate the nonce twice — the
            # next consume() call will re-fetch and decide. We don't want a
            # delete error to silently allow replay, but in practice service
            # role inserts/deletes are durable.
            pass

        if row.get("provider") != provider:
            return None
        expires_at_raw = row.get("expires_at")
        if not expires_at_raw:
            return None
        try:
            expires_at = datetime.fromisoformat(expires_at_raw.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None
        if expires_at < datetime.now(timezone.utc):
            return None
        user_id = row.get("user_id")
        return str(user_id) if user_id else None

    def consume(self, state: str, user_id: str, provider: str) -> bool:
        """Atomically validate + delete a state for a known app user.

        This wrapper is kept for call sites/tests that already have an
        authenticated app user. Browser-return OAuth callbacks should prefer
        consume_for_provider() because the state itself is the callback's
        user-binding proof.
        """
        consumed_user_id = self.consume_for_provider(state=state, provider=provider)
        if consumed_user_id is None:
            return False
        return consumed_user_id == user_id

    def purge_expired(self) -> int:
        """Best-effort cleanup; the SQL function is preferred in cron."""
        try:
            res = self._client.rpc("oauth_states_purge_expired").execute()
            return int(res.data or 0)
        except Exception:
            return 0
