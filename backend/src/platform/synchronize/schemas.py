"""Binding metadata projection; canonical DTOs live in public_schemas only."""

# Owned execution credentials are never metadata. Opaque user content/history
# is not recursively renamed or rewritten.
_REDACTED_CONFIG_KEYS = ("credentials_ref", "access_key")


def _redact_config(config) -> dict | None:
    if not isinstance(config, dict):
        return config
    return {key: value for key, value in config.items() if key not in _REDACTED_CONFIG_KEYS}


def binding_to_response(binding) -> dict:
    config = _redact_config(binding.config)
    trigger = getattr(binding, "trigger", None) or {}
    if getattr(binding, "legacy_read_only_reason", None):
        # Historical configuration is opaque, not an executable/public config
        # contract. Whitelist display metadata rather than guessing secret keys.
        source = config.get("source") if isinstance(config, dict) else None
        name = source.get("resource_name") if isinstance(source, dict) else None
        config = {"source": {"resource_name": name}} if isinstance(name, str) else {}
        trigger = {"type": trigger.get("type", "manual")}
    return {
        "id": binding.id,
        "project_id": binding.project_id,
        "path": binding.path,
        "direction": binding.direction,
        "provider": binding.provider,
        "config": config,
        "status": binding.status,
        "last_synchronize_commit_id": binding.last_synchronize_commit_id,
        "error_message": binding.error_message,
        "trigger": trigger,
        "last_synced_at": getattr(binding, "last_synced_at", None),
        "created_at": getattr(binding, "created_at", None),
        "updated_at": getattr(binding, "updated_at", None),
    }
