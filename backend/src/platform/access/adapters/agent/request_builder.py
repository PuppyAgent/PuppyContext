"""
Request Builder — Helper functions for building Claude API requests.

Contains utility functions and constants used by AgentService to construct
tool definitions, sanitize names, create API clients, and normalize content.
"""
import re
from typing import Any, Iterable

from src.config import settings


# Anthropic official bash tool (Computer Use format, only supported by official API)
BASH_TOOL_NATIVE = {"type": "bash_20250124", "name": "bash"}

# Generic bash tool definition (compatible with third-party proxy gateways)
BASH_TOOL_COMPAT = {
    "name": "bash",
    "description": (
        "Execute a bash command in the sandbox environment. "
        "Use this to run shell commands, view files, manipulate data, etc. "
        "Returns the command output (stdout and stderr combined)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The bash command to execute",
            }
        },
        "required": ["command"],
    },
}


def _use_native_anthropic() -> bool:
    """Determine whether the official Anthropic API is being used (not a third-party proxy)."""
    base_url = settings.ANTHROPIC_BASE_URL
    if not base_url:
        return True
    return "api.anthropic.com" in base_url


def _get_bash_tool() -> dict:
    """Select the appropriate bash tool definition based on the API endpoint."""
    if _use_native_anthropic():
        return BASH_TOOL_NATIVE
    return BASH_TOOL_COMPAT


def _sanitize_tool_name(name: str) -> str:
    """Convert a tool name to a Claude-compatible format (only a-zA-Z0-9_-)."""
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    # Strip leading/trailing underscores and collapse consecutive underscores
    sanitized = re.sub(r"_+", "_", sanitized).strip("_")
    return sanitized or "unnamed"


def _default_anthropic_client():
    """Create a default AsyncAnthropic client using settings."""
    from anthropic import AsyncAnthropic
    api_key = settings.ANTHROPIC_API_KEY or None
    base_url = settings.ANTHROPIC_BASE_URL or None
    return AsyncAnthropic(api_key=api_key, base_url=base_url)


def _get_attr(obj, name: str, default=None):
    """Access an attribute from either a dict or an object."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _normalize_content(content: Iterable[Any]):
    """Normalize content blocks into a list of plain dicts."""
    normalized = []
    for block in content:
        block_type = _get_attr(block, "type")
        if block_type == "text":
            normalized.append({"type": "text", "text": _get_attr(block, "text")})
        elif block_type == "tool_use":
            normalized.append({
                "type": "tool_use",
                "id": _get_attr(block, "id"),
                "name": _get_attr(block, "name"),
                "input": _get_attr(block, "input"),
            })
    return normalized
