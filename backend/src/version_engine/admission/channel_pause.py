"""Channel-level permission gate for admitted scope credentials."""

from __future__ import annotations

import threading
import time

from fastapi import HTTPException

from src.platform.repository_target.auth_context import repository_target_from_auth
from src.platform.repository_target.models import repository_target_scope_id
from src.platform.access.model_repository import AccessModelRepository
from src.utils.logger import log_error, log_warning


# Recognised channel headers. Anything else is silently ignored so that
# unknown / future client kinds don't break authentication. The worst case is
# that pause becomes informational for that client kind, not that a legitimate
# request gets rejected.
_KNOWN_CHANNELS = frozenset({"cli", "git_remote", "mcp"})
_CHANNEL_PAUSE_CACHE_TTL_SECONDS = 2.0
_channel_pause_cache: dict[tuple[str, str], tuple[float, str | None, str | None]] = {}
_channel_pause_cache_lock = threading.Lock()


def clear_channel_pause_cache(scope_id: str | None = None, channel: str | None = None) -> None:
    """Clear cached connector pause state after connector status changes."""

    normalized_channel = (channel or "").strip().lower()
    with _channel_pause_cache_lock:
        if scope_id and normalized_channel:
            _channel_pause_cache.pop((scope_id, normalized_channel), None)
        elif scope_id:
            for key in list(_channel_pause_cache):
                if key[0] == scope_id:
                    _channel_pause_cache.pop(key, None)
        elif normalized_channel:
            for key in list(_channel_pause_cache):
                if key[1] == normalized_channel:
                    _channel_pause_cache.pop(key, None)
        else:
            _channel_pause_cache.clear()


def _get_cached_channel_pause(scope_id: str, channel: str) -> tuple[str | None, str | None] | None:
    now = time.monotonic()
    key = (scope_id, channel)
    with _channel_pause_cache_lock:
        cached = _channel_pause_cache.get(key)
        if cached is None:
            return None
        expires_at, connector_id, status = cached
        if expires_at <= now:
            _channel_pause_cache.pop(key, None)
            return None
        return connector_id, status


def _set_cached_channel_pause(
    scope_id: str,
    channel: str,
    connector_id: str | None,
    status: str | None,
) -> None:
    key = (scope_id, channel)
    with _channel_pause_cache_lock:
        _channel_pause_cache[key] = (
            time.monotonic() + _CHANNEL_PAUSE_CACHE_TTL_SECONDS,
            connector_id,
            status,
        )


def enforce_channel_pause(
    auth: dict,
    channel: str | None,
    *,
    log_prefix: str = "[Auth]",
) -> None:
    """Reject requests for paused built-in access surfaces.

    Credentials resolve to an exact repository target, while pause/resume is
    represented on that target's Access Surface. Keeping this gate in admission makes Git
    smart HTTP, version WebSocket, and scoped AP-FS routes enforce the same
    policy. The repository below is the legacy connector facade over
    access_surfaces.
    """

    normalized_channel = (channel or "").strip().lower()
    if normalized_channel not in _KNOWN_CHANNELS:
        return
    target = repository_target_from_auth(auth)
    scope_id = repository_target_scope_id(target)
    target_key = f"{target.project_id}\n{scope_id or '<project-root>'}"
    if normalized_channel in _KNOWN_CHANNELS:
        cached = _get_cached_channel_pause(target_key, normalized_channel)
        if cached is None:
            try:
                connector = AccessModelRepository().get_by_target_kind(
                    target.project_id,
                    scope_id,
                    normalized_channel,
                )
            except Exception as e:
                # Fail open ONLY because pause is a recoverable UX gate,
                # not a security boundary — repo membership / mode /
                # excludes are checked elsewhere. A transient connector
                # repo failure should not block legitimate traffic for
                # all scopes. But we cache the failure with the same
                # short TTL so we re-probe quickly when the repo recovers
                # (and don't spam the log on every request).
                log_error(
                    f"{log_prefix} Channel-pause lookup failed for target={target_key} "
                    f"channel={normalized_channel}: {e}; failing open (pause UX only — "
                    f"membership/mode/excludes still enforced)"
                )
                _set_cached_channel_pause(
                    target_key, normalized_channel, None, None,
                )
                connector_id = None
                connector_status = None
            else:
                connector_id = connector.id if connector is not None else None
                connector_status = connector.status if connector is not None else None
                _set_cached_channel_pause(
                    target_key,
                    normalized_channel,
                    connector_id,
                    connector_status,
                )
        else:
            connector_id, connector_status = cached

        if connector_status == "paused":
            log_warning(
                f"{log_prefix} Rejected {normalized_channel} request to target={target_key}: "
                f"connector {connector_id} is paused"
            )
            raise HTTPException(
                status_code=403,
                detail=(
                    f"The '{normalized_channel}' connector for this scope is paused. "
                    "Resume it from the Access page to re-enable this channel."
                ),
            )
