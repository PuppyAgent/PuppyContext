"""Structured config contract for durable integrations."""

from __future__ import annotations

from typing import Any

from src.provider._base import AuthRequirement


USER_CONFIG_KEYS = {"source", "options", "materialization_schema"}
SYSTEM_CONFIG_KEYS = {
    "access_key",
    "authority",
    "conflict_strategy",
    "credentials_ref",
    "target_path",
    "user_id",
}
REMOVED_FLAT_CONFIG_KEYS = {
    "calendar_ids",
    "crawl_options",
    "date_range",
    "days_future",
    "days_past",
    "dimensions",
    "external_resource_id",
    "external_resource_label",
    "external_url",
    "max_results",
    "name",
    "query",
    "row_limit",
    "site_url",
    "source_url",
}


def _nonempty_listish(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(str(item).strip() for item in value)
    return False


def validate_structured_config(
    provider: str,
    spec,
    config: dict[str, Any],
    *,
    allow_system_keys: bool = False,
) -> None:
    allowed_keys = set(USER_CONFIG_KEYS)
    if allow_system_keys:
        allowed_keys.update(SYSTEM_CONFIG_KEYS)
    removed_keys = sorted(set(config) & REMOVED_FLAT_CONFIG_KEYS)
    if removed_keys:
        raise ValueError(
            "legacy flat config fields are no longer supported: "
            f"{', '.join(removed_keys)}"
        )
    unexpected_keys = sorted(set(config) - allowed_keys)
    if unexpected_keys:
        raise ValueError(
            "config only accepts source/options structured fields; "
            f"unexpected keys: {', '.join(unexpected_keys)}"
        )

    source = config.get("source")
    options = config.get("options")
    if not isinstance(source, dict):
        raise ValueError("config.source is required")
    if not isinstance(options, dict):
        raise ValueError("config.options is required")

    if provider == "url":
        if not source.get("resource_url"):
            raise ValueError("config.source.resource_url is required")
        return

    if spec.auth in {AuthRequirement.OAUTH, AuthRequirement.OPTIONAL_OAUTH}:
        if not source.get("resource_id"):
            raise ValueError("config.source.resource_id is required")

    if provider == "google_calendar":
        source_metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
        calendar_ids = options.get("calendar_ids") or source_metadata.get("calendar_ids")
        if not _nonempty_listish(calendar_ids):
            raise ValueError("config.options.calendar_ids is required")


def validate_bootstrap_config(config: dict[str, Any]) -> None:
    removed_keys = sorted(set(config) & REMOVED_FLAT_CONFIG_KEYS)
    if removed_keys:
        raise ValueError(
            "legacy flat config fields are no longer supported: "
            f"{', '.join(removed_keys)}"
        )
    unexpected_keys = sorted(set(config) - USER_CONFIG_KEYS)
    if unexpected_keys:
        raise ValueError(
            "bootstrap config only accepts source/options structured fields; "
            f"unexpected keys: {', '.join(unexpected_keys)}"
        )
    if "options" in config and not isinstance(config.get("options"), dict):
        raise ValueError("config.options must be an object")
