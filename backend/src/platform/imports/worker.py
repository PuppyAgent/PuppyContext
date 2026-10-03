"""ARQ worker settings for one-time import jobs."""

from __future__ import annotations

import logging
from pathlib import Path

from dotenv import load_dotenv

_env_path = Path(__file__).resolve().parents[3] / ".env"
load_dotenv(_env_path, override=False)

from arq.connections import RedisSettings  # noqa: E402

from src.platform.imports.config import import_config  # noqa: E402
from src.platform.imports.jobs import execute_import_job  # noqa: E402
from src.platform.imports.repository import ImportJobRepository  # noqa: E402
from src.platform.imports.runner import OneTimeImportRunner  # noqa: E402


logger = logging.getLogger(__name__)


async def startup(ctx: dict) -> None:
    ctx["import_job_repository"] = ImportJobRepository()
    ctx["one_time_import_runner"] = OneTimeImportRunner()
    ctx["arq_queue_name"] = import_config.import_arq_queue_name
    logger.info("Import ARQ worker startup complete")


async def shutdown(ctx: dict) -> None:
    logger.info("Import ARQ worker shutdown")


class WorkerSettings:
    functions = [execute_import_job]  # noqa: RUF012
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(import_config.redis_url)
    queue_name = import_config.import_arq_queue_name
    job_timeout = import_config.import_task_timeout
