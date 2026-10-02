"""ARQ worker settings for durable Synchronize sync runs."""

from __future__ import annotations

import logging
from pathlib import Path

from dotenv import load_dotenv

_env_path = Path(__file__).resolve().parents[3] / ".env"
load_dotenv(_env_path, override=False)

from arq.connections import RedisSettings  # noqa: E402

from src.platform.synchronize.providers import get_synchronize_provider_registry  # noqa: E402
from src.platform.synchronize.run_repository import SyncRunRepository  # noqa: E402
from src.infra.supabase.client import SupabaseClient  # noqa: E402
from src.platform.synchronize.config import synchronize_config  # noqa: E402
from src.platform.synchronize.engine import SynchronizeEngine  # noqa: E402
from src.platform.synchronize.jobs import execute_synchronize_run  # noqa: E402
from src.platform.synchronize.github.jobs import execute_synchronize_github_pull  # noqa: E402
from src.platform.synchronize.repository import SynchronizeRepository  # noqa: E402


logger = logging.getLogger(__name__)


async def startup(ctx: dict) -> None:
    registry = get_synchronize_provider_registry()
    supabase = SupabaseClient()
    run_repo = SyncRunRepository(supabase)
    ctx["sync_run_repository"] = run_repo
    ctx["synchronize_engine"] = SynchronizeEngine(
        registry=registry,
        repository=SynchronizeRepository(supabase),
        run_repo=run_repo,
    )
    ctx["arq_queue_name"] = synchronize_config.synchronize_arq_queue_name
    logger.info("Sync ARQ worker startup complete")


async def shutdown(ctx: dict) -> None:
    logger.info("Sync ARQ worker shutdown")


class WorkerSettings:
    functions = [execute_synchronize_run, execute_synchronize_github_pull]  # noqa: RUF012
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(synchronize_config.redis_url)
    queue_name = synchronize_config.synchronize_arq_queue_name
    job_timeout = synchronize_config.synchronize_task_timeout
