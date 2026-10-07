"""History change normalization.

Version history stores native tree actions as ``add`` / ``update`` / ``delete``.
User-facing history surfaces use Git-style operation labels:
``added`` / ``modified`` / ``deleted``. Keep the mapping in one place so
stored history rows and UI contracts stay aligned.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, TypedDict

HistoryAction = Literal["add", "update", "delete"]
HistoryOp = Literal["added", "modified", "deleted"]


class HistoryChange(TypedDict):
    path: str
    action: HistoryAction
    op: HistoryOp


_ACTION_ALIASES: dict[str, HistoryAction] = {
    "add": "add",
    "added": "add",
    "create": "add",
    "created": "add",
    "update": "update",
    "updated": "update",
    "modify": "update",
    "modified": "update",
    "delete": "delete",
    "deleted": "delete",
    "remove": "delete",
    "removed": "delete",
}

_ACTION_TO_OP: dict[HistoryAction, HistoryOp] = {
    "add": "added",
    "update": "modified",
    "delete": "deleted",
}


def normalize_history_action(value: Any) -> HistoryAction:
    raw = str(value or "").strip().lower()
    return _ACTION_ALIASES.get(raw, "update")


def normalize_history_change(change: Mapping[str, Any]) -> HistoryChange:
    action = normalize_history_action(change.get("action") or change.get("op"))
    return {
        "path": str(change.get("path") or ""),
        "action": action,
        "op": _ACTION_TO_OP[action],
    }


def normalize_history_changes(changes: list[Mapping[str, Any]] | None) -> list[HistoryChange]:
    return [normalize_history_change(change) for change in (changes or [])]
