"""Consume native repository ref events on the scheduler's async event loop."""

from src.utils.logger import log_error
from src.version_engine.derived.native_events import process_native_events


async def process_native_projection() -> dict:
    try:
        return {"status": "ok", "processed": await process_native_events()}
    except Exception as exc:
        log_error(f"[native-projection] scheduler job failed: {type(exc).__name__}")
        return {"status": "failed", "error": type(exc).__name__}
