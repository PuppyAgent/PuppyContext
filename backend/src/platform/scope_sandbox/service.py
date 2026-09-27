"""Application service tying the scope-sandbox library to the HTTP API (#9).

Owns a process-level ``ScopeSandboxManager`` per provider (the manager holds the
warm/cold session lifecycle; the durable store is shared), and orchestrates the
end-user "connect" flow:

  resolve scope → acquire/reuse a sandbox for the scope (clone the scope's git
  content on cold create) → ensure SSH is reachable → grant the user a
  short-lived, revocable key (their pasted public key) → hand back the
  connection info + a ready-to-paste VSCode Remote-SSH config block.

Provider-agnostic: E2B (no native TCP → sshd + websocat tunnel provisioned at
runtime, connection carries a ProxyCommand) and Fly (native TCP, sshd baked into
the image) both flow through the same code; only ``ConnectionInfo`` differs.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from src.config import settings
from src.platform.project.write_lease import ProjectWriteLease, ProjectWriteLeaseFactory
from src.platform.scope_sandbox import scope_provision, ssh_credentials, ssh_e2b
from src.platform.scope_sandbox.factory import provider_from_settings, store_from_settings
from src.platform.scope_sandbox.manager import AcquireResult, ScopeSandboxManager
from src.platform.scope_sandbox.provider import SandboxProvider, SandboxSpec
from src.platform.scope_sandbox.registry import SandboxSessionStore
from src.utils.logger import log_info, log_warning
from src.version_engine.entrypoints.git.locator import canonical_git_url

# The URL is non-secret. The token stays in server-only SandboxSpec metadata
# until the provider writes it through its sensitive file channel.
GIT_URL_ENV = "PUPPY_SCOPE_GIT_URL"
GIT_TOKEN_ENV = "PUPPY_SCOPE_GIT_TOKEN"

# Short-lived by default — a connect grants ~8h, re-connect renews. Offboarding
# (revoke) or the TTL elapsing both cut access.
DEFAULT_TTL_S = 8 * 3600


@dataclass
class ConnectInfo:
    """What the frontend needs to open VSCode Remote-SSH."""

    provider: str
    state: str
    via: str                 # reused | resumed | created
    host: str
    port: int
    username: str
    proxy_command: str | None
    needs_websocat: bool     # True for E2B (ProxyCommand uses local websocat)
    workspace_path: str      # Git working tree to open in VSCode/Cursor
    ssh_config_block: str
    expires_at: float
    connected_users: int


def _scope_service():
    from src.repo.scope_service import ScopeService
    return ScopeService()


class ScopeSandboxService:
    """One per process. Injectable bits keep it unit-testable without live SDKs."""

    def __init__(
        self,
        *,
        store: SandboxSessionStore | None = None,
        scope_lookup=None,
        manager_factory=None,
        sidecar_starter=None,
        scope_credential_issuer=None,
        git_credential_issuer=None,
        write_lease_factory: ProjectWriteLeaseFactory = ProjectWriteLease,
    ) -> None:
        # Shared durable store across providers (one row per scope).
        self._store = store or store_from_settings(settings)
        # scope_id -> repository Scope object with Project/path geometry
        self._scope_lookup = scope_lookup or (lambda sid: _scope_service().get(sid))
        self._manager_factory = manager_factory or self._build_manager
        # injectable so unit tests skip the (DB-touching) sync-sidecar start
        self._sidecar_starter = sidecar_starter or self._maybe_start_sidecar
        self._scope_credential_issuer = (
            scope_credential_issuer or self._issue_scope_credential
        )
        self._git_credential_issuer = (
            git_credential_issuer or self._issue_git_credential
        )
        self._managers: dict[str, ScopeSandboxManager] = {}
        self._write_lease_factory = write_lease_factory

    # ── manager wiring ────────────────────────────────────────────────

    def _build_manager(self, provider_name: str) -> ScopeSandboxManager:
        provider = provider_from_settings(settings, name=provider_name)
        return ScopeSandboxManager(
            provider,
            self._store,
            bootstrap=self._make_bootstrap(),
            # offboarding via revoke_user also pulls the user's SSH key
            revoke_hook=ssh_credentials.revoke_ssh_access,
        )

    def _manager(self, provider_name: str | None = None) -> ScopeSandboxManager:
        name = provider_name or settings.SCOPE_SANDBOX_PROVIDER
        mgr = self._managers.get(name)
        if mgr is None:
            mgr = self._manager_factory(name)
            self._managers[name] = mgr
        return mgr

    def _make_bootstrap(self):
        """Cold-create hook: stand up SSH (E2B only) + clone the scope content."""
        async def bootstrap(provider: SandboxProvider, sandbox_id: str, spec: SandboxSpec) -> None:
            # E2B has no native TCP ingress → provision sshd + websocat tunnel at
            # runtime (publickey-only). Fly bakes sshd into the image → skip.
            # With the custom E2B template (roadmap #6) sshd+websocat are baked, so
            # use the FAST path (seed + start only).
            if not provider.capabilities().supports_tcp_ingress:
                baked = bool(getattr(settings, "SCOPE_SANDBOX_E2B_TEMPLATE", ""))
                await ssh_e2b.provision_e2b_ssh(provider, sandbox_id, baked=baked)  # no seed key
            git_url = spec.env.get(GIT_URL_ENV, "")
            if git_url:
                git_credential = str(spec.metadata.get(GIT_TOKEN_ENV) or "")
                if not git_credential:
                    raise RuntimeError("Sandbox Git credential is missing")
                await scope_provision.write_scope_git_credential(
                    provider,
                    sandbox_id,
                    git_credential,
                )
                await scope_provision.provision_scope_workspace(
                    provider, sandbox_id, git_url=git_url,
                )
        return bootstrap

    # ── git url ───────────────────────────────────────────────────────

    @staticmethod
    def _git_url(
        public_base: str,
        project_id: str,
        scope_id: str,
    ) -> str:
        return canonical_git_url(public_base, project_id, scope_id)

    @staticmethod
    def _issue_scope_credential(
        scope_id: str,
        user_id: str,
        expires_at: datetime,
    ) -> str | None:
        from src.platform.access.surface_repository import AccessSurfaceRepository

        return AccessSurfaceRepository().issue_scope_session_credential(
            scope_id=scope_id,
            created_by=user_id,
            expires_at=expires_at,
        )

    @staticmethod
    def _issue_git_credential(
        scope_id: str,
        user_id: str,
        expires_at: datetime,
    ) -> str | None:
        from src.platform.access.surface_repository import AccessSurfaceRepository

        return AccessSurfaceRepository().issue_git_session_credential(
            scope_id=scope_id,
            created_by=user_id,
            expires_at=expires_at,
        )

    @staticmethod
    def _workspace_path(username: str, workdir: str) -> str:
        def clean(value: str, fallback: str) -> str:
            return value.replace("'", "").replace('"', "").replace("\n", "").strip().strip("/") or fallback

        user = clean(username, "user")
        wd = clean(workdir, "scope")
        return f"/home/{user}/{wd}"

    # ── connect / status / revoke ─────────────────────────────────────

    async def connect(
        self,
        *,
        project_id: str,
        scope_id: str,
        user_id: str,
        user_email: str,
        user_name: str,
        public_key: str,
        public_base: str,
        provider_name: str | None = None,
        ttl_s: int = DEFAULT_TTL_S,
        now: float | None = None,
    ) -> ConnectInfo:
        # Sandbox creation/resume/extension is an external Project write. Keep
        # it inside the same deletion admission barrier as Git, S3 and search.
        async with self._write_lease_factory(project_id, "sandbox.connect"):
            return await self._connect_admitted(
                project_id=project_id,
                scope_id=scope_id,
                user_id=user_id,
                user_email=user_email,
                user_name=user_name,
                public_key=public_key,
                public_base=public_base,
                provider_name=provider_name,
                ttl_s=ttl_s,
                now=now,
            )

    async def _connect_admitted(
        self,
        *,
        project_id: str,
        scope_id: str,
        user_id: str,
        user_email: str,
        user_name: str,
        public_key: str,
        public_base: str,
        provider_name: str | None = None,
        ttl_s: int = DEFAULT_TTL_S,
        now: float | None = None,
    ) -> ConnectInfo:
        scope = self._scope_lookup(scope_id)
        if scope is None or scope.project_id != project_id:
            raise LookupError("scope not found in project")

        now = time.time() if now is None else now
        expires_at = now + ttl_s
        credential_expires_at = datetime.fromtimestamp(
            expires_at, tz=timezone.utc
        )
        access_key = self._scope_credential_issuer(
            scope_id,
            user_id,
            credential_expires_at,
        )
        if not access_key:
            raise RuntimeError("Failed to issue sandbox scope credential")
        git_credential = self._git_credential_issuer(
            scope_id,
            user_id,
            credential_expires_at,
        )
        if not git_credential:
            raise RuntimeError("Failed to issue sandbox Git credential")
        git_url = self._git_url(
            public_base,
            project_id,
            scope_id,
        )
        mgr = self._manager(provider_name)

        spec = SandboxSpec(
            scope_id=scope_id,
            project_id=project_id,
            env={GIT_URL_ENV: git_url},
            metadata={GIT_TOKEN_ENV: git_credential},
        )
        result: AcquireResult = await mgr.acquire(spec, user_id, now=now)
        session = result.session
        provider = mgr.provider
        sid = session.sandbox_id

        conn = session.connection
        host = conn.host if conn else sid
        port = conn.port if conn else 22
        username = conn.username if conn else "user"
        proxy = conn.proxy_command if conn else None

        # A warm/resumed sandbox keeps its disk, so renew the helper's secret
        # file on every connect. The raw value travels via E2B's filesystem API
        # or Fly Machines Exec stdin, never via the shell command or locator.
        await scope_provision.write_scope_git_credential(
            provider,
            sid,
            git_credential,
        )

        # per-user working tree + git identity (#7): push attributes to the person
        workdir = await ssh_credentials.provision_user_workspace(
            provider, sid, user_id,
            git_url=git_url, user_email=user_email, user_name=user_name,
        )
        workspace_path = self._workspace_path(username, workdir)
        # short-lived, revocable SSH grant (#5)
        await ssh_credentials.grant_ssh_access(
            provider, sid, user_id, public_key, expires_at=expires_at,
        )

        # Start the in-sandbox sync sidecar (P0 closed loop): watch the user's
        # working tree → checkpoint/publish + consume upstream events. Best-effort,
        # gated on the scope's auto_sync setting. (Single sidecar per box for now;
        # multi-user-per-scope sidecars are a follow-up.)
        await self._sidecar_starter(
            provider, sid, project_id=project_id, scope_id=scope_id,
            user_id=user_id, username=username, access_key=access_key,
            public_base=public_base, repo_dir=workspace_path,
        )

        log_info(
            f"[scope-sandbox] connect scope={scope_id} user={user_id} "
            f"via={result.via.value} provider={session.provider}"
        )
        return ConnectInfo(
            provider=session.provider,
            state=session.state.value,
            via=result.via.value,
            host=host,
            port=port,
            username=username,
            proxy_command=proxy,
            needs_websocat=proxy is not None and "websocat" in proxy,
            workspace_path=workspace_path,
            ssh_config_block=_ssh_config_block(scope_id, host, port, username, proxy),
            expires_at=expires_at,
            connected_users=len(session.connected_users),
        )

    async def _maybe_start_sidecar(
        self, provider, sandbox_id: str, *, project_id: str, scope_id: str,
        user_id: str, username: str, access_key: str, public_base: str, repo_dir: str,
    ) -> None:
        """Install + start the scope-sync sidecar for this connection (best-effort,
        gated on the scope's auto_sync setting). Never breaks connect."""
        try:
            from src.platform.scope_sync import sidecar_provision as sp
            from src.platform.scope_sync.service import get_scope_sync_service

            resolved = get_scope_sync_service().resolve_policy(
                project_id=project_id, scope_id=scope_id)
            if not resolved.get("auto_sync", True):
                return
            env = sp.build_sidecar_env(
                resolved["policy"], repo_dir=repo_dir,
                events_url=f"{public_base.rstrip('/')}/api/v1/scope-sync/ap/events",
                project_id=project_id, scope_id=scope_id, token=access_key,
            )
            await sp.install_and_start(provider, sandbox_id, repo_dir=repo_dir, env=env)
            log_info(f"[scope-sandbox] sync sidecar started scope={scope_id} repo={repo_dir}")
        except Exception as exc:  # noqa: BLE001 - sidecar is best-effort
            log_warning(f"[scope-sandbox] sidecar start skipped scope={scope_id}: {exc}")

    def status(self, *, project_id: str, scope_id: str, user_id: str) -> dict:
        session = self._store.get(scope_id)
        if session is None or session.project_id != project_id:
            return {"state": "none", "connected": False, "connected_users": 0}
        conn = session.connection
        host = conn.host if conn else session.sandbox_id
        port = conn.port if conn else 22
        username = conn.username if conn else "user"
        proxy = conn.proxy_command if conn else None
        return {
            "state": session.state.value,
            "provider": session.provider,
            "connected": user_id in session.connected_users,
            "connected_users": len(session.connected_users),
            "sandbox_id": session.sandbox_id,
            "host": host,
            "port": port,
            "username": username,
            "proxy_command": proxy,
            "needs_websocat": proxy is not None and "websocat" in proxy,
            "workspace_path": self._workspace_path(username, user_id),
            "ssh_config_block": _ssh_config_block(scope_id, host, port, username, proxy),
        }

    async def revoke(self, *, project_id: str, scope_id: str, user_id: str) -> int:
        """Revoke the user's SSH access + drop them from the session. Returns the
        number of users still connected."""
        session = self._store.get(scope_id)
        if session is None or session.project_id != project_id:
            return 0
        mgr = self._manager(session.provider)  # use the provider that owns this box
        return await mgr.revoke_user(scope_id, user_id)

    def available_providers(self) -> dict:
        """Which providers this deployment can actually use (drives the frontend
        selector) + the configured default. A provider is 'configured' iff its
        credentials are present in settings."""
        # E2B_API_KEY is read from the environment by the SDK (not a Settings
        # field); Fly creds ARE Settings fields.
        e2b = bool(os.environ.get("E2B_API_KEY") or getattr(settings, "E2B_API_KEY", None))
        fly = bool(getattr(settings, "SCOPE_SANDBOX_FLY_APP", "")
                   and getattr(settings, "SCOPE_SANDBOX_FLY_TOKEN", ""))
        return {
            "default": getattr(settings, "SCOPE_SANDBOX_PROVIDER", "e2b"),
            "providers": [
                {"id": "e2b", "label": "E2B", "configured": e2b},
                {"id": "fly", "label": "Fly", "configured": fly},
            ],
        }

    async def reap(self, *, now: float | None = None):
        """Sweep idle sessions across ALL providers present in the store (each
        provider's manager reaps only its own sandboxes). Matches the reaper's
        ``_Reapable`` protocol so it can be driven by ``reaper.start_reaper``."""
        from src.platform.scope_sandbox.manager import ReapSummary

        total = ReapSummary()
        providers = {s.provider for s in self._store.list_all()}
        for name in providers:
            try:
                mgr = self._manager(name)
            except Exception:  # noqa: BLE001 - a misconfigured provider must not block others
                continue
            summary = await mgr.reap(now=now, only_provider=name)
            total.kept += summary.kept
            total.stopped += summary.stopped
            total.destroyed += summary.destroyed
            total.errors += summary.errors
        return total


def _ssh_config_block(
    scope_id: str, host: str, port: int, username: str, proxy_command: str | None,
) -> str:
    """A ready-to-paste ``~/.ssh/config`` block (provider-agnostic)."""
    alias = f"puppy-{scope_id[:8]}"
    lines = [
        f"Host {alias}",
        f"    HostName {host}",
        f"    User {username}",
        f"    Port {port}",
        "    IdentityFile ~/.ssh/id_ed25519   # path to YOUR private key",
    ]
    if proxy_command:
        lines.append(f"    ProxyCommand {proxy_command}")
    lines += [
        "    StrictHostKeyChecking no",
        "    UserKnownHostsFile /dev/null",
    ]
    return "\n".join(lines) + "\n"


# Process-level singleton (built lazily so importing the module is cheap).
_SERVICE: ScopeSandboxService | None = None


def get_scope_sandbox_service() -> ScopeSandboxService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = ScopeSandboxService()
    return _SERVICE
