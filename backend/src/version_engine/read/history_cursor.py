"""Signed, stateless cursors for immutable project-History ref snapshots."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import zlib

from src.config import settings
from src.version_engine.read.history_models import HistoryCursorError, HistoryCursorState
from src.version_engine.write_engine.git_object_format import is_git_object_id

_CURSOR_PREFIX = "h1"
_CURSOR_VERSION = 1
_MAX_CURSOR_CHARS = 64 * 1024
_MAX_ROOTS = 512
_KEY_CONTEXT = b"puppyone/project-history-cursor/v1"


class HistoryCursorCodec:
    """Encode ref roots into an authenticated cursor safe across replicas.

    The derived HMAC key is domain-separated from user JWT signing while still
    inheriting its cross-replica stability and rotation.  A cursor grants no
    authorization: every request still passes project membership checks.
    """

    def __init__(self, secret: str | bytes | None = None) -> None:
        raw_secret = secret if secret is not None else settings.JWT_SECRET
        secret_bytes = raw_secret.encode("utf-8") if isinstance(raw_secret, str) else raw_secret
        if not secret_bytes:
            raise ValueError("History cursor signing requires a non-empty secret")
        self._key = hmac.new(secret_bytes, _KEY_CONTEXT, hashlib.sha256).digest()

    def encode(self, state: HistoryCursorState) -> str:
        _validate_state(state)
        payload = json.dumps(
            {
                "v": _CURSOR_VERSION,
                "p": state.project_id,
                "s": state.snapshot_id,
                "r": list(state.roots),
                "h": state.head_commit_id,
                "a": state.anchor_commit_id,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        body = zlib.compress(payload, level=9)
        signature = hmac.new(self._key, body, hashlib.sha256).digest()
        return f"{_CURSOR_PREFIX}.{_encode_base64(body)}.{_encode_base64(signature)}"

    def decode(self, cursor: str, *, project_id: str) -> HistoryCursorState:
        if not cursor or len(cursor) > _MAX_CURSOR_CHARS:
            raise HistoryCursorError("history cursor is invalid")
        try:
            prefix, encoded_body, encoded_signature = cursor.split(".", 2)
            if prefix != _CURSOR_PREFIX:
                raise HistoryCursorError("history cursor version is unsupported")
            body = _decode_base64(encoded_body)
            signature = _decode_base64(encoded_signature)
        except HistoryCursorError:
            raise
        except Exception as exc:
            raise HistoryCursorError("history cursor is invalid") from exc

        expected = hmac.new(self._key, body, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise HistoryCursorError("history cursor signature is invalid")

        try:
            raw = json.loads(zlib.decompress(body).decode("utf-8"))
            state = HistoryCursorState(
                project_id=str(raw["p"]),
                snapshot_id=str(raw["s"]),
                roots=tuple(str(root) for root in raw["r"]),
                head_commit_id=str(raw.get("h") or ""),
                anchor_commit_id=str(raw["a"]),
            )
            if raw.get("v") != _CURSOR_VERSION:
                raise HistoryCursorError("history cursor version is unsupported")
            _validate_state(state)
        except HistoryCursorError:
            raise
        except Exception as exc:
            raise HistoryCursorError("history cursor payload is invalid") from exc
        if state.project_id != project_id:
            raise HistoryCursorError("history cursor belongs to another project")
        return state


def _validate_state(state: HistoryCursorState) -> None:
    if not state.project_id or not _is_sha256(state.snapshot_id):
        raise HistoryCursorError("history cursor payload is invalid")
    if not state.roots or len(state.roots) > _MAX_ROOTS:
        raise HistoryCursorError("history cursor root snapshot is invalid")
    if len(set(state.roots)) != len(state.roots):
        raise HistoryCursorError("history cursor root snapshot is invalid")
    if not all(is_git_object_id(root) for root in state.roots):
        raise HistoryCursorError("history cursor root snapshot is invalid")
    if state.head_commit_id and not is_git_object_id(state.head_commit_id):
        raise HistoryCursorError("history cursor head is invalid")
    if not is_git_object_id(state.anchor_commit_id):
        raise HistoryCursorError("history cursor anchor is invalid")


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _encode_base64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_base64(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)
