import json
from pathlib import Path
from typing import Any, Literal

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration"""

    model_config = SettingsConfigDict(
        env_file=".env", case_sensitive=True, extra="ignore", env_file_encoding="utf-8"
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        # Explicit constructor/launch settings win; .env supplies local defaults.
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
        )

    # Service configuration
    APP_NAME: str = "ContextBase"
    APP_ENV: Literal["development", "test", "staging", "production"] = Field(
        default="development",
        validation_alias=AliasChoices("APP_ENV", "ENVIRONMENT"),
    )
    DEBUG: bool | None = None
    VERSION: str = "0.0.3"

    # Local storage configuration, mostly using Supabase now
    DATA_PATH: Path = Path("./data")
    STORAGE_TYPE: Literal["json", "db", "supabase"] = "supabase"

    # CORS configuration
    ALLOWED_HOSTS: list[str] | None = None

    @staticmethod
    def _parse_hosts_string(raw: str) -> list[str]:
        """Parse a string value into a list of host strings."""
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    "ALLOWED_HOSTS must be a JSON array or comma-separated string"
                ) from exc
            if not isinstance(parsed, list):
                raise ValueError("ALLOWED_HOSTS JSON value must be an array")
            return [str(item) for item in parsed]
        return [item.strip() for item in raw.split(",")]

    @staticmethod
    def _normalize_host_list(hosts: list[str]) -> list[str]:
        """Strip empty entries and trailing slashes (except for '*')."""
        return [host if host == "*" else host.rstrip("/") for host in hosts if host]

    @field_validator("ALLOWED_HOSTS", mode="before")
    @classmethod
    def normalize_allowed_hosts(cls, value: Any) -> Any:
        """Supports JSON arrays, single value strings, or comma-separated strings."""
        if value is None:
            return None

        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return []
            hosts = cls._parse_hosts_string(raw)
        elif isinstance(value, list):
            hosts = [str(item).strip() for item in value]
        else:
            return value

        return cls._normalize_host_list(hosts)

    @model_validator(mode="after")
    def apply_runtime_defaults(self):
        """Apply default configuration based on environment to reduce production misconfiguration risk."""
        if self.DEBUG is None:
            self.DEBUG = self.APP_ENV in {"development", "test"}

        # Retain the Desktop orchestrator's explicit public locator override.
        # Existing launchers use it to keep loopback Git remote responses local.
        if self.PUPPYONE_PUBLIC_URL_OVERRIDE.strip():
            self.PUBLIC_URL = self.PUPPYONE_PUBLIC_URL_OVERRIDE.strip()

        if self.ALLOWED_HOSTS is None:
            if self.APP_ENV in {"development", "test"}:
                # Next.js auto-rolls forward (3000 → 3001 → 3002 → …) when
                # an earlier port is occupied (orphan dev servers, other IDE
                # workers, etc.), and a missing entry here surfaces as every
                # OPTIONS preflight returning 400 with no useful UI signal.
                # Cover Next.js (3000-3004) and the Electron/Vite desktop
                # renderer (5173-5177) so a couple of port bumps don't
                # silently break the local stack. Explicit env var
                # ALLOWED_HOSTS still overrides this list for non-default
                # setups.
                dev_ports = [3000, 3001, 3002, 3003, 3004, 5173, 5174, 5175, 5176, 5177]
                self.ALLOWED_HOSTS = [
                    origin
                    for port in dev_ports
                    for origin in (f"http://localhost:{port}", f"http://127.0.0.1:{port}")
                ]
            else:
                self.ALLOWED_HOSTS = ["*"] if self.DEBUG else []

        return self

    @model_validator(mode="after")
    def enforce_skip_auth_safety(self):
        """Refuse to boot if SKIP_AUTH=True outside development/test.

        SKIP_AUTH bypasses ALL authentication and returns a hardcoded mock
        user, both for the platform JWT pipeline (`platform/auth/dependencies`)
        and the hash access-key pipeline (`version_engine/server/auth`). Leaving it
        on in staging/production would expose every endpoint as anonymous.

        Failing fast here means the application crashes at startup instead of
        silently serving an open-door API. There is no legitimate reason to
        ever enable SKIP_AUTH in a non-dev environment, so we refuse to start
        rather than degrade silently.
        """
        if self.SKIP_AUTH and self.APP_ENV not in {"development", "test"}:
            raise ValueError(
                f"SKIP_AUTH=True is only permitted when APP_ENV is "
                f"'development' or 'test'. Got APP_ENV={self.APP_ENV!r}. "
                f"Refusing to start with authentication disabled in "
                f"{self.APP_ENV} — this would expose every endpoint as "
                f"anonymous. Unset SKIP_AUTH or set APP_ENV=development."
            )
        return self

    @model_validator(mode="after")
    def enforce_managed_ai_safety(self):
        if self.MANAGED_AI_ENABLED:
            if self.SKIP_AUTH:
                raise ValueError("Managed AI requires authenticated users; SKIP_AUTH must be false")
            if not self.PUPPYPAY_BASE_URL or len(self.PUPPYPAY_INTERNAL_API_SECRET) < 32:
                raise ValueError("Managed AI requires the private PuppyPay gateway")
            if self.APP_ENV in {"staging", "production"}:
                if not self.PUPPYPAY_BASE_URL.startswith("https://"):
                    raise ValueError("Managed AI requires HTTPS for PuppyPay")
                if (
                    len(self.INTERNAL_API_SECRET) < 32
                    or self.INTERNAL_API_SECRET == self.PUPPYPAY_INTERNAL_API_SECRET
                ):
                    raise ValueError("Managed AI requires distinct service credentials")
        return self

    @model_validator(mode="after")
    def enforce_entitlements_safety(self):
        """Fail closed on contradictory hosted billing configuration."""
        if self.BILLING_ENFORCEMENT != "disabled" and self.ENTITLEMENTS_MODE == "disabled":
            raise ValueError(
                "BILLING_ENFORCEMENT requires ENTITLEMENTS_MODE to be 'db' or 'local'."
            )
        hosted_environment = self.APP_ENV in {"staging", "production"}
        hosted_global_enabled = hosted_environment and self.BILLING_ENFORCEMENT != "disabled"
        hosted_db_projection = hosted_environment and self.ENTITLEMENTS_MODE == "db"
        db_global_enabled = (
            self.ENTITLEMENTS_MODE == "db" and self.BILLING_ENFORCEMENT != "disabled"
        )
        integration_enabled = (
            hosted_global_enabled
            or hosted_db_projection
            or db_global_enabled
            or any(
                (
                    self.BILLING_UI_ENABLED,
                    self.BILLING_WRITES_ENABLED,
                    self.SEAT_BILLING_MODE != "disabled",
                    self.RUNTIME_METERING_MODE != "disabled",
                    self.STORAGE_ENFORCEMENT_MODE != "disabled",
                )
            )
        )
        if integration_enabled and not self.PUPPYPAY_BASE_URL:
            raise ValueError("PUPPYPAY_BASE_URL is required when billing integration is enabled")
        if integration_enabled:
            if self.ENTITLEMENTS_MODE != "db":
                raise ValueError("Hosted billing integration requires ENTITLEMENTS_MODE='db'")
            if len(self.PUPPYPAY_INTERNAL_API_SECRET) < 32:
                raise ValueError("PUPPYPAY_INTERNAL_API_SECRET must contain at least 32 characters")
        if (
            self.APP_ENV in {"staging", "production"}
            and integration_enabled
            and not self.PUPPYPAY_BASE_URL.startswith("https://")
        ):
            raise ValueError("PUPPYPAY_BASE_URL must use HTTPS in hosted environments")
        if (
            self.APP_ENV in {"staging", "production"}
            and integration_enabled
            and self.PUPPYPAY_INTERNAL_API_SECRET == self.INTERNAL_API_SECRET
        ):
            raise ValueError(
                "PUPPYPAY_INTERNAL_API_SECRET must be distinct from INTERNAL_API_SECRET"
            )
        if self.BILLING_WRITES_ENABLED and not self.BILLING_UI_ENABLED:
            raise ValueError("BILLING_WRITES_ENABLED requires BILLING_UI_ENABLED")
        if self.RUNTIME_AGENT_TIMEOUT_SECONDS > self.RUNTIME_AGENT_MAX_UNITS * 60:
            raise ValueError(
                "RUNTIME_AGENT_TIMEOUT_SECONDS cannot exceed the authorized "
                "RUNTIME_AGENT_MAX_UNITS at 60 seconds per standard RU"
            )
        minimum_recovery_retry = int(self.PUPPYPAY_TIMEOUT_SECONDS * 2) + 5
        if minimum_recovery_retry > self.RUNTIME_BILLING_RECOVERY_RETRY_SECONDS:
            raise ValueError(
                "RUNTIME_BILLING_RECOVERY_RETRY_SECONDS must exceed two PuppyPay transport timeouts"
            )
        return self

    @model_validator(mode="after")
    def enforce_project_deletion_quiescence(self):
        """Keep physical cleanup behind every admitted Git write window.

        A Project delete removes authorization immediately, but a smart-HTTP
        request that already received a runtime grant may still be flushing
        immutable objects.  The S3 bridge has a 30-minute upper wait budget;
        use that as a hard floor even when the Git subprocess timeout is
        configured lower.
        """

        minimum_quiescence = max(self.GIT_SUBPROCESS_TIMEOUT_SECONDS, 30 * 60)
        if minimum_quiescence > self.PROJECT_DELETION_QUIESCENCE_SECONDS:
            raise ValueError(
                "PROJECT_DELETION_QUIESCENCE_SECONDS must be at least "
                f"{minimum_quiescence} seconds so admitted Git writes quiesce "
                "before Project-owned objects are purged."
            )
        return self

    @model_validator(mode="after")
    def enforce_host_derived_storage_ephemerality(self):
        """Hosted local workspaces/Git views must remain disposable caches.

        Durable Project deletion is global, while this process can only scrub
        its own filesystem. Until a shared/distributed cleanup adapter exists,
        accepting persistent replica-local derived storage would create a false
        deletion guarantee, so hosted startup fails closed.
        """

        if (
            self.APP_ENV in {"staging", "production"}
            and self.HOST_DERIVED_STORAGE_MODE != "ephemeral"
        ):
            raise ValueError(
                "Hosted WORKSPACE_BASE_DIR and GIT_VIEW_CACHE_DIR must use "
                "ephemeral derived storage; persistent host storage requires "
                "an explicit shared/distributed cleanup adapter"
            )
        return self

    @model_validator(mode="after")
    def enforce_access_credential_secret_safety(self):
        """Hosted runtimes must use a dedicated stable HMAC secret for Access keys."""
        if self.APP_ENV not in {"development", "test"}:
            secret = (self.ACCESS_CREDENTIAL_HASH_SECRET or "").strip()
            if (
                len(secret) < 32
                or secret == "ContextBase-access-credential-development-secret"
                or secret in {self.JWT_SECRET, self.INTERNAL_API_SECRET}
            ):
                raise ValueError(
                    "ACCESS_CREDENTIAL_HASH_SECRET must be dedicated, non-default, "
                    "and at least 32 characters outside development/test"
                )
        return self

    @model_validator(mode="after")
    def enforce_auth_security_store_safety(self):
        """Hosted auth controls require one shared, cross-replica Redis."""
        if self.APP_ENV not in {"development", "test"}:
            if not self.auth_security_redis_url:
                raise ValueError(
                    "AUTH_SECURITY_REDIS_URL (or legacy RATELIMIT_REDIS_URL / "
                    "ETL_REDIS_URL) is required outside development/test"
                )
            if not self.NOTIFICATIONS_REDIS_URL:
                raise ValueError("NOTIFICATIONS_REDIS_URL is required outside development/test")
            if not self.DESKTOP_AUTH_PUBLIC_BASE_URL.startswith("https://"):
                raise ValueError(
                    "DESKTOP_AUTH_PUBLIC_BASE_URL must use https outside development/test"
                )
        return self

    @model_validator(mode="after")
    def enforce_jwt_secret_safety(self):
        """Refuse to boot with a weak/default JWT_SECRET outside development/test.

        JWT_SECRET signs user session JWTs and MCP tokens. If the in-source
        default ("ContextBase-256-bit-secret") ever ships to staging/production,
        anyone can forge tokens and bypass authentication. Mirrors
        enforce_skip_auth_safety: fail fast at startup rather than serve a
        forgeable signing key.
        """
        if self.APP_ENV not in {"development", "test"}:
            secret = (self.JWT_SECRET or "").strip()
            if not secret or secret == "ContextBase-256-bit-secret" or len(secret) < 32:
                raise ValueError(
                    "JWT_SECRET must be a strong, non-default value (>=32 chars) "
                    f"outside development/test. Got APP_ENV={self.APP_ENV!r} with a "
                    "missing/default/short secret. Refusing to start with a forgeable "
                    "signing key — set JWT_SECRET to a securely generated random value."
                )
        return self

    @model_validator(mode="after")
    def enforce_mcp_token_secret_safety(self):
        """MCP JWTs have a separate trust domain and always expire."""
        if self.APP_ENV not in {"development", "test"}:
            secret = (self.MCP_TOKEN_SECRET or "").strip()
            if (
                not secret
                or secret == "ContextBase-mcp-development-secret"
                or len(secret) < 32
                or secret == self.JWT_SECRET
            ):
                raise ValueError("MCP_TOKEN_SECRET must be distinct, non-default, and >=32 chars")
            if self.MCP_TOKEN_TTL_SECONDS <= 0:
                raise ValueError("MCP_TOKEN_TTL_SECONDS must be positive")
        return self

    @model_validator(mode="after")
    def enforce_hosted_sandbox_safety(self):
        """Hosted deployments may never fall back to a shared-kernel Docker sandbox."""
        if self.APP_ENV in {"staging", "production"}:
            if self.SANDBOX_TYPE != "e2b":
                raise ValueError("Hosted SANDBOX_TYPE must be 'e2b' (no Docker fallback)")
            if not self.E2B_API_KEY:
                raise ValueError("E2B_API_KEY is required for hosted sandbox execution")
            if self.SCOPE_SANDBOX_STORE != "supabase":
                raise ValueError("Hosted sandbox sessions require SCOPE_SANDBOX_STORE=supabase")
            if not self.SCOPE_SANDBOX_REAPER_ENABLED:
                raise ValueError(
                    "Hosted sandbox sessions require SCOPE_SANDBOX_REAPER_ENABLED=true"
                )
        return self

    @model_validator(mode="after")
    def enforce_git_transport_caps(self):
        if self.APP_ENV not in {"development", "test"} and (
            self.GIT_MAX_RECEIVE_PACK_BYTES <= 0 or self.GIT_MAX_UPLOAD_PACK_BYTES <= 0
        ):
            raise ValueError("Hosted Git transport hard caps must be positive")
        return self

    # JWT configuration
    JWT_SECRET: str = "ContextBase-256-bit-secret"
    JWT_ALGORITHM: str = "HS256"
    MCP_TOKEN_SECRET: str = "ContextBase-mcp-development-secret"
    MCP_TOKEN_AUDIENCE: str = "puppyone-mcp"
    MCP_TOKEN_TTL_SECONDS: int = 30 * 24 * 60 * 60  # 30 days
    # Shared fail-closed store for OAuth one-time state and auth rate limits.
    AUTH_SECURITY_REDIS_URL: str = ""
    # Compatibility inputs retained for deployments predating the shared auth
    # security store. They are never fail-open fallbacks: when selected, the
    # Redis store remains mandatory and every operation still fails closed.
    RATELIMIT_REDIS_URL: str = ""
    ETL_REDIS_URL: str = ""
    # Browser/CLI-reachable Supabase origin. Empty means SUPABASE_URL is already public.
    SUPABASE_PUBLIC_URL: str = ""
    # Browser-reachable Puppyone web origin used by the Desktop login handoff.
    FRONTEND_URL: str = ""
    DESKTOP_AUTH_PUBLIC_BASE_URL: str = ""
    DESKTOP_AUTH_ALLOWED_CALLBACKS: str = "puppyone://auth/callback"
    DESKTOP_AUTH_STATE_TTL_SECONDS: int = 10 * 60
    DESKTOP_AUTH_EXCHANGE_TTL_SECONDS: int = 60
    # Cluster-aware version notifications (ISSUE-015). When set, WebSocket
    # commit_update events are fanned out across replicas via Redis pub/sub.
    # Local development may leave this empty; hosted validation requires it.
    NOTIFICATIONS_REDIS_URL: str = ""

    @property
    def auth_security_redis_url(self) -> str:
        """Resolve the shared Redis store without breaking older deployments.

        ``AUTH_SECURITY_REDIS_URL`` is the explicit modern setting. Before it
        existed, hosted installs commonly supplied either the old rate-limit
        URL or the shared ARQ URL. All three are Redis connection strings; the
        auth store uses its own key namespace and remains fail-closed.
        """

        return next(
            (
                value.strip()
                for value in (
                    self.AUTH_SECURITY_REDIS_URL,
                    self.RATELIMIT_REDIS_URL,
                    self.ETL_REDIS_URL,
                )
                if value.strip()
            ),
            "",
        )

    # Anthropic configuration
    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_BASE_URL: str = ""
    ANTHROPIC_MODEL: str = "claude-sonnet-4-5-20250929"

    # Sandbox configuration
    # - "e2b": Use E2B cloud sandbox (requires E2B_API_KEY)
    # - "docker": Use local Docker container sandbox
    # - "auto": Auto-select (use E2B if E2B_API_KEY is available, otherwise use Docker)
    SANDBOX_TYPE: Literal["e2b", "docker", "auto"] = "auto"
    E2B_API_KEY: str = ""
    CLOUD_AGENT_IMAGE: str = "puppyone-cloud-agent:workspace-v2"
    CLOUD_AGENT_E2B_TEMPLATE: str = ""
    CLOUD_AGENT_DEFAULT_MODEL: str = ""
    CLOUD_AGENT_MAX_TOKENS: int = Field(default=4096, ge=1, le=32768)
    CLOUD_AGENT_CONTEXT_WINDOW: int = Field(default=131072, ge=16384, le=2000000)
    CLOUD_AGENT_MAX_MODEL_CALLS: int = Field(default=32, ge=1, le=100)
    CLOUD_AGENT_CONCURRENCY: int = Field(default=4, ge=1, le=32)
    # Docker sandbox dedicated temp directory; only needed when containerized backend controls host Docker
    SANDBOX_TMPDIR: str | None = None
    # Sandbox file download concurrency
    SANDBOX_DOWNLOAD_CONCURRENCY: int = 10
    # Large file streaming threshold (bytes); files exceeding this size use streaming transfer
    SANDBOX_LARGE_FILE_THRESHOLD: int = 50 * 1024 * 1024  # 50MB
    # ── Scope-sandbox (V2 "sandbox as access point") ──
    # NOTE: the per-project/enterprise choice of "fly" vs "e2b" is a USER
    # selection made in the frontend and persisted as a project setting; it is
    # resolved at runtime via factory.provider_from_settings(settings, name=...).
    # The env var below is only the DEFAULT/fallback for projects that haven't
    # chosen. The vars here hold the per-provider CREDENTIALS (always needed for
    # whichever provider a project selects). See docs/proposals/PUP-sandbox-access-point.md.
    # Default only; the per-project UI choice overrides. Defaults to "e2b" — the
    # live-validated path (full SSH + credential round-trip proven). Fly is
    # code-complete but not yet live (needs payment + dedicated IPv4), so it
    # shouldn't be the silent fallback for projects that haven't chosen.
    SCOPE_SANDBOX_PROVIDER: Literal["fly", "e2b"] = "e2b"
    SCOPE_SANDBOX_FLY_APP: str = ""
    SCOPE_SANDBOX_FLY_TOKEN: str = ""
    SCOPE_SANDBOX_FLY_IMAGE: str = ""
    # Custom E2B template (roadmap #6) baking sshd+websocat+sidecar. When set,
    # the provider launches this template and the bootstrap uses the FAST provision
    # path (seed key + start pre-installed daemons). Unset → default template +
    # full runtime install. Build via sandbox/scope-e2b/build.sh.
    SCOPE_SANDBOX_E2B_TEMPLATE: str = ""
    # Session store backend: "memory" (dev/single-process) or "supabase"
    # (durable, multi-worker-visible — required for the reaper + multi-instance).
    SCOPE_SANDBOX_STORE: Literal["memory", "supabase"] = "supabase"
    # Background reaper is part of the lifecycle correctness contract: without
    # it a worker crash can leave paid provider resources alive indefinitely.
    SCOPE_SANDBOX_REAPER_ENABLED: bool = True
    SCOPE_SANDBOX_REAPER_INTERVAL_S: int = 120

    # Workspace Provider configuration
    # - "auto": Auto-detect platform (macOS -> APFS Clone, Linux -> OverlayFS, other -> full copy)
    # - "apfs": Force APFS Clone (macOS only)
    # - "overlayfs": Force OverlayFS (Linux only)
    # - "fallback": Force full copy
    WORKSPACE_PROVIDER: str = "auto"
    WORKSPACE_BASE_DIR: str = "/tmp/contextbase"
    # These paths contain only reconstructable, authorization-fenced derived
    # state. "persistent" is allowed for local/self-hosted experimentation but
    # hosted startup rejects it until a global cleanup adapter is implemented.
    HOST_DERIVED_STORAGE_MODE: Literal["ephemeral", "persistent"] = "ephemeral"

    # Test configuration
    SKIP_AUTH: bool = False  # Whether to skip authentication (for test environments only)

    # ETL configuration
    # - None: Auto mode (disabled by default in local DEBUG, enabled by default in non-DEBUG)
    # - True/False: Force enable/disable (can be overridden via ENABLE_ETL env variable)
    ENABLE_ETL: bool | None = None

    # OCR / Smart-Parse pipeline switch.
    #
    # When False, file ingest requests asking for `mode="ocr_parse"`
    # are silently downgraded to `mode="raw"` (S3 upload + completed
    # task, no MineRU/LLM round-trip). This is intentionally a
    # separate flag from ENABLE_ETL because raw uploads still go
    # through the ETL service (just not the OCR branch) and we
    # want the rest of that pipeline alive.
    #
    # Default False: the smart-parse path is paused while we
    # rework it. Flip to True (or set `ENABLE_OCR=true` in the
    # environment) to bring it back online — no other code change
    # needed; the router branch is preserved as-is.
    ENABLE_OCR: bool = False

    @property
    def etl_enabled(self) -> bool:
        """Whether ETL is enabled (controls both ETL route imports and ETL service startup)"""
        if self.ENABLE_ETL is not None:
            return self.ENABLE_ETL
        return not self.DEBUG

    # Notion configuration
    # Method 1: Internal Integration (simple, only requires API Key)
    NOTION_API_KEY: str = (
        ""  # Format: secret_xxx, obtained from https://www.notion.so/my-integrations
    )
    # Method 2: OAuth (suitable for multi-user scenarios)
    # ========== OAuth configuration ==========
    # Unified format: /oauth/{provider}/callback

    # Notion OAuth configuration
    NOTION_CLIENT_ID: str = ""
    NOTION_CLIENT_SECRET: str = ""
    NOTION_REDIRECT_URI: str = "http://localhost:3000/oauth/notion/callback"

    # GitHub OAuth configuration
    GITHUB_CLIENT_ID: str = ""
    GITHUB_CLIENT_SECRET: str = ""
    GITHUB_REDIRECT_URI: str = "http://localhost:3000/oauth/github/callback"

    # Google OAuth configuration (all Google services share the same OAuth Client)
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_SHEETS_REDIRECT_URI: str = "http://localhost:3000/oauth/google-sheets/callback"
    GMAIL_REDIRECT_URI: str = "http://localhost:3000/oauth/gmail/callback"
    GOOGLE_DRIVE_REDIRECT_URI: str = "http://localhost:3000/oauth/google-drive/callback"
    GOOGLE_CALENDAR_REDIRECT_URI: str = "http://localhost:3000/oauth/google-calendar/callback"
    GOOGLE_DOCS_REDIRECT_URI: str = "http://localhost:3000/oauth/google-docs/callback"
    GOOGLE_SEARCH_CONSOLE_REDIRECT_URI: str = (
        "http://localhost:3000/oauth/google-search-console/callback"
    )

    # Linear OAuth configuration
    LINEAR_CLIENT_ID: str = ""
    LINEAR_CLIENT_SECRET: str = ""
    LINEAR_REDIRECT_URI: str = "http://localhost:3000/oauth/linear/callback"

    # Airtable OAuth configuration
    AIRTABLE_CLIENT_ID: str = ""
    AIRTABLE_CLIENT_SECRET: str = ""
    AIRTABLE_REDIRECT_URI: str = "http://localhost:3000/oauth/airtable/callback"

    # Inter-service communication
    INTERNAL_API_SECRET: str = ""  # Internal service communication secret
    MCP_SERVER_URL: str = ""  # MCP service address
    ACCESS_CREDENTIAL_HASH_SECRET: str = "ContextBase-access-credential-development-secret"
    # Verification only during key rotation; new credentials always use the primary key.
    ACCESS_CREDENTIAL_PREVIOUS_HASH_SECRET: str = ""

    # Product entitlements / billing enforcement.
    # disabled: open-source/self-hosted default, no product limits enforced.
    # local: read a local JSON entitlement snapshot, useful for tests/self-hosted overrides.
    # db: read organization_entitlements, written by PuppyPay through internal API.
    ENTITLEMENTS_MODE: Literal["disabled", "local", "db"] = "disabled"
    BILLING_ENFORCEMENT: Literal["disabled", "shadow", "required"] = "disabled"
    LOCAL_ENTITLEMENTS_FILE: str | None = None
    MANAGED_AI_ENABLED: bool = False
    MANAGED_AI_OPENROUTER_KEY: str = ""
    BILLING_UI_ENABLED: bool = False
    BILLING_WRITES_ENABLED: bool = False
    SEAT_BILLING_MODE: Literal["disabled", "shadow", "required"] = "disabled"
    RUNTIME_METERING_MODE: Literal["disabled", "shadow", "required"] = "disabled"
    STORAGE_ENFORCEMENT_MODE: Literal["disabled", "shadow", "required"] = "disabled"
    PUPPYPAY_BASE_URL: str = ""
    PUPPYPAY_INTERNAL_API_SECRET: str = ""
    PUPPYPAY_TIMEOUT_SECONDS: float = Field(default=10.0, gt=0, le=60)
    ENTITLEMENT_PROVISIONING_INTERVAL_SECONDS: int = Field(default=30, ge=10, le=3600)
    ENTITLEMENT_PROVISIONING_BATCH_SIZE: int = Field(default=25, ge=1, le=100)
    ENTITLEMENT_PROVISIONING_LEASE_SECONDS: int = Field(default=60, ge=10, le=3600)
    SEAT_PROPOSAL_INTERVAL_SECONDS: int = Field(default=30, ge=10, le=3600)
    SEAT_PROPOSAL_BATCH_SIZE: int = Field(default=25, ge=1, le=100)
    SEAT_PROPOSAL_LEASE_SECONDS: int = Field(default=60, ge=10, le=3600)
    RUNTIME_DEFAULT_RESERVATION_UNITS: int = Field(default=10, gt=0, le=10_000)
    RUNTIME_AGENT_MAX_UNITS: int = Field(
        default=30,
        gt=0,
        le=10_000,
        description="Upper-bound RU reservation for one Agent execution",
    )
    RUNTIME_AGENT_TIMEOUT_SECONDS: int = Field(default=1800, ge=60, le=86_400)
    RUNTIME_RESERVATION_CLAIM_SECONDS: int = Field(default=120, ge=30, le=3600)
    RUNTIME_BILLING_HEARTBEAT_SECONDS: int = Field(default=300, ge=30, le=3600)
    RUNTIME_BILLING_RECOVERY_INTERVAL_SECONDS: int = Field(default=60, ge=10, le=3600)
    RUNTIME_BILLING_RECOVERY_RETRY_SECONDS: int = Field(default=180, ge=30, le=3600)
    STORAGE_RECONCILIATION_INTERVAL_SECONDS: int = Field(
        default=3600,
        ge=60,
        le=86400,
    )
    STORAGE_RECONCILIATION_MIN_AGE_SECONDS: int = Field(
        default=86400,
        ge=60,
        le=2_592_000,
    )
    STORAGE_RECONCILIATION_BATCH_SIZE: int = Field(default=25, ge=1, le=200)

    # Shared secret for the login-free landing "X → MCP" preview endpoint.
    # When set, /api/v1/landing/preview requires header X-Landing-Secret to
    # match — so only the marketing site's server-side proxy can reach it
    # (the proxy adds CAPTCHA + per-IP rate limiting). Unset = open (dev only).
    LANDING_INGEST_SECRET: str | None = None

    # Abuse control for the public landing preview endpoint.
    # Per-IP sliding-window rate limit (in-process; single-deployment safe).
    LANDING_PREVIEW_RATE_WINDOW: int = 600  # seconds
    LANDING_PREVIEW_RATE_MAX: int = 12  # max previews / window / IP
    # Cloudflare Turnstile secret. When set, /landing/preview requires a valid
    # turnstile token (verified server-side). Unset = CAPTCHA disabled (dev).
    TURNSTILE_SECRET: str | None = None

    # Public access URL (used to generate external API links)
    # - Local development: http://localhost:8000
    # - Railway: https://your-app.railway.app
    # - If not set, it will be auto-inferred from request headers
    PUBLIC_URL: str = ""
    PUPPYONE_PUBLIC_URL_OVERRIDE: str = ""

    # Context Publish configuration
    PUBLISH_DEFAULT_EXPIRES_DAYS: int = 7
    PUBLISH_KEY_LENGTH: int = 16
    PUBLISH_CACHE_TTL_SECONDS: int = 10

    # Search Tool indexing (async)
    # - Only used for async indexing wait_for timeout control, preventing background tasks from hanging indefinitely
    SEARCH_INDEX_TIMEOUT_SECONDS: int = 120

    # Git-native version engine hardening.
    VERSION_OUTBOX_ENABLED: bool = True
    VERSION_OUTBOX_INTERVAL_SECONDS: int = 30
    VERSION_OUTBOX_BATCH_SIZE: int = 50
    # Version-engine request tracing.
    #
    # Development/test can emit every phase so local Save latency is easy to
    # inspect. Staging/production should normally leave this unset/false and
    # rely on slow-request summaries only.
    VERSION_TRACE_ENABLED: bool | None = None
    VERSION_TRACE_SLOW_PHASE_MS: int = 250
    VERSION_TRACE_SLOW_REQUEST_MS: int = 2_000
    # Durable L6 Git view cache root. The cache is rebuildable from Version
    # Engine facts and should live outside the source checkout by default.
    GIT_VIEW_CACHE_DIR: Path = Path("~/.puppyone/git-view-cache")
    # Bound the local git view cache so per-replica disk cannot grow without
    # limit (ISSUE-011). Least-recently-rebuilt views are evicted first. 0
    # disables the respective cap.
    GIT_VIEW_CACHE_MAX_BYTES: int = 5 * 1024 * 1024 * 1024  # 5 GiB
    GIT_VIEW_CACHE_MAX_VIEWS: int = 500

    # Git transport hardening (ISSUE-014). Bound the compressed request body of a
    # push and the wall-clock time of any git subprocess so a hostile client
    # cannot exhaust disk/CPU/memory via an oversized or decompression-bomb pack.
    # 0 disables the respective limit.
    GIT_MAX_RECEIVE_PACK_BYTES: int = 1024 * 1024 * 1024  # 1 GiB compressed push cap
    GIT_MAX_UPLOAD_PACK_BYTES: int = 16 * 1024 * 1024  # 16 MiB fetch-negotiation cap
    GIT_SUBPROCESS_TIMEOUT_SECONDS: int = 300  # per git invocation

    VERSION_OBJECT_GC_ENABLED: bool = False
    VERSION_OBJECT_GC_DRY_RUN: bool = True
    VERSION_OBJECT_GC_INTERVAL_SECONDS: int = 60 * 60
    VERSION_OBJECT_GC_RETENTION_SECONDS: int = 7 * 24 * 60 * 60
    VERSION_OBJECT_GC_QUARANTINE_SECONDS: int = 7 * 24 * 60 * 60
    VERSION_OBJECT_GC_MAX_PROJECTS_PER_RUN: int = 25
    VERSION_OBJECT_GC_MAX_DELETE_PER_PROJECT: int = 1000
    VERSION_OBJECT_GC_REQUIRED_DRY_RUN_DAYS: int = 7
    VERSION_OBJECT_GC_PROJECT_ALLOWLIST: str = ""

    # Project deletion is immediately authoritative in Postgres; Project-owned
    # S3 prefixes are removed by this durable, retryable control-plane worker.
    PROJECT_DELETION_CLEANUP_ENABLED: bool = True
    PROJECT_DELETION_CLEANUP_INTERVAL_SECONDS: int = Field(default=60, ge=10, le=3600)
    PROJECT_DELETION_QUIESCENCE_SECONDS: int = Field(default=3600, ge=1800, le=86_400)
    PROJECT_DELETION_VERIFY_DELAY_SECONDS: int = Field(default=60, ge=10, le=3600)
    PROJECT_DELETION_CLEANUP_LEASE_SECONDS: int = Field(default=3600, ge=300, le=86_400)

    # Project rows are prepared transactionally but stay invisible while the
    # initial root ref remains owned by L5. Resume any operation interrupted
    # between those durable facts through VersionWriteEngine.initialize_project_tree.
    PROJECT_INITIALIZATION_RECONCILE_ENABLED: bool = True
    PROJECT_INITIALIZATION_RECONCILE_INTERVAL_SECONDS: int = Field(
        default=30,
        ge=10,
        le=3600,
    )
    PROJECT_INITIALIZATION_RECONCILE_LEASE_SECONDS: int = Field(
        default=300,
        ge=30,
        le=3600,
    )
    PROJECT_INITIALIZATION_MAX_ATTEMPTS: int = Field(default=8, ge=1, le=100)

    # Post-commit tree-closure tripwire. When on, every product write verifies
    # the freshly-published root resolves its entire subtree closure and fails
    # loud (MissingBlobError) on a dangling tree. Off by default: the walk is
    # O(tree) per write. Builders are proven complete by unit test; enable this
    # for prod paranoia or incident triage. See ProductOperationAdapter.
    VERSION_VERIFY_TREE_CLOSURE_ON_WRITE: bool = False

    # Background primary-loose-object integrity scan (runbook §8①).
    # Disabled + diagnosis-only by default; ops flips _HEAL on after
    # observing the dry-run "ticket" log lines.
    VERSION_INTEGRITY_SCAN_ENABLED: bool = False
    VERSION_INTEGRITY_SCAN_HEAL: bool = False
    VERSION_INTEGRITY_SCAN_INTERVAL_SECONDS: int = 6 * 60 * 60
    VERSION_INTEGRITY_SCAN_MAX_PROJECTS_PER_RUN: int = 25

    # Shadow-snapshot TTL reaper (GAP-10). Shadow snapshots are an
    # ephemeral, best-effort projection of a teammate's un-pushed working
    # tree; once stale they only leak a DB row + an S3 manifest.json. The
    # reaper deletes snapshots not refreshed within the TTL. Enabled by
    # default with a generous 14-day window.
    SHADOW_SNAPSHOT_REAPER_ENABLED: bool = True
    SHADOW_SNAPSHOT_TTL_SECONDS: int = 14 * 24 * 60 * 60
    SHADOW_SNAPSHOT_REAPER_INTERVAL_SECONDS: int = 6 * 60 * 60
    SHADOW_SNAPSHOT_REAPER_MAX_PER_RUN: int = 500

    # Integration sync-run lease. Active sync runs must renew this lease while
    # executing; the reaper fails expired rows so a crashed worker cannot block a
    # connection lane indefinitely.
    SYNC_RUN_LEASE_SECONDS: int = 30 * 60
    SYNC_RUN_HEARTBEAT_INTERVAL_SECONDS: int = 60
    SYNC_RUN_REAPER_ENABLED: bool = True
    SYNC_RUN_REAPER_INTERVAL_SECONDS: int = 5 * 60
    SYNC_RUN_REAPER_MAX_PER_RUN: int = 100

    # One-time import-job reaper. Active import rows (QUEUED/RUNNING) untouched
    # for longer than the stale window mean a dead worker / never-consumed job;
    # the reaper fails them so they don't sit active forever. The reaper floors
    # the stale window above the import worker job_timeout so a live job (which
    # ARQ kills at job_timeout) is never reaped.
    IMPORT_JOB_REAPER_ENABLED: bool = True
    IMPORT_JOB_REAPER_INTERVAL_SECONDS: int = 5 * 60
    IMPORT_JOB_STALE_SECONDS: int = 60 * 60
    IMPORT_JOB_REAPER_MAX_PER_RUN: int = 100

    # Upload-job reaper. Upload finalize runs inline in the API request (not a
    # worker), so a job only stays `running` forever if the API process died
    # mid-finalize. The stale window must exceed the max HTTP request lifetime so
    # a legitimately long finalize is never reaped (1h >> any request timeout).
    UPLOAD_JOB_REAPER_ENABLED: bool = True
    UPLOAD_JOB_REAPER_INTERVAL_SECONDS: int = 5 * 60
    UPLOAD_JOB_STALE_SECONDS: int = 60 * 60
    UPLOAD_JOB_REAPER_MAX_PER_RUN: int = 100

    # Database provider sensitive config encryption (AES-256-GCM)
    # Base64-encoded string of 32-byte key
    DB_CONNECTOR_ENCRYPTION_KEY: str = ""
    DB_CONNECTOR_ENCRYPTION_KID: str = "k1"


settings = Settings()
