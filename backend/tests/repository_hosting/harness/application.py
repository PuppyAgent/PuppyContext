"""Actual application child process, only inside the owned Linux Docker runner."""

from __future__ import annotations

import base64
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

from .supabase_api import SupabaseAPI


class Application:
    def __init__(
        self, directory, *, profile="native core Git/API; optional external integrations disabled"
    ):
        self.profile = profile
        if not Path("/evidence/container-environment.json").is_file() or sys.platform != "linux":
            raise RuntimeError("application acceptance requires the owned Docker runner")
        self.auth = SupabaseAPI(os.environ)
        self.auth.authenticate()
        bucket = os.environ["S3_BUCKET_NAME"]
        response = self.auth.request(
            "POST", "/storage/v1/bucket", json={"id": bucket, "name": bucket, "public": False}
        )
        if response.status_code not in (200, 201):
            assert self.auth.request("GET", "/storage/v1/bucket/" + bucket).status_code == 200
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.directory = directory
        self.env = dict(os.environ)
        self.env.update(
            DEBUG="false",
            SKIP_AUTH="false",
            APP_ENV="test",
            LOG_DIR=str(directory / "logs"),
            SCHEDULER_ENABLED="false",
            ENABLE_ETL="false",
            MCP_SERVER_URL="",
            SCOPE_SANDBOX_REAPER_ENABLED="false",
            ENTITLEMENTS_MODE="disabled",
            BILLING_ENFORCEMENT="disabled",
            STORAGE_ENFORCEMENT_MODE="disabled",
            RUNTIME_METERING_MODE="disabled",
            SEAT_BILLING_MODE="disabled",
            PUBLIC_URL=self.url,
            FRONTEND_URL=self.url,
            SUPABASE_PUBLIC_URL=os.environ["SUPABASE_URL"],
            INTERNAL_API_SECRET=secrets.token_hex(32),
            MCP_TOKEN_SECRET=secrets.token_hex(32),
            ACCESS_CREDENTIAL_HASH_SECRET=secrets.token_hex(32),
            DB_CONNECTOR_ENCRYPTION_KEY=base64.b64encode(secrets.token_bytes(32)).decode(),
            WORKSPACE_BASE_DIR=str(directory / "workspaces"),
            GIT_VIEW_CACHE_DIR=str(directory / "git-cache"),
            PUPPYONE_GIT_VIEW_CACHE_DIR=str(directory / "git-cache"),
        )
        # Git recipes can idle beyond Uvicorn's keep-alive window. A fresh
        # connection avoids racing its close when the next API fixture starts;
        # do not retry state-changing requests or conceal application failures.
        self.client = httpx.Client(
            base_url=self.url,
            trust_env=False,
            timeout=60,
            limits=httpx.Limits(max_keepalive_connections=0),
        )
        self.process = None
        self.starts = []

    def start(self):
        if self.process is not None and self.process.poll() is None:
            raise RuntimeError("application is already running")
        log = Path("/evidence/application.log")
        with log.open("a") as output:
            log.chmod(0o600)
            self.process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "src.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(self.port),
                    "--no-access-log",
                ],
                env=self.env,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
        self.starts.append(self.process.pid)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise AssertionError("actual API exited; inspect private application.log")
            try:
                response = self.client.get("/ready", timeout=5)
                if response.status_code == 200 and response.json()["status"] == "ready":
                    report = response.json()
                    assert report["environment"]["internal_api_secret_configured"]
                    assert report["errors"] == {"config": [], "dependencies": []}
                    names = (
                        "SUPABASE_URL",
                        "SUPABASE_KEY",
                        "JWT_SECRET",
                        "S3_ENDPOINT_URL",
                        "S3_BUCKET_NAME",
                        "S3_ACCESS_KEY_ID",
                        "S3_SECRET_ACCESS_KEY",
                        "S3_REGION",
                        "AUTH_SECURITY_REDIS_URL",
                        "NOTIFICATIONS_REDIS_URL",
                        "ETL_REDIS_URL",
                        "INTERNAL_API_SECRET",
                        "ACCESS_CREDENTIAL_HASH_SECRET",
                        "MCP_TOKEN_SECRET",
                        "DB_CONNECTOR_ENCRYPTION_KEY",
                        "LOG_DIR",
                        "GIT_VIEW_CACHE_DIR",
                        "WORKSPACE_BASE_DIR",
                    )
                    assert all(self.env.get(name) for name in names)
                    Path("/evidence/application-environment.json").write_text(
                        json.dumps(
                            {
                                "process_ids": self.starts,
                                "configured_names": sorted(names),
                                "readiness": report,
                                "skip_auth": False,
                                "profile": self.profile,
                            },
                            indent=2,
                        )
                        + "\n"
                    )
                    return report
            except httpx.TransportError:
                pass
            time.sleep(0.1)
        raise AssertionError("actual API readiness timed out; inspect private application.log")

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
                raise AssertionError("actual API did not stop cleanly")

    def request(self, method, path, *, token=True, expected=200, **kwargs):
        headers = {"X-PuppyOne-Repository-Contract": "2", **kwargs.pop("headers", {})}
        if token:
            headers["Authorization"] = self.auth.headers("authenticated")["Authorization"]
        response = self.client.request(method, path, headers=headers, **kwargs)
        assert response.status_code == expected, f"{method} {path}: HTTP {response.status_code}"
        return response

    def api(self, method, path, **kwargs):
        body = self.request(method, "/api/v1" + path, **kwargs).json()
        assert body["code"] == 0
        return body["data"]

    def close(self):
        try:
            self.stop()
        finally:
            self.auth.close()
            self.client.close()
