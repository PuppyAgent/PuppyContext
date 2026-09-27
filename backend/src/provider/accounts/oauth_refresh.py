"""Per-provider OAuth refresh dispatcher for gateway credentials.

A ``gateways`` row stores the OAuth ``access_token`` + ``refresh_token``
inside its ``credentials`` JSONB column. When the access token expires,
``GatewayService.refresh_token`` calls into here to hit the provider's
token endpoint and obtain a fresh access token.

The provider config table below mirrors the per-provider OAuth services
in ``provider/oauth/`` — same token URLs, same client_id /
client_secret settings, same param/body convention. Keeping the
dispatcher centralized (instead of touching nine service files to add a
new method) avoids spreading a half-duplicated refresh path.

Returns ``None`` if the provider isn't OAuth-backed or the refresh
attempt failed; callers MUST surface a useful error message rather
than silently swallowing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from src.config import settings
from src.utils.logger import log_error, log_warning


# ── Provider configuration ───────────────────────────────────────

# Each entry describes how to POST a refresh-token grant to the
# provider's token endpoint:
#   - token_url:    full URL to POST to.
#   - client_id:    OAuth app's public client_id.
#   - client_secret: OAuth app's confidential client_secret. May be
#                   ``""`` for providers that accept client_id only
#                   (none currently).
#   - body_format:  ``"form"`` or ``"json"`` — most providers want
#                   ``application/x-www-form-urlencoded``; GitHub
#                   accepts JSON.
#
# A provider is omitted from this table iff it doesn't issue refresh
# tokens (e.g. Notion's bearer token has no expiry on the workspace
# install). Calling ``refresh_gateway_token`` for an omitted provider
# returns ``None`` and the gateway endpoint surfaces a 400 to the UI
# telling the user there's nothing to refresh.
_PROVIDER_CONFIG: dict[str, dict[str, Any]] = {
    "github": {
        "token_url": "https://github.com/login/oauth/access_token",
        "client_id_setting": "GITHUB_CLIENT_ID",
        "client_secret_setting": "GITHUB_CLIENT_SECRET",
        "body_format": "json",
    },
    "linear": {
        "token_url": "https://api.linear.app/oauth/token",
        "client_id_setting": "LINEAR_CLIENT_ID",
        "client_secret_setting": "LINEAR_CLIENT_SECRET",
        "body_format": "form",
    },
    "airtable": {
        "token_url": "https://airtable.com/oauth2/v1/token",
        "client_id_setting": "AIRTABLE_CLIENT_ID",
        "client_secret_setting": "AIRTABLE_CLIENT_SECRET",
        "body_format": "form",
    },
}

# Every Google product shares one token URL and client_id pair. Add a
# row per provider slug so the dispatcher can be a flat O(1) lookup
# rather than a prefix-matching ladder.
_GOOGLE_CONFIG: dict[str, Any] = {
    "token_url": "https://oauth2.googleapis.com/token",
    "client_id_setting": "GOOGLE_CLIENT_ID",
    "client_secret_setting": "GOOGLE_CLIENT_SECRET",
    "body_format": "form",
}
for _slug in (
    "gmail", "google_drive", "google_docs", "google_sheets",
    "google_calendar", "google_search_console",
):
    _PROVIDER_CONFIG[_slug] = _GOOGLE_CONFIG


# ── Dispatcher ───────────────────────────────────────────────────


async def refresh_gateway_token(provider: str, refresh_token: str) -> dict | None:
    """POST a refresh_token grant to the provider and return the new
    credentials dict, or ``None`` if the provider isn't supported or
    the call failed.

    Successful return shape (keys present iff the provider returned them):

        {
            "access_token": str,
            "refresh_token": str,         # always set — falls back to
                                          # the input value when the
                                          # provider re-issues only the
                                          # access token (Google's
                                          # offline-access default).
            "token_type": str | None,
            "expires_at": str | None,     # ISO-8601 UTC, derived from
                                          # ``expires_in`` so callers
                                          # don't have to do clock
                                          # math themselves.
            "scope": str | None,
        }

    Failures (unsupported provider, missing client credentials, HTTP
    error, malformed response) are logged and surfaced as ``None``.
    The gateway service raises a clean error to the caller; we never
    re-throw HTTPX exceptions across the boundary.
    """
    if not refresh_token:
        return None

    cfg = _PROVIDER_CONFIG.get(provider)
    if not cfg:
        log_warning(f"[GatewayRefresh] No refresh config for provider={provider}")
        return None

    client_id = getattr(settings, cfg["client_id_setting"], "") or ""
    client_secret = getattr(settings, cfg["client_secret_setting"], "") or ""
    if not client_id or not client_secret:
        log_warning(
            f"[GatewayRefresh] OAuth client credentials not configured for "
            f"provider={provider}"
        )
        return None

    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            if cfg["body_format"] == "json":
                response = await client.post(
                    cfg["token_url"],
                    json=payload,
                    headers={"Accept": "application/json"},
                )
            else:
                response = await client.post(
                    cfg["token_url"],
                    data=payload,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
            response.raise_for_status()
            token_data = response.json()
    except httpx.HTTPStatusError as exc:
        log_error(
            f"[GatewayRefresh] {provider} refresh returned "
            f"{exc.response.status_code}: {exc.response.text[:200]}"
        )
        return None
    except (httpx.HTTPError, ValueError) as exc:
        # ValueError catches malformed JSON bodies that ``response.json()``
        # raises before we'd see them as HTTP errors.
        log_error(f"[GatewayRefresh] {provider} refresh failed: {exc}")
        return None

    access_token = token_data.get("access_token")
    if not access_token:
        log_error(
            f"[GatewayRefresh] {provider} refresh succeeded but no "
            f"access_token in response keys={list(token_data)}"
        )
        return None

    expires_at: str | None = None
    expires_in = token_data.get("expires_in")
    if expires_in:
        try:
            expires_at = (
                datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
            ).isoformat()
        except (TypeError, ValueError):
            expires_at = None

    return {
        "access_token": access_token,
        # Providers that don't re-issue a refresh_token (Google's offline
        # access flow is the canonical example) leave the existing one in
        # place. Persisting the prior value here keeps the gateway usable
        # for future refreshes.
        "refresh_token": token_data.get("refresh_token") or refresh_token,
        "token_type": token_data.get("token_type"),
        "expires_at": expires_at,
        "scope": token_data.get("scope"),
    }
