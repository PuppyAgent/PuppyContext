"""
Scheduler service for managing scheduled agent executions.
"""

from datetime import datetime
from typing import Optional

from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.job import Job
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.config import settings
from src.infra.scheduler.config import scheduler_settings
from src.infra.scheduler.jobs import (
    execute_agent_task,
    execute_sync_pull,
    process_git_object_gc,
    process_object_integrity_scan,
    process_project_deletion_cleanup,
    process_project_initialization_reconciliation,
    process_sync_run_reaper,
    process_version_outbox,
)
from src.infra.scheduler.jobs.import_job_reaper import process_import_job_reaper
from src.infra.scheduler.jobs.shadow_snapshot_reaper import (
    process_shadow_snapshot_reaper,
)
from src.infra.scheduler.jobs.upload_job_reaper import process_upload_job_reaper
from src.utils.logger import log_error, log_info, log_warning


class SchedulerService:
    """
    Service for managing APScheduler and agent jobs.

    Responsibilities:
    - Start/stop the scheduler
    - Add/remove/update agent jobs dynamically
    - Load all schedule agents from database on startup
    """

    _instance: Optional["SchedulerService"] = None

    def __init__(self):
        self.scheduler: Optional[AsyncIOScheduler] = None
        self._started = False

    @classmethod
    def get_instance(cls) -> "SchedulerService":
        """Get singleton instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def start(self):
        """Initialize and start the scheduler."""
        if not scheduler_settings.enabled:
            log_info("⏭️  Scheduler disabled (SCHEDULER_ENABLED=false)")
            return

        if self._started:
            log_warning("Scheduler already started")
            return

        log_info("⏰ Starting APScheduler...")

        # Configure executors. The default executor must remain asyncio-aware so
        # APScheduler awaits coroutine jobs instead of running them in a thread.
        executors = {
            "default": AsyncIOExecutor(),
            "threadpool": ThreadPoolExecutor(scheduler_settings.max_workers),
        }

        # Configure job defaults
        job_defaults = {
            "coalesce": scheduler_settings.coalesce,
            "max_instances": 1,
            "misfire_grace_time": scheduler_settings.misfire_grace_time,
        }

        # Create scheduler
        self.scheduler = AsyncIOScheduler(
            executors=executors,
            job_defaults=job_defaults,
            timezone=scheduler_settings.timezone,
        )

        # Start the scheduler
        self.scheduler.start()
        self._started = True

        # Load existing schedule agents from database
        await self._load_scheduled_agents()
        await self._load_scheduled_syncs()

        if settings.VERSION_OUTBOX_ENABLED:
            self.scheduler.add_job(
                process_version_outbox,
                trigger=IntervalTrigger(
                    seconds=settings.VERSION_OUTBOX_INTERVAL_SECONDS,
                ),
                id="version-outbox",
                name="Version Outbox Repair",
                replace_existing=True,
                executor="threadpool",
            )

        if settings.VERSION_OBJECT_GC_ENABLED:
            self.scheduler.add_job(
                process_git_object_gc,
                trigger=IntervalTrigger(
                    seconds=settings.VERSION_OBJECT_GC_INTERVAL_SECONDS,
                ),
                id="version-object-gc",
                name="Version Engine Git Object GC",
                replace_existing=True,
                executor="threadpool",
            )

        if settings.PROJECT_DELETION_CLEANUP_ENABLED:
            self.scheduler.add_job(
                process_project_deletion_cleanup,
                trigger=IntervalTrigger(
                    seconds=settings.PROJECT_DELETION_CLEANUP_INTERVAL_SECONDS,
                ),
                id="project-deletion-cleanup",
                name="Project Object Prefix Cleanup",
                replace_existing=True,
            )

        if settings.PROJECT_INITIALIZATION_RECONCILE_ENABLED:
            self.scheduler.add_job(
                process_project_initialization_reconciliation,
                trigger=IntervalTrigger(
                    seconds=settings.PROJECT_INITIALIZATION_RECONCILE_INTERVAL_SECONDS,
                ),
                id="project-initialization-reconciler",
                name="Project Root Initialization Reconciler",
                replace_existing=True,
            )

        if settings.VERSION_INTEGRITY_SCAN_ENABLED:
            self.scheduler.add_job(
                process_object_integrity_scan,
                trigger=IntervalTrigger(
                    seconds=settings.VERSION_INTEGRITY_SCAN_INTERVAL_SECONDS,
                ),
                id="version-object-integrity-scan",
                name="Version Engine Object Integrity Scan",
                replace_existing=True,
                executor="threadpool",
            )

        if settings.SHADOW_SNAPSHOT_REAPER_ENABLED:
            self.scheduler.add_job(
                process_shadow_snapshot_reaper,
                trigger=IntervalTrigger(
                    seconds=settings.SHADOW_SNAPSHOT_REAPER_INTERVAL_SECONDS,
                ),
                id="shadow-snapshot-reaper",
                name="Shadow Snapshot TTL Reaper",
                replace_existing=True,
            )

        if settings.SYNC_RUN_REAPER_ENABLED:
            self.scheduler.add_job(
                process_sync_run_reaper,
                trigger=IntervalTrigger(
                    seconds=settings.SYNC_RUN_REAPER_INTERVAL_SECONDS,
                ),
                id="sync-run-reaper",
                name="Integration Sync Run Lease Reaper",
                replace_existing=True,
            )

        if settings.IMPORT_JOB_REAPER_ENABLED:
            self.scheduler.add_job(
                process_import_job_reaper,
                trigger=IntervalTrigger(
                    seconds=settings.IMPORT_JOB_REAPER_INTERVAL_SECONDS,
                ),
                id="import-job-reaper",
                name="One-Time Import Job Reaper",
                replace_existing=True,
            )

        if settings.UPLOAD_JOB_REAPER_ENABLED:
            self.scheduler.add_job(
                process_upload_job_reaper,
                trigger=IntervalTrigger(
                    seconds=settings.UPLOAD_JOB_REAPER_INTERVAL_SECONDS,
                ),
                id="upload-job-reaper",
                name="Upload Job Reaper",
                replace_existing=True,
            )

        log_info(f"✅ APScheduler started with {scheduler_settings.max_workers} workers")

    async def shutdown(self):
        """Gracefully shutdown the scheduler."""
        if self.scheduler and self._started:
            log_info("⏰ Shutting down APScheduler...")
            self.scheduler.shutdown(wait=True)
            self._started = False
            log_info("✅ APScheduler stopped")

    async def _load_scheduled_agents(self):
        """Load all schedule agents from database and register jobs."""
        if not self.scheduler:
            return

        try:
            from src.platform.access.surface_repository import AccessSurfaceRepository

            agents = [
                row for row in AccessSurfaceRepository().list_all(
                    kind="agent", status="active"
                )
                if (row.get("config") or {}).get("type") == "schedule"
                and ((row.get("config") or {}).get("trigger") or {}).get("type")
                in {"cron", "scheduled"}
            ]
            log_info(f"📋 Found {len(agents)} schedule agents to load")

            for agent in agents:
                config = agent.get("config") or {}
                trigger = config.get("trigger") or {}
                await self.add_agent_job(
                    agent_id=agent["id"],
                    trigger_config=trigger.get("config") or trigger,
                    agent_name=config.get("name", "Unknown")
                )

            log_info(f"✅ Loaded {len(agents)} agent jobs")

        except Exception as e:
            log_error(f"❌ Failed to load scheduled agents: {e}")

    async def add_agent_job(
        self,
        agent_id: str,
        trigger_config: dict,
        agent_name: str = ""
    ) -> Optional[Job]:
        """
        Add a new agent job to the scheduler.

        Args:
            agent_id: The agent's unique ID (used as job_id)
            trigger_config: Configuration containing schedule info
                - schedule: cron expression (e.g., "0 9 * * *")
                - timezone: optional timezone override
                - date: ISO date for one-time execution
                - repeat_type: 'once', 'daily', 'weekly'
            agent_name: Human-readable name for logging
        """
        if not self.scheduler or not self._started:
            log_warning(f"Scheduler not running, skipping job for agent {agent_id}")
            return None

        # Remove existing job if any
        self.remove_agent_job(agent_id)

        # Parse trigger configuration
        trigger = self._parse_trigger(trigger_config)
        if not trigger:
            log_warning(f"Invalid trigger config for agent {agent_id}: {trigger_config}")
            return None

        # Add the job
        job = self.scheduler.add_job(
            execute_agent_task,
            trigger=trigger,
            id=agent_id,
            name=f"Agent: {agent_name or agent_id}",
            args=[agent_id],
            replace_existing=True,
            executor="threadpool",
        )

        next_run = job.next_run_time.strftime("%Y-%m-%d %H:%M:%S") if job.next_run_time else "N/A"
        log_info(f"📅 Added job for agent '{agent_name}' ({agent_id}), next run: {next_run}")

        return job

    # ── Sync Trigger (unified API) ───────────────────────────

    async def sync_trigger(
        self,
        connection_id: str,
        provider: str = "",
        trigger_config: dict | None = None,
    ) -> Optional[Job]:
        """Unified trigger registration for scheduled Connect rows.

        Connect routers should call this single method instead of directly
        manipulating scheduler jobs.

        Args:
            connection_id: The connection / sync row ID.
            provider: Source provider name (e.g. "gmail", "github").
            trigger_config: Scheduling config dict, or *None* to remove.

        Returns:
            The registered ``Job``, or ``None`` if removed / skipped.
        """
        if not self.scheduler or not self._started:
            return None

        job_id = f"sync:{connection_id}"

        try:
            self.scheduler.remove_job(job_id)
        except Exception:
            pass

        if not trigger_config:
            return None

        trigger = self._parse_trigger(trigger_config)
        if not trigger:
            log_warning(f"Invalid trigger config for {connection_id}: {trigger_config}")
            return None

        job = self.scheduler.add_job(
            execute_sync_pull,
            trigger=trigger,
            id=job_id,
            name=f"Sync: {provider} ({connection_id[:8]})",
            args=[connection_id],
            replace_existing=True,
            executor="threadpool",
        )

        next_run = job.next_run_time.strftime("%Y-%m-%d %H:%M:%S") if job.next_run_time else "N/A"
        log_info(f"📅 Registered trigger for {provider} ({connection_id[:8]}), next: {next_run}")
        return job

    # ── Internal: startup loaders ─────────────────────────────

    async def _load_scheduled_syncs(self):
        """Load all syncs with trigger type 'scheduled' and register jobs."""
        if not self.scheduler:
            return

        try:
            from src.infra.supabase.client import SupabaseClient

            client = SupabaseClient().client
            result = (
                client.table("connections")
                .select("id, provider, trigger_type, trigger_config, status")
                .eq("status", "active")
                .execute()
            )

            syncs = [
                {
                    "id": row["id"],
                    "provider": row.get("provider", ""),
                    "trigger": {
                        "type": row.get("trigger_type"),
                        **(row.get("trigger_config") or {}),
                    },
                }
                for row in (result.data or [])
            ]
            scheduled = [
                s for s in syncs
                if (s.get("trigger") or {}).get("type") == "scheduled"
            ]

            log_info(f"Found {len(scheduled)} scheduled syncs to load")

            for sync_row in scheduled:
                await self.sync_trigger(
                    connection_id=sync_row["id"],
                    provider=sync_row.get("provider", ""),
                    trigger_config=sync_row.get("trigger") or {},
                )

            if scheduled:
                log_info(f"Loaded {len(scheduled)} sync polling jobs")

        except Exception as e:
            log_error(f"Failed to load scheduled syncs: {e}")

    def remove_agent_job(self, agent_id: str) -> bool:
        """Remove an agent job from the scheduler."""
        if not self.scheduler:
            return False

        try:
            self.scheduler.remove_job(agent_id)
            log_info(f"🗑️  Removed job for agent {agent_id}")
            return True
        except Exception:
            # Job doesn't exist, that's fine
            return False

    def _parse_trigger(self, config: dict):
        """
        Parse trigger configuration and return APScheduler trigger.

        Supports:
        - Cron expression: {"schedule": "0 9 * * *"}
        - Simple time + repeat: {"time": "09:00", "repeat_type": "daily", "date": "2026-01-30"}
        """
        # If explicit cron schedule provided
        if "schedule" in config:
            try:
                return CronTrigger.from_crontab(
                    config["schedule"],
                    timezone=config.get("timezone", scheduler_settings.timezone)
                )
            except Exception as e:
                log_error(f"Invalid cron expression '{config['schedule']}': {e}")
                return None

        # Parse simple time/date/repeat format from frontend
        time_str = config.get("time", "09:00")  # HH:MM format
        date_str = config.get("date")  # YYYY-MM-DD format
        repeat_type = config.get("repeat_type", "once")  # once, daily, weekly
        timezone = config.get("timezone", scheduler_settings.timezone)

        try:
            hour, minute = map(int, time_str.split(":"))
        except (ValueError, AttributeError):
            hour, minute = 9, 0  # Default to 9:00 AM

        if repeat_type == "once":
            # One-time execution
            if date_str:
                try:
                    run_date = datetime.fromisoformat(f"{date_str}T{time_str}:00")
                    return DateTrigger(run_date=run_date, timezone=timezone)
                except Exception as e:
                    log_error(f"Invalid date '{date_str}': {e}")
                    return None
            else:
                log_warning("One-time trigger without date, skipping")
                return None

        elif repeat_type == "daily":
            # Every day at specified time
            return CronTrigger(hour=hour, minute=minute, timezone=timezone)

        elif repeat_type == "weekly":
            # Every week on the same day
            if date_str:
                try:
                    dt = datetime.fromisoformat(date_str)
                    day_of_week = dt.weekday()  # 0=Monday, 6=Sunday
                    return CronTrigger(
                        day_of_week=day_of_week,
                        hour=hour,
                        minute=minute,
                        timezone=timezone
                    )
                except Exception as e:
                    log_error(f"Invalid date for weekly trigger '{date_str}': {e}")
                    return None
            else:
                # Default to Monday if no date specified
                return CronTrigger(day_of_week=0, hour=hour, minute=minute, timezone=timezone)

        else:
            log_warning(f"Unknown repeat_type: {repeat_type}")
            return None



# Global instance getter
def get_scheduler_service() -> SchedulerService:
    """Get the global scheduler service instance."""
    return SchedulerService.get_instance()
