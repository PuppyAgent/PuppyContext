from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

from loguru import logger

from src.utils.request_context import patch_log_record_from_context

_CONFIGURED = False


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    return default if raw is None or not raw.strip() else raw.strip()


class InterceptHandler(logging.Handler):
    """Redirect standard-library logging records to Loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except Exception:
            level = record.levelno

        # Find caller depth so Loguru can point to the original logging call site
        frame = logging.currentframe()
        depth = 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        # These reports contain bounded operation names/counts/timings, never
        # request payloads. Preserve them across the stdlib -> Loguru bridge;
        # arbitrary third-party LogRecord extras must not reach JSON logs.
        metrics = {
            key: getattr(record, key)
            for key in ("agent_performance", "agent_api_performance")
            if hasattr(record, key)
        }
        logger.bind(**metrics).opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


def _console_formatter(record: dict[str, Any]) -> str:
    extra = record.get("extra", {}) or {}
    rid = extra.get("request_id")
    method = extra.get("method")
    path = extra.get("path")
    status = extra.get("status_code")
    latency = extra.get("latency_ms")

    ctx_parts: list[str] = []
    if rid:
        ctx_parts.append(f"rid={rid}")
    if method and path:
        ctx_parts.append(f"{method} {path}")
    if status is not None:
        ctx_parts.append(f"status={status}")
    if latency is not None:
        ctx_parts.append(f"latency_ms={latency}")

    ctx = " | " + " ".join(ctx_parts) if ctx_parts else ""
    return (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
        "{message}" + ctx + "\n{exception}"
    )


def _add_console_sink(log_level: str, json_console: bool) -> None:
    if json_console:
        logger.add(
            sys.stderr, level=log_level, serialize=True, backtrace=True, diagnose=False
        )
    else:
        logger.add(
            sys.stderr,
            level=log_level,
            backtrace=True,
            diagnose=False,
            enqueue=True,
            colorize=True,
            format=_console_formatter,
        )


def _add_file_sink(log_path: str, log_level: str, log_rotation: str, log_retention: str, json_file: bool) -> None:
    logger.add(
        log_path,
        level=log_level,
        rotation=log_rotation,
        retention=log_retention,
        enqueue=True,
        backtrace=True,
        diagnose=False,
        serialize=json_file,
    )


def _intercept_stdlib_logging(log_level: str, disable_uvicorn_access: bool) -> None:
    intercept_handler = InterceptHandler()
    logging.root.handlers = [intercept_handler]
    logging.root.setLevel(log_level)

    for name in (
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "fastapi",
        "starlette",
        "httpx",
        "asyncio",
    ):
        _logger = logging.getLogger(name)
        _logger.handlers = [intercept_handler]
        _logger.propagate = False

    if disable_uvicorn_access:
        logging.getLogger("uvicorn.access").disabled = True


def setup_logging() -> None:
    """
    Configure Loguru + intercept stdlib logging (including uvicorn.*).

    Configuration via environment variables:
    - LOG_LEVEL: INFO/DEBUG/WARNING/ERROR (default: INFO)
    - LOG_DIR: directory for log files (default: ./logs)
    - LOG_FILE_NAME: filename (default: app.log)
    - LOG_ROTATION: e.g. "100 MB" / "1 day" (default: "100 MB")
    - LOG_RETENTION: e.g. "14 days" (default: "14 days")
    - LOG_JSON_CONSOLE: 1/0
        - Default: 0 (colorized readable text) if stderr is a TTY (local dev), otherwise 1 (JSON, suitable for log collection)
    - LOG_JSON_FILE: 1/0 (default: 1)
    - DISABLE_UVICORN_ACCESS_LOG: 1/0 (default: 1)  # Avoid duplicating custom access log
    """

    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    log_level = _env_str("LOG_LEVEL", "INFO").upper()
    log_dir = Path(_env_str("LOG_DIR", "./logs"))
    log_file_name = _env_str("LOG_FILE_NAME", "app.log")
    log_rotation = _env_str("LOG_ROTATION", "100 MB")
    log_retention = _env_str("LOG_RETENTION", "14 days")
    # Dev-friendly default: pretty colorized logs on TTY; JSON on non-TTY (container/log collectors).
    default_json_console = not sys.stderr.isatty()
    json_console = _env_bool("LOG_JSON_CONSOLE", default_json_console)
    json_file = _env_bool("LOG_JSON_FILE", True)
    disable_uvicorn_access = _env_bool("DISABLE_UVICORN_ACCESS_LOG", True)

    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / log_file_name

    # Remove default Loguru handler and configure our own sinks
    logger.remove()

    # Inject request context fields into every record (request_id, etc.)
    logger.configure(patcher=patch_log_record_from_context)

    _add_console_sink(log_level, json_console)
    _add_file_sink(str(log_path), log_level, log_rotation, log_retention, json_file)
    _intercept_stdlib_logging(log_level, disable_uvicorn_access)


def get_loguru_logger(**extra: Any):
    """Convenience wrapper to bind structured fields."""
    return logger.bind(**extra)
