"""
ContextBase Backend Server Entrypoint.
"""

# ruff: noqa: E402

import asyncio
import os
import time
from contextlib import asynccontextmanager, suppress

from fastapi import Depends, FastAPI, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.infra.mcp_health import get_mcp_health_client

# Record application start time
APP_START_TIME = time.time()

# Load .env file (for local development only; production uses system environment variables directly)
from dotenv import load_dotenv

dotenv_start = time.time()
# Explicit launch/deployment configuration takes precedence over local defaults.
# Overriding it can silently redirect Desktop auth or billing to another origin.
load_dotenv(override=False)
dotenv_duration = time.time() - dotenv_start

# Initialize Loguru + intercept standard logging (including uvicorn.*)
from src.utils.logging_setup import setup_logging

setup_logging()

# Record module import times
config_start = time.time()
from src.config import settings

config_duration = time.time() - config_start

exceptions_start = time.time()
from src.exception_handler import (
    app_exception_handler,
    generic_exception_handler,
    http_exception_handler,
    security_store_unavailable_handler,
    validation_exception_handler,
)
from src.exceptions import AppException
from src.platform.auth.shared_security_store import SecurityStoreUnavailable

exceptions_duration = time.time() - exceptions_start

logger_start = time.time()
from src.utils.logger import log_error, log_info

logger_duration = time.time() - logger_start

# Record router module import times
table_router_start = time.time()
from src.content.table.router import router as table_router

table_router_duration = time.time() - table_router_start

tool_router_start = time.time()
from src.tool.router import router as tool_router

tool_router_duration = time.time() - tool_router_start

mcp_v3_router_start = time.time()
from src.platform.access.adapters.agent.mcp.router import router as mcp_v3_router

mcp_v3_router_duration = time.time() - mcp_v3_router_start

agent_router_start = time.time()
from src.platform.access.adapters.agent.config.router import router as agent_config_router
from src.platform.access.adapters.agent.router import router as agent_router

agent_router_duration = time.time() - agent_router_start

context_publish_router_start = time.time()
from src.context_publish.router import public_router as context_publish_public_router
from src.context_publish.router import router as context_publish_router

context_publish_router_duration = time.time() - context_publish_router_start

# Legacy ingest compatibility router (ETL/task management + old upload/import aliases)
ingest_router_start = time.time()
from src.ingest.router import router as ingest_router

ingest_router_duration = time.time() - ingest_router_start

project_router_start = time.time()
from src.platform.project.router import router as project_router
from src.platform.template_registry.router import router as template_registry_router

project_router_duration = time.time() - project_router_start

from src.platform.organization.router import router as organization_router

oauth_router_start = time.time()
from src.provider.oauth.router import router as oauth_router

oauth_router_duration = time.time() - oauth_router_start

internal_router_start = time.time()
from src.internal.router import router as internal_router

internal_router_duration = time.time() - internal_router_start

content_router_start = time.time()

content_router_duration = time.time() - content_router_start

analytics_router_start = time.time()
from src.platform.analytics.router import router as analytics_router

analytics_router_duration = time.time() - analytics_router_start

profile_router_start = time.time()
from src.platform.profile.router import router as profile_router

profile_router_duration = time.time() - profile_router_start

imports_router_start = time.time()
from src.platform.activity.router import router as activity_router
from src.platform.imports.router import router as imports_router

imports_router_duration = time.time() - imports_router_start

database_import_router_start = time.time()
from src.platform.imports.database.router import router as database_import_router

database_import_router_duration = time.time() - database_import_router_start

# Scheduler service import
scheduler_start = time.time()
from src.infra.scheduler.config import scheduler_settings
from src.infra.scheduler.service import get_scheduler_service

scheduler_import_duration = time.time() - scheduler_start


def _validate_security_baseline() -> None:
    """Validate critical security configuration in non-development environments."""
    if settings.DEBUG:
        return

    if not (settings.INTERNAL_API_SECRET or "").strip():
        raise RuntimeError("INTERNAL_API_SECRET must be configured when DEBUG is False")

    if "*" in (settings.ALLOWED_HOSTS or []):
        raise RuntimeError("ALLOWED_HOSTS cannot contain '*' when DEBUG is False")


_validate_security_baseline()

routers_duration = (
    table_router_duration
    + tool_router_duration
    + mcp_v3_router_duration
    + agent_router_duration
    + context_publish_router_duration
    + ingest_router_duration
    + project_router_duration
    + oauth_router_duration
    + internal_router_duration
    + content_router_duration
    + analytics_router_duration
    + profile_router_duration
    + imports_router_duration
    + database_import_router_duration
)


def _log_import_times() -> None:
    """Output module import time breakdown."""
    log_info("📦 Module import time breakdown:")
    log_info(f"  ├─ .env loading: {dotenv_duration * 1000:.2f}ms")
    log_info(f"  ├─ Config module (config): {config_duration * 1000:.2f}ms")
    log_info(f"  ├─ Exception handling module (exceptions): {exceptions_duration * 1000:.2f}ms")
    log_info(f"  ├─ Logging module (logger): {logger_duration * 1000:.2f}ms")
    log_info("  ├─ Router modules:")
    log_info(f"  │  ├─ table_router: {table_router_duration * 1000:.2f}ms")
    log_info(f"  │  ├─ tool_router: {tool_router_duration * 1000:.2f}ms")
    log_info(f"  │  ├─ mcp_router(v3): {mcp_v3_router_duration * 1000:.2f}ms")
    log_info(f"  │  ├─ agent_router: {agent_router_duration * 1000:.2f}ms")
    log_info(f"  │  ├─ context_publish_router: {context_publish_router_duration * 1000:.2f}ms")
    log_info(f"  │  ├─ ingest_router: {ingest_router_duration * 1000:.2f}ms")
    log_info(f"  │  ├─ project_router: {project_router_duration * 1000:.2f}ms")
    log_info(f"  │  ├─ oauth_router: {oauth_router_duration * 1000:.2f}ms")
    log_info(f"  │  ├─ internal_router: {internal_router_duration * 1000:.2f}ms")
    log_info(f"  │  ├─ content_router: {content_router_duration * 1000:.2f}ms")
    log_info(f"  │  └─ imports_router: {imports_router_duration * 1000:.2f}ms")
    log_info(f"  └─ Total router time: {routers_duration * 1000:.2f}ms")
    log_info(f"📊 Total import time: {(time.time() - APP_START_TIME) * 1000:.2f}ms")
    log_info("")


async def _init_mcp_health_check() -> None:
    """Check MCP Server health status."""
    mcp_init_start = time.time()
    try:
        log_info("🔌 Checking MCP Server health status...")
        mcp_service = get_mcp_health_client()
        health_result = await mcp_service.check_mcp_server_health()
        mcp_duration = time.time() - mcp_init_start
        if health_result.get("status", "") != "unhealthy":
            log_info(
                f"✅ MCP Server health check completed: {health_result} (took: {mcp_duration * 1000:.2f}ms)"
            )
        else:
            log_error(f"❌ MCP Server is down, health info: {health_result}")
    except Exception as e:
        mcp_duration = time.time() - mcp_init_start
        log_error(f"❌ MCP Server health check failed (took: {mcp_duration * 1000:.2f}ms): {e}")


async def _init_scheduler() -> None:
    """Initialize Scheduler service."""
    scheduler_init_start = time.time()
    try:
        if scheduler_settings.enabled:
            log_info("⏰ Initializing Scheduler service...")
            scheduler_service = get_scheduler_service()
            await scheduler_service.start()
            scheduler_duration = time.time() - scheduler_init_start
            log_info(
                f"✅ Scheduler service started successfully (took: {scheduler_duration * 1000:.2f}ms)"
            )
        else:
            log_info("⏭️  Scheduler service skipped (SCHEDULER_ENABLED is off)")
    except Exception as e:
        scheduler_duration = time.time() - scheduler_init_start
        log_error(
            f"❌ Scheduler service failed to start (took: {scheduler_duration * 1000:.2f}ms): {e}"
        )


async def _init_file_ingest() -> None:
    """Initialize File Ingest service if ETL is enabled."""
    if not settings.etl_enabled:
        log_info("⏭️  File Ingest service skipped (ENABLE_ETL is off)")
        return
    file_ingest_init_start = time.time()
    try:
        log_info("📄 Initializing File Ingest service...")
        from pathlib import Path

        from src.platform.upload.dependencies import get_etl_service

        file_ingest_service = await get_etl_service()
        Path(".mineru_cache").mkdir(parents=True, exist_ok=True)
        Path(".etl_rules").mkdir(parents=True, exist_ok=True)
        await file_ingest_service.start()
        file_ingest_duration = time.time() - file_ingest_init_start
        log_info(
            f"✅ File Ingest service started successfully (took: {file_ingest_duration * 1000:.2f}ms)"
        )
        if settings.DEBUG:
            log_info("   ℹ️  File workers started in DEBUG mode (for development testing)")
    except Exception as e:
        file_ingest_duration = time.time() - file_ingest_init_start
        log_error(
            f"❌ File Ingest service failed to start (took: {file_ingest_duration * 1000:.2f}ms): {e}"
        )


def _init_provider_registry() -> None:
    """Initialize ProviderRegistry singleton."""
    registry_init_start = time.time()
    try:
        log_info("🔌 Initializing ProviderRegistry...")
        from src.provider.dependencies import init_registry

        init_registry()
        registry_duration = time.time() - registry_init_start
        log_info(
            f"✅ ProviderRegistry initialized successfully (took: {registry_duration * 1000:.2f}ms)"
        )
    except Exception as e:
        registry_duration = time.time() - registry_init_start
        log_error(
            f"❌ ProviderRegistry initialization failed (took: {registry_duration * 1000:.2f}ms): {e}"
        )


async def _init_version_trees() -> None:
    """Auto-initialize empty Version Engine trees for projects missing a root."""
    version_init_start = time.time()
    try:
        log_info("🌳 Checking and initializing Version Engine trees...")
        from src.infra.supabase.client import SupabaseClient as _SC
        from src.version_engine.bootstrap.dependencies import build_worker_version_engine_container
        from src.version_engine.infrastructure.supabase.db_names import PROJECT_ROOT_HASH_COLUMN

        _sb = _SC()
        resp = (
            _sb.client.table("projects")
            .select("id")
            # Startup compatibility repair is only for already-published
            # legacy rows. New initializing rows belong exclusively to the
            # durable creation reconciler; in particular, a deferred/template
            # publication must never be replaced by an empty root here.
            .eq("lifecycle_status", "ready")
            .or_(f"{PROJECT_ROOT_HASH_COLUMN}.is.null,{PROJECT_ROOT_HASH_COLUMN}.eq.")
            .execute()
        )
        uninit_projects = resp.data or []
        if uninit_projects:
            _writer = build_worker_version_engine_container().admin_service()
            for row in uninit_projects:
                try:
                    await _writer.init_tree(row["id"])
                except Exception as init_err:
                    log_error(
                        f"  ❌ Failed to init Version Engine tree for {row['id']}: {init_err}"
                    )
            log_info(f"  ✅ Initialized Version Engine tree for {len(uninit_projects)} project(s)")
        else:
            log_info("  ✅ All projects already have a Version Engine tree")
        version_init_duration = time.time() - version_init_start
        log_info(
            f"✅ Version Engine tree check completed (took: {version_init_duration * 1000:.2f}ms)"
        )
    except Exception as e:
        version_init_duration = time.time() - version_init_start
        log_error(
            f"❌ Version Engine tree initialization failed (took: {version_init_duration * 1000:.2f}ms): {e}"
        )


def _init_scope_sandbox_reaper(app: FastAPI) -> None:
    """Start the scope-sandbox reaper (idle→stop, long-idle→destroy) if enabled.

    It is enabled by default and mandatory in hosted deployments so crashed
    workers cannot orphan paid provider resources. Stored on app.state so
    shutdown can stop both durable reaper loops cleanly."""
    if not getattr(settings, "SCOPE_SANDBOX_REAPER_ENABLED", False):
        return
    from src.platform.scope_sandbox.execution.reaper import start_execution_reaper
    from src.platform.scope_sandbox.reaper import start_reaper
    from src.platform.scope_sandbox.service import get_scope_sandbox_service

    service = get_scope_sandbox_service()
    task, stop_event = start_reaper(
        service,
        interval_s=settings.SCOPE_SANDBOX_REAPER_INTERVAL_S,
    )
    app.state.scope_sandbox_reaper = (task, stop_event)
    app.state.sandbox_execution_reaper = start_execution_reaper(
        interval_s=settings.SCOPE_SANDBOX_REAPER_INTERVAL_S,
    )
    log_info(f"🧹 Scope-sandbox reaper started (every {settings.SCOPE_SANDBOX_REAPER_INTERVAL_S}s)")


def _init_runtime_billing_reaper(app: FastAPI) -> None:
    if settings.RUNTIME_METERING_MODE == "disabled":
        return
    from src.platform.billing.runtime import get_runtime_metering_service

    stop_event = asyncio.Event()

    async def loop() -> None:
        while not stop_event.is_set():
            try:
                await get_runtime_metering_service().recover_once()
            except Exception as exc:  # durable rows remain available for the next pass
                log_error(f"Runtime billing recovery error: {type(exc).__name__}")
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=settings.RUNTIME_BILLING_RECOVERY_INTERVAL_SECONDS,
                )

    app.state.runtime_billing_reaper = (asyncio.create_task(loop()), stop_event)
    log_info("Runtime billing recovery loop started")


def _init_entitlement_provisioner(app: FastAPI) -> None:
    if settings.ENTITLEMENTS_MODE != "db" or not settings.PUPPYPAY_BASE_URL:
        return
    from src.platform.billing.provisioning import get_entitlement_provisioning_service

    service = get_entitlement_provisioning_service()
    stop_event = asyncio.Event()

    async def loop() -> None:
        while not stop_event.is_set():
            try:
                result = await service.recover_once(
                    limit=settings.ENTITLEMENT_PROVISIONING_BATCH_SIZE,
                )
                if result["claimed"]:
                    log_info(
                        "Entitlement provisioning: "
                        f"enqueued={result['enqueued']} claimed={result['claimed']} "
                        f"completed={result['succeeded']} failed={result['failed']}"
                    )
            except Exception as exc:
                log_error(f"Entitlement provisioning error: {type(exc).__name__}")
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=settings.ENTITLEMENT_PROVISIONING_INTERVAL_SECONDS,
                )

    app.state.entitlement_provisioner = (asyncio.create_task(loop()), stop_event)
    log_info("Entitlement provisioning loop started")


def _init_seat_proposal_worker(app: FastAPI) -> None:
    if settings.SEAT_BILLING_MODE == "disabled" or not settings.PUPPYPAY_BASE_URL:
        return
    from src.platform.billing.seat_proposals import get_seat_proposal_service

    service = get_seat_proposal_service()
    stop_event = asyncio.Event()

    async def loop() -> None:
        while not stop_event.is_set():
            try:
                result = await service.recover_once(limit=settings.SEAT_PROPOSAL_BATCH_SIZE)
                if result["claimed"]:
                    log_info(
                        "Seat proposals: "
                        f"claimed={result['claimed']} quoted={result['quoted']} "
                        f"failed={result['failed']}"
                    )
            except Exception as exc:
                log_error(f"Seat proposal worker error: {type(exc).__name__}")
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=settings.SEAT_PROPOSAL_INTERVAL_SECONDS,
                )

    app.state.seat_proposal_worker = (asyncio.create_task(loop()), stop_event)
    log_info("Seat proposal worker started")


def _init_storage_reconciler(app: FastAPI) -> None:
    if settings.STORAGE_ENFORCEMENT_MODE == "disabled":
        return
    from src.platform.billing.storage import StorageReconciliationService

    service = StorageReconciliationService(
        repo_manager=app.state.version_engine.repo_manager,
    )
    stop_event = asyncio.Event()

    async def loop() -> None:
        while not stop_event.is_set():
            try:
                result = await service.reconcile_once(
                    limit=settings.STORAGE_RECONCILIATION_BATCH_SIZE,
                    min_age_seconds=settings.STORAGE_RECONCILIATION_MIN_AGE_SECONDS,
                )
                if result["claimed"]:
                    log_info(
                        "Storage reconciliation: "
                        f"claimed={result['claimed']} reconciled={result['reconciled']} "
                        f"failed={result['failed']}"
                    )
            except Exception as exc:
                log_error(f"Storage reconciliation error: {type(exc).__name__}")
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=settings.STORAGE_RECONCILIATION_INTERVAL_SECONDS,
                )

    app.state.storage_reconciler = (asyncio.create_task(loop()), stop_event)
    log_info("Storage reconciliation loop started")


async def _shutdown_services() -> None:
    """Shutdown cleanup logic."""
    log_info("ContextBase API shutting down...")

    if scheduler_settings.enabled:
        try:
            scheduler_service = get_scheduler_service()
            await scheduler_service.shutdown()
            log_info("Scheduler service stopped successfully")
        except Exception as e:
            log_error(f"Failed to stop Scheduler service: {e}")

    log_info("Filesystem sync: client-side, no cleanup needed")

    if settings.etl_enabled:
        try:
            from src.platform.upload.dependencies import get_etl_service

            file_ingest_service = await get_etl_service()
            await file_ingest_service.stop()
            log_info("File Ingest service stopped successfully")
        except Exception as e:
            log_error(f"Failed to stop File Ingest service: {e}")


@asynccontextmanager
async def app_lifespan(app: FastAPI):
    """
    FastAPI application lifecycle management.

    Database connections, caches, and other resources can be initialized here.
    """
    log_info("=" * 80)
    log_info("🚀 ContextBase API starting...")
    log_info("=" * 80)

    from src.version_engine.bootstrap.container import build_version_engine_container

    # Probe external storage at non-debug process boot so production fails fast
    # on misconfigured S3/Supabase. Local development/test runs often use partial
    # env files or offline services; keep those bootable and let individual
    # request paths surface dependency failures when exercised.
    app.state.version_engine = build_version_engine_container(
        probe=settings.APP_ENV not in {"development", "test"},
    )

    # Wire the outbox → agent-resolver bridge. Until a real runner
    # is installed via ``AgentResolverDispatcher.install(...)`` the
    # outbox hook gracefully defers agent-kind pending rows (logging
    # the deferral) — agent_review / agent_auto_resolve policies
    # still queue conflicts but they'll wait for a human in the
    # interim. The hook itself is a thin router; install your agent
    # backend wherever you boot model integration (typically in the
    # workers, e.g. ARQ ``WorkerSettings.on_startup``).
    from src.version_engine.derived.agent_resolver import (
        AgentResolverDispatcher,
        NoopAgentRunner,
        agent_resolver_outbox_hook,
    )
    from src.version_engine.derived.outbox import register_pending_conflict_hook

    register_pending_conflict_hook(agent_resolver_outbox_hook)
    if AgentResolverDispatcher.get() is None:
        AgentResolverDispatcher.install(NoopAgentRunner())

    _log_import_times()

    await _init_mcp_health_check()
    await _init_scheduler()
    await _init_file_ingest()
    _init_provider_registry()
    await _init_version_trees()
    _init_scope_sandbox_reaper(app)
    _init_entitlement_provisioner(app)
    _init_seat_proposal_worker(app)
    _init_runtime_billing_reaper(app)
    _init_storage_reconciler(app)

    log_info("📁 Filesystem sync: client-side via Git smart-HTTP (no server init needed)")

    total_startup_time = time.time() - APP_START_TIME
    log_info("")
    log_info("=" * 80)
    log_info(
        f"✨ ContextBase API startup complete! Total time: {total_startup_time * 1000:.2f}ms ({total_startup_time:.3f}s)"
    )
    log_info("=" * 80)
    log_info("")

    yield
    reaper = getattr(app.state, "scope_sandbox_reaper", None)
    if reaper is not None:
        task, stop_event = reaper
        stop_event.set()
        try:
            await task
        except Exception as e:
            log_error(f"Scope-sandbox reaper shutdown error: {e}")
    execution_reaper = getattr(app.state, "sandbox_execution_reaper", None)
    if execution_reaper is not None:
        task, stop_event = execution_reaper
        stop_event.set()
        try:
            await task
        except Exception as e:
            log_error(f"Sandbox execution reaper shutdown error: {e}")
    runtime_billing_reaper = getattr(app.state, "runtime_billing_reaper", None)
    if runtime_billing_reaper is not None:
        task, stop_event = runtime_billing_reaper
        stop_event.set()
        try:
            await task
        except Exception as e:
            log_error(f"Runtime billing reaper shutdown error: {e}")
    entitlement_provisioner = getattr(app.state, "entitlement_provisioner", None)
    if entitlement_provisioner is not None:
        task, stop_event = entitlement_provisioner
        stop_event.set()
        try:
            await task
        except Exception as e:
            log_error(f"Entitlement provisioner shutdown error: {e}")
    seat_proposal_worker = getattr(app.state, "seat_proposal_worker", None)
    if seat_proposal_worker is not None:
        task, stop_event = seat_proposal_worker
        stop_event.set()
        try:
            await task
        except Exception as e:
            log_error(f"Seat proposal worker shutdown error: {e}")
    storage_reconciler = getattr(app.state, "storage_reconciler", None)
    if storage_reconciler is not None:
        task, stop_event = storage_reconciler
        stop_event.set()
        try:
            await task
        except Exception as e:
            log_error(f"Storage reconciliation shutdown error: {e}")
    await _shutdown_services()


def create_app() -> FastAPI:
    """Create FastAPI application instance."""
    app_create_start = time.time()

    # Initialize FastAPI application
    fastapi_start = time.time()
    app = FastAPI(
        title="ContextBase API",
        description="Hostable context configuration and export platform",
        version="1.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=app_lifespan,
    )
    fastapi_duration = time.time() - fastapi_start

    # Configure CORS middleware
    cors_start = time.time()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.ALLOWED_HOSTS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    cors_duration = time.time() - cors_start

    # Request context + access log（X-Request-Id / latency / status_code）
    from src.utils.middleware import RequestContextMiddleware

    app.add_middleware(RequestContextMiddleware)

    # Keep lifecycle admission outside Version Engine internals while making
    # every FastAPI Product command hold a renewable Project write lease.
    from src.platform.project.write_lease import (
        get_leased_version_write_command_service,
        git_project_write_lease,
    )
    from src.version_engine.bootstrap.dependencies import (
        get_version_write_command_service,
    )

    app.dependency_overrides[get_version_write_command_service] = (
        get_leased_version_write_command_service
    )

    # Register routes
    router_register_start = time.time()
    app.include_router(table_router, prefix="/api/v1", tags=["tables"])
    app.include_router(tool_router, prefix="/api/v1", tags=["tools"])
    app.include_router(mcp_v3_router, prefix="/api/v1", tags=["mcp"])
    app.include_router(agent_router, prefix="/api/v1", tags=["agents"])
    app.include_router(agent_config_router, prefix="/api/v1", tags=["agent-config"])
    from src.platform.access.adapters.agent.chat.router import router as chat_router

    app.include_router(chat_router, prefix="/api/v1", tags=["chat"])
    app.include_router(context_publish_router, prefix="/api/v1", tags=["publishes"])
    # public short link: /p/{publish_key}
    app.include_router(context_publish_public_router, tags=["publishes"])

    # Legacy ingest compatibility router. Product-level Upload and Import
    # routes are registered separately below.
    app.include_router(ingest_router, prefix="/api/v1", tags=["ingest-compat"])
    from src.platform.upload.router import router as upload_router

    app.include_router(upload_router, prefix="/api/v1", tags=["upload"])
    from src.platform.office.router import router as office_router

    app.include_router(office_router, prefix="/api/v1", tags=["managed-office"])

    app.include_router(project_router, prefix="/api/v1", tags=["projects"])
    app.include_router(template_registry_router, prefix="/api/v1", tags=["templates"])
    from src.platform.repository_context.router import router as repository_context_router

    app.include_router(
        repository_context_router,
        prefix="/api/v1",
        tags=["repository-context"],
    )
    app.include_router(oauth_router, prefix="/api/v1", tags=["oauth"])
    app.include_router(
        internal_router, tags=["internal"]
    )  # Internal API does not use /api/v1 prefix
    from src.internal.mcp_runtime import router as mcp_runtime_router

    app.include_router(mcp_runtime_router, tags=["internal-mcp-runtime"])
    from src.version_engine.entrypoints.http.content import router as content_router

    app.include_router(content_router, prefix="/api/v1", tags=["content"])
    from src.version_engine.entrypoints.http.audit import router as audit_router

    app.include_router(audit_router, prefix="/api/v1", tags=["audit-logs"])
    from src.version_engine.entrypoints.http.conflict import router as conflict_router

    app.include_router(conflict_router, prefix="/api/v1/content", tags=["conflicts"])
    from src.version_engine.entrypoints.http.shadow_snapshot import router as shadow_router

    app.include_router(shadow_router, prefix="/api/v1", tags=["shadow-snapshots"])
    from src.version_engine.entrypoints.git.router import router as git_protocol_router

    app.include_router(
        git_protocol_router,
        tags=["git-protocol"],
        dependencies=[Depends(git_project_write_lease)],
    )
    # WebSocket /ws — server→client commit_update notifications.
    from src.version_engine.entrypoints.http.websocket import ws_router as version_ws_router

    app.include_router(version_ws_router, tags=["version-ws"])
    from src.version_engine.entrypoints.http.access_point_fs import router as ap_fs_router

    app.include_router(ap_fs_router, prefix="/api/v1", tags=["access-point-fs"])
    from src.platform.workspace.router import router as workspace_router

    app.include_router(workspace_router, prefix="/api/v1", tags=["workspace"])
    from src.platform.synchronize.router import router as synchronize_router

    app.include_router(synchronize_router, prefix="/api/v1", tags=["integrations"])
    from src.platform.synchronize.public_router import router as synchronize_public_router
    app.include_router(synchronize_public_router, prefix="/api/v1")
    # GitHub Synchronize: bind a project to a (repo, branch) pair, run
    # imports/exports, receive webhooks. Two routers because the webhook
    # callback isn't per-project.
    from src.platform.synchronize.github.router import (
        router as github_sync_router,
    )
    from src.platform.synchronize.github.router import (
        webhook_router as github_webhook_router,
    )

    from src.platform.synchronize.github.public_router import router as synchronize_github_router
    from src.platform.synchronize.github.public_router import webhook_router as synchronize_github_webhook_router

    app.include_router(synchronize_github_router)
    app.include_router(synchronize_github_webhook_router)
    app.include_router(github_sync_router, tags=["github-integration"])
    app.include_router(github_webhook_router, tags=["github-integration"])
    from src.platform.scope_sandbox.router import router as scope_sandbox_router

    app.include_router(scope_sandbox_router, tags=["scope-sandboxes"])
    from src.platform.scope_sync.router import router as scope_sync_router

    app.include_router(scope_sync_router, tags=["scope-sync"])
    from src.platform.auth.router import router as auth_router

    app.include_router(auth_router, prefix="/api/v1", tags=["auth"])
    app.include_router(analytics_router, tags=["analytics"])
    app.include_router(profile_router, tags=["profile"])
    app.include_router(imports_router, prefix="/api/v1", tags=["imports"])
    app.include_router(activity_router, prefix="/api/v1", tags=["activity"])
    from src.platform.imports.database.public_router import router as import_database_sources_router

    app.include_router(import_database_sources_router, prefix="/api/v1")
    app.include_router(database_import_router, prefix="/api/v1", tags=["db-connector"])
    app.include_router(organization_router, prefix="/api/v1", tags=["organizations"])
    from src.platform.billing.router import router as billing_router

    app.include_router(billing_router, prefix="/api/v1", tags=["billing"])
    from src.platform.managed_ai.router import internal_router as managed_ai_internal_router
    from src.platform.managed_ai.router import router as managed_ai_router

    app.include_router(managed_ai_router, prefix="/api/v1")
    app.include_router(managed_ai_internal_router)
    from src.platform.access.adapters.mcp_endpoint.router import router as mcp_endpoint_router

    app.include_router(mcp_endpoint_router, prefix="/api/v1", tags=["mcp-endpoints"])
    from src.platform.landing.router import router as landing_router

    app.include_router(landing_router, prefix="/api/v1", tags=["landing"])
    from src.platform.access.adapters.sandbox_endpoint.router import (
        router as sandbox_endpoint_router,
    )

    app.include_router(sandbox_endpoint_router, prefix="/api/v1", tags=["sandbox-endpoints"])
    from src.platform.project.dashboard_router import router as dashboard_router

    app.include_router(dashboard_router, prefix="/api/v1", tags=["projects"])
    from src.platform.access.public_router import project_router as public_project_access_router
    from src.platform.access.public_router import router as public_access_router
    from src.platform.access.router import router as access_router

    # Canonical static prefix must precede legacy /access/{connection_id}.
    app.include_router(public_access_router, prefix="/api/v1")
    app.include_router(public_project_access_router, prefix="/api/v1")
    app.include_router(access_router, prefix="/api/v1", tags=["access"])
    from src.provider.accounts.router import router as gateway_router

    app.include_router(gateway_router, prefix="/api/v1", tags=["gateways"])

    # Repository data-plane surface: scope CRUD, repo identity, connectors.
    from src.platform.access.project_router import router as project_access_router
    from src.repo.identity_router import router as repo_identity_router
    from src.repo.scope_router import router as repo_scope_router

    app.include_router(repo_scope_router, prefix="/api/v1", tags=["repo-scopes"])
    app.include_router(repo_identity_router, prefix="/api/v1", tags=["repo-identity"])
    app.include_router(project_access_router, prefix="/api/v1", tags=["connectors"])
    router_register_duration = time.time() - router_register_start

    # Register exception handlers
    exception_handler_start = time.time()
    app.add_exception_handler(AppException, app_exception_handler)  # type: ignore
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore
    app.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore
    app.add_exception_handler(  # type: ignore
        SecurityStoreUnavailable,
        security_store_unavailable_handler,
    )
    app.add_exception_handler(Exception, generic_exception_handler)  # type: ignore
    exception_handler_duration = time.time() - exception_handler_start

    app_create_duration = time.time() - app_create_start

    # Unified logging output (setup_logging already called at top of file)
    log_info("⚙️  FastAPI app creation time breakdown:")
    log_info(f"  ├─ FastAPI instantiation: {fastapi_duration * 1000:.2f}ms")
    log_info(f"  ├─ CORS middleware config: {cors_duration * 1000:.2f}ms")
    log_info(f"  ├─ Route registration: {router_register_duration * 1000:.2f}ms")
    log_info(f"  └─ Exception handler registration: {exception_handler_duration * 1000:.2f}ms")
    log_info(f"📦 Total app creation time: {app_create_duration * 1000:.2f}ms")
    log_info("")

    return app


# Create application instance
app = create_app()


async def _build_readiness_report(mcp_service) -> dict:

    env_status = {
        "supabase_configured": bool(os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_KEY")),
        "s3_configured": bool(os.getenv("S3_BUCKET_NAME")),
        "mineru_configured": bool(os.getenv("MINERU_API_KEY")),
        "anthropic_configured": bool(os.getenv("ANTHROPIC_API_KEY")),
        "e2b_configured": bool(os.getenv("E2B_API_KEY")),
        "internal_api_secret_configured": bool((settings.INTERNAL_API_SECRET or "").strip()),
    }

    config_errors: list[str] = []
    from src.infra.readiness import core_dependency_errors

    dependency_errors = await core_dependency_errors()

    if not settings.DEBUG and not env_status["internal_api_secret_configured"]:
        config_errors.append("INTERNAL_API_SECRET is empty while DEBUG is False")

    # Cache MCP health check to avoid blocking every /health call (~12s timeout)
    import time as _time

    _now = _time.time()
    if not settings.MCP_SERVER_URL:
        mcp_status = {"status": "disabled"}
    elif (
        not hasattr(_build_readiness_report, "_mcp_cache")
        or _now - _build_readiness_report._mcp_cache_time > 60
    ):
        try:
            mcp_status = await mcp_service.check_mcp_server_health()
        except Exception as e:
            mcp_status = {"status": "unhealthy", "error": str(e)}
        _build_readiness_report._mcp_cache = mcp_status
        _build_readiness_report._mcp_cache_time = _now
    else:
        mcp_status = _build_readiness_report._mcp_cache

    mcp_state = str(mcp_status.get("status", "")).strip().lower()
    if mcp_state in {"", "unhealthy", "error", "down", "unavailable"}:
        dependency_errors.append("MCP server is unhealthy")

    if config_errors:
        status = "unhealthy"
    elif dependency_errors:
        status = "degraded"
    else:
        status = "ready"

    return {
        "status": status,
        "service": "ContextBase API",
        "version": settings.VERSION,
        "environment": env_status,
        "mcp_status": mcp_status,
        "errors": {
            "config": config_errors,
            "dependencies": dependency_errors,
        },
    }


@app.get("/live")
async def live_check():
    """Liveness: only indicates the process is alive."""
    return {
        "status": "alive",
        "service": "ContextBase API",
        "version": settings.VERSION,
    }


@app.get("/ready")
async def ready_check(
    response: Response,
    mcp_service=Depends(get_mcp_health_client),
):
    """Readiness: indicates whether the service can accept traffic."""
    report = await _build_readiness_report(mcp_service)
    if report["status"] != "ready":
        response.status_code = 503
    return report


@app.get("/health")
async def health_check(
    response: Response,
):
    """Fast health check — skips slow MCP probe to avoid blocking traffic."""
    report = {
        "status": "ready",
        "service": "ContextBase API",
        "version": settings.VERSION,
    }
    if report["status"] != "ready":
        response.status_code = 503
    return report


# Startup command example:
# uvicorn src.main:app --host 0.0.0.0 --port 9090 --reload --log-level info --no-access-log
