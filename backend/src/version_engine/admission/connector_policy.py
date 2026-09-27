"""AccessSurface-level admission policy for Version Engine entry points."""

from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Any

from fastapi import HTTPException

from src.platform.repository_target.auth_context import repository_target_from_auth
from src.platform.repository_target.models import repository_target_scope_id
from src.platform.access.model_repository import AccessModelRepository
from src.utils.logger import log_error, log_warning
from src.version_engine.admission.channel_pause import enforce_channel_pause


CLI_FS_READ_COMMANDS = frozenset({
    "semantics",
    "ls",
    "tree",
    "find",
    "grep",
    "stat",
    "cat",
    "head",
    "tail",
    "download",
})
CLI_FS_WRITE_COMMANDS = frozenset({
    "write",
    "mkdir",
    "touch",
    "upload",
    "cp",
    "mv",
})
CLI_FS_DELETE_COMMANDS = frozenset({"rm", "rmdir"})
CLI_FS_KNOWN_COMMANDS = CLI_FS_READ_COMMANDS | CLI_FS_WRITE_COMMANDS | CLI_FS_DELETE_COMMANDS
CLI_FS_DEFAULT_ALLOWED_COMMANDS = CLI_FS_READ_COMMANDS | CLI_FS_WRITE_COMMANDS

_CLI_FS_POLICY_CACHE_TTL_SECONDS = 2.0


@dataclass(frozen=True)
class ConnectorPolicySnapshot:
    connector_id: str
    provider: str
    status: str
    allowed_commands: frozenset[str]


_policy_cache: dict[tuple[str, str, str], tuple[float, ConnectorPolicySnapshot]] = {}
_policy_cache_lock = threading.Lock()


def clear_connector_policy_cache(
    *,
    scope_id: str | None = None,
    provider: str | None = None,
) -> None:
    """Drop cached connector policy snapshots.

    AccessSurface policy is a hot-path admission check. The TTL is short, but CRUD
    paths call this so UI changes feel immediate.
    """

    with _policy_cache_lock:
        if scope_id is None and provider is None:
            _policy_cache.clear()
            return
        for key in list(_policy_cache):
            _key_project_id, key_scope_id, key_provider = key
            if scope_id is not None and key_scope_id != scope_id:
                continue
            if provider is not None and key_provider != provider:
                continue
            _policy_cache.pop(key, None)


def effective_cli_fs_allowed_commands(policy: dict[str, Any] | None) -> frozenset[str]:
    """Return the effective CLI FS allow-list from a connector policy blob.

    Missing policy means "legacy default" for existing connectors. An explicit
    malformed allow-list fails closed by returning an empty set.
    """

    if not isinstance(policy, dict) or not policy:
        return CLI_FS_DEFAULT_ALLOWED_COMMANDS
    fs_policy = policy.get("fs")
    if not isinstance(fs_policy, dict):
        return CLI_FS_DEFAULT_ALLOWED_COMMANDS
    if "allowed_commands" not in fs_policy:
        return CLI_FS_DEFAULT_ALLOWED_COMMANDS

    raw_commands = fs_policy.get("allowed_commands")
    if not isinstance(raw_commands, list):
        return frozenset()

    allowed: set[str] = set()
    for item in raw_commands:
        command = str(item).strip().lower()
        if command in CLI_FS_KNOWN_COMMANDS:
            allowed.add(command)
    return frozenset(allowed)


def _get_cached_policy(
    project_id: str,
    scope_id: str,
    provider: str,
) -> ConnectorPolicySnapshot | None:
    now = time.monotonic()
    key = (project_id, scope_id, provider)
    with _policy_cache_lock:
        cached = _policy_cache.get(key)
        if cached is None:
            return None
        expires_at, snapshot = cached
        if expires_at <= now:
            _policy_cache.pop(key, None)
            return None
        return snapshot


def _set_cached_policy(
    project_id: str,
    scope_id: str,
    provider: str,
    snapshot: ConnectorPolicySnapshot,
) -> None:
    key = (project_id, scope_id, provider)
    with _policy_cache_lock:
        _policy_cache[key] = (
            time.monotonic() + _CLI_FS_POLICY_CACHE_TTL_SECONDS,
            snapshot,
        )


def get_connector_policy_snapshot(
    project_id: str,
    scope_id: str,
    provider: str,
) -> ConnectorPolicySnapshot:
    cached = _get_cached_policy(project_id, scope_id, provider)
    if cached is not None:
        return cached

    try:
        connector = AccessModelRepository().get_by_target_kind(
            project_id,
            scope_id,
            provider,
        )
    except Exception as exc:
        log_error(
            f"[ConnectorPolicy] Lookup failed for scope={scope_id} "
            f"provider={provider}: {exc}; failing closed"
        )
        raise HTTPException(
            status_code=403,
            detail="Connector policy could not be verified",
        ) from exc

    if connector is None:
        raise HTTPException(
            status_code=403,
            detail=f"'{provider}' connector is not configured for this scope",
        )

    snapshot = ConnectorPolicySnapshot(
        connector_id=connector.id,
        provider=connector.kind,
        status=connector.status,
        allowed_commands=(
            effective_cli_fs_allowed_commands(connector.policy)
            if provider == "cli"
            else frozenset()
        ),
    )
    _set_cached_policy(project_id, scope_id, provider, snapshot)
    return snapshot


def admit_cli_fs_command(
    auth: dict,
    command: str,
    channel: str | None,
    *,
    log_prefix: str = "[AP-FS]",
) -> None:
    """Enforce connector policy for one AP-FS command.

    Access-key auth already resolved the scope. This gate only answers:
    "is this CLI connector allowed to run this FS CLI command?"
    """

    normalized_channel = (channel or "").strip().lower()
    if normalized_channel != "cli":
        raise HTTPException(
            status_code=400,
            detail="AP-FS commands must use X-Puppy-Client: cli",
        )

    normalized_command = command.strip().lower()
    if normalized_command not in CLI_FS_KNOWN_COMMANDS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown CLI FS command: {command!r}",
        )

    target = repository_target_from_auth(auth)
    scope_id = repository_target_scope_id(target)
    if scope_id is None:
        raise HTTPException(
            status_code=403,
            detail="This legacy CLI surface requires an exact repository Scope",
        )

    snapshot = get_connector_policy_snapshot(target.project_id, scope_id, "cli")
    if snapshot.status == "paused":
        log_warning(
            f"{log_prefix} Rejected cli command={normalized_command} "
            f"scope={scope_id}: connector {snapshot.connector_id} is paused"
        )
        raise HTTPException(
            status_code=403,
            detail="The 'cli' connector for this scope is paused",
        )

    if normalized_command not in snapshot.allowed_commands:
        log_warning(
            f"{log_prefix} Rejected cli command={normalized_command} "
            f"scope={scope_id}: connector {snapshot.connector_id} policy denied it"
        )
        raise HTTPException(
            status_code=403,
            detail={
                "error_code": "CLI_FS_COMMAND_DENIED",
                "command": normalized_command,
                "connector_id": snapshot.connector_id,
                "message": f"CLI connector policy does not allow '{normalized_command}'",
            },
        )
