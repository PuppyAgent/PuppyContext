#!/usr/bin/env python3
"""Real local CLI/API/Auth/PostgreSQL/Redis/MinIO/worker acceptance, no IO doubles.

Run with backend/.venv/bin/python. Requires Docker, Node CLI dependencies and
Supabase CLI 2.107.0. Only generated local volumes/accounts/credentials are used.
The URL adapter reads public HTML from httpbingo.org; OAuth/OCR/LLM/cloud rollout
are NOT certified. No .env or hosted credentials are inherited by the app.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import secrets
import socket
import subprocess
import time
import uuid
from pathlib import Path

import boto3
import httpx
from botocore.config import Config
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from test_self_hosted_install import jwt

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "https://httpbingo.org/html"
BAD_SOURCE = "https://httpbingo.org/status/404"


class Stack:
    def __init__(self, directory: Path, port: int, supabase: Path, *, source_root: Path = ROOT):
        self.source_root = source_root.resolve()
        for env_file in (self.source_root/".env", self.source_root/"backend/.env", self.source_root/"backend/mcp_service/.env"):
            if env_file.exists():
                raise ValueError("Use a clean worktree without .env files; hosted secrets must not enter this test")
        docker_host = os.environ.get("DOCKER_HOST") or subprocess.check_output(
            ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"], text=True).strip()
        if not docker_host.startswith(("unix://", "npipe://")):
            raise ValueError("This harness requires a local Docker socket, never a remote daemon")
        if directory.exists() and any(directory.iterdir()):
            raise ValueError("Use a new artifact directory; existing data is never reset")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        self.directory = directory.resolve()
        self.project_name = "p1-entrypoints-" + secrets.token_hex(5)
        self.port = port
        self.python = str(self.source_root / "backend/.venv/bin/python")
        self.processes = {}
        self.http = httpx.Client(trust_env=False, timeout=90)
        self.base = f"http://127.0.0.1:{port + 90}"
        self.auth = f"http://localhost:{port + 80}"
        self.token = None
        self.project = None
        self.receipt = {"source_sha": self.git("rev-parse", "HEAD"),
                        "source_dirty": bool(self.git("status", "--porcelain")),
                        "environment": "isolated-local", "checks": [],
                        "limits": ["public HTML source", "no OAuth/OCR/LLM acceptance",
                                   "fresh install and restart, not old-release/cloud cutover",
                                   "manual Synchronize; scheduler disabled",
                                   "queued-work restart, not an in-flight SIGKILL recovery drill"]}
        for offset in (32, 79, 80, 90, 91, 92, 93):
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", port + offset))
        secret = secrets.token_hex(32)
        self.values = dict(POSTGRES_PASSWORD=secrets.token_hex(16), JWT_SECRET=secret,
            ANON_KEY=jwt("anon", secret), SERVICE_ROLE_KEY=jwt("service_role", secret),
            DATABASE_PORT=str(port+32), SUPABASE_API_PORT=str(port+80),
            BACKEND_PORT=str(port+90), FRONTEND_PORT=str(port), MINIO_CONSOLE_PORT=str(port+91),
            S3_ACCESS_KEY="entrypointlocal", S3_SECRET_KEY=secrets.token_hex(24), S3_BUCKET="entrypoint-local",
            DB_CONNECTOR_ENCRYPTION_KEY=base64.b64encode(secrets.token_bytes(32)).decode("ascii"))
        v = self.values
        self.private("compose.env", "".join(f"{k}={val}\n" for k, val in v.items()))
        self.private("values.json", json.dumps(v))
        self.private("override.yml", f'''services:
  kong:
    ports: !override ["127.0.0.1:{port+80}:8000"]
  minio:
    image: puppyone-entrypoint-minio-build:local
    entrypoint: ["/go/bin/minio"]
    build: !reset null
    ports: !override ["127.0.0.1:{port+91}:9001", "127.0.0.1:{port+92}:9000"]
  redis:
    image: redis:6-alpine
    ports: ["127.0.0.1:{port+79}:6379"]
''')
        self.compose = ["docker", "compose", "--project-name", self.project_name,
            "--env-file", str(self.directory/"compose.env"), "-f", str(self.source_root/"docker/docker-compose.yml"),
            "-f", str(self.directory/"override.yml")]
        self.private("compose-command.json", json.dumps(self.compose))
        self.env = {k: os.environ[k] for k in ("PATH", "LANG", "TMPDIR") if k in os.environ}
        self.env.update(HOME=str(self.directory/"home"), PYTHONPATH=str(self.source_root/"backend"),
            NO_PROXY="localhost,127.0.0.1,::1", APP_ENV="development", DEBUG="false", SKIP_AUTH="false",
            SUPABASE_URL=self.auth, SUPABASE_PUBLIC_URL=self.auth, SUPABASE_KEY=v["SERVICE_ROLE_KEY"],
            SUPABASE_ANON_KEY=v["ANON_KEY"], JWT_SECRET=secret, MCP_TOKEN_SECRET="local-mcp-"+secret,
            DB_CONNECTOR_ENCRYPTION_KEY=v["DB_CONNECTOR_ENCRYPTION_KEY"],
            ACCESS_CREDENTIAL_HASH_SECRET="local-hash-"+secret, INTERNAL_API_SECRET="local-internal-"+secret,
            S3_ENDPOINT_URL=f"http://127.0.0.1:{port+92}", S3_BUCKET_NAME=v["S3_BUCKET"], S3_REGION="us-east-1",
            S3_ACCESS_KEY_ID=v["S3_ACCESS_KEY"], S3_SECRET_ACCESS_KEY=v["S3_SECRET_KEY"],
            ETL_REDIS_URL=f"redis://127.0.0.1:{port+79}/0", AUTH_SECURITY_REDIS_URL=f"redis://127.0.0.1:{port+79}/1",
            PUBLIC_URL=self.base, FRONTEND_URL=f"http://localhost:{port}", SCHEDULER_ENABLED="false",
            ENABLE_ETL="true", OCR_PROVIDER="mineru", ENTITLEMENTS_MODE="disabled", BILLING_ENFORCEMENT="disabled",
            MANAGED_AI_ENABLED="false", MCP_SERVER_URL=f"http://127.0.0.1:{port+93}", MAIN_SERVICE_URL=self.base,
            SCOPE_SANDBOX_REAPER_ENABLED="false", WORKSPACE_BASE_DIR=str(self.directory/"workspaces"),
            GIT_VIEW_CACHE_DIR=str(self.directory/"git-cache"))
        self.supabase = supabase.resolve()
        # The MCP SDK creates its own httpx client; do not send loopback bearer
        # credentials through macOS system proxies either.
        os.environ["NO_PROXY"] = self.env["NO_PROXY"]

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.source_root, text=True).strip()

    def use_source(self, source_root: Path):
        """Switch actual child-process code only after every old process exits."""
        if any(process.poll() is None for process in self.processes.values()):
            raise RuntimeError("Stop every old application process before source cutover")
        source_root = source_root.resolve()
        if any((source_root/path).exists() for path in (".env", "backend/.env", "backend/mcp_service/.env")):
            raise ValueError("Cutover source must not supply .env inputs")
        self.source_root = source_root
        self.python = str(source_root/"backend/.venv/bin/python")
        self.env["PYTHONPATH"] = str(source_root/"backend")

    def private(self, name, text):
        path = self.directory/name
        path.write_text(text)
        path.chmod(0o600)

    def run(self, name, *args, env=None, expected=0, timeout=600):
        result = subprocess.run(args, cwd=self.directory, env=env or self.env,
                                capture_output=True, text=True, timeout=timeout)
        self.private(name + ".log", result.stdout + result.stderr)
        if result.returncode != expected:
            raise RuntimeError(f"{name} exited {result.returncode}; inspect its private log")
        return result.stdout

    def start(self, name):
        if name in self.processes and self.processes[name].poll() is None:
            raise RuntimeError(f"Already running: {name}")
        if name in {"api", "mcp"}:
            module = "src.main:app" if name == "api" else "mcp_service.server:app"
            args = ["-m", "uvicorn", module, "--host", "127.0.0.1", "--port",
                    str(self.port + (90 if name == "api" else 93)), "--no-access-log"]
        else:
            domain = "imports" if name == "import" else name
            args = ["-m", "arq", f"src.platform.{domain}.worker.WorkerSettings"]
        with (self.directory/f"{name}.log").open("a") as log:
            self.processes[name] = subprocess.Popen([self.python, *args], env=self.env,
                cwd=self.directory, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        self.private("processes.json", json.dumps({k: p.pid for k, p in self.processes.items()}))

    def stop(self, name):
        process = self.processes[name]
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=30)
        return {"role": name, "pid": process.pid, "exit_code": process.returncode}

    def check(self, name, **evidence):
        self.receipt["checks"].append({"name": name, "status": "PASS", **evidence})
        self.private("receipt.json", json.dumps(self.receipt, indent=2))
        print("PASS", name, flush=True)

    def api(self, method, path, *, token=None, expected=200, **kwargs):
        headers = {"X-PuppyOne-Repository-Contract": "2", **kwargs.pop("headers", {})}
        if token or self.token:
            headers["Authorization"] = "Bearer " + (token or self.token)
        r = self.http.request(method, self.base+"/api/v1"+path, headers=headers, **kwargs)
        if r.status_code != expected:
            raise RuntimeError(f"{method} {path}: {r.status_code}: {r.text[:800]}")
        body = r.json()
        if "code" in body:
            assert body["code"] == 0, body
            return body["data"]
        return body

    def cli(self, *args):
        result = self.run("last-cli", "node", str(self.source_root/"cli/bin/puppyone.js"), "--json",
            "--api-url", self.base, "--api-key", self.token, "--project", self.project, *args, timeout=90)
        body = json.loads(result)
        assert body["success"], body
        return body

    def ready(self):
        for _ in range(60):
            try:
                r = self.http.get(self.base+"/ready", timeout=5)
                if r.status_code == 200:
                    return r.json()
            except httpx.TransportError:
                pass
            if self.processes["api"].poll() is not None:
                raise RuntimeError("API exited; inspect api.log")
            time.sleep(1)
        raise RuntimeError("API never became ready; inspect api.log")

    def wait(self, path, wanted):
        for _ in range(120):
            row = self.api("GET", path)
            if row["status"] in {"completed", "success", "skipped", "failed", "cancelled"}:
                assert row["status"] in wanted, row
                return row
            time.sleep(.5)
        raise RuntimeError(f"Durable task did not finish: {path}")

    def setup(self):
        assert self.run("supabase-version", str(self.supabase), "--version").strip() == "2.107.0"
        # Use the committed immutable MinIO build target. The Go base already
        # includes CA/curl, avoiding another apt download for this local harness.
        self.run("minio-build", "docker", "build", "--target", "build", "-t",
            "puppyone-entrypoint-minio-build:local", "-f", str(self.source_root/"docker/Dockerfile.minio"),
            str(self.source_root/"docker"), timeout=1800, env=dict(os.environ))
        self.run("infra-up", *self.compose, "up", "-d", "--wait", "--wait-timeout", "150",
                 "--no-build", "db", "auth", "rest", "kong", "redis", "minio", env=dict(os.environ))
        migration_env = {**self.env, "PATH": str(self.supabase.parent)+":"+self.env["PATH"],
            "POSTGRES_PASSWORD": self.values["POSTGRES_PASSWORD"], "PGHOST": "127.0.0.1", "PGPORT": str(self.port+32)}
        for name in ("migrate", "migrate-replay"):
            self.run(name, self.python, str(self.source_root/"scripts/self_hosted_migrate.py"), env=migration_env)
        self.run("storage-bootstrap", self.python, "-m", "src.infra.self_hosted_bootstrap")
        for name in ("mcp", "api", "import", "synchronize", "upload"):
            self.start(name)
        self.check("fresh schema + replay + real dependency readiness", readiness=self.ready())

    def signup(self, label):
        r = self.http.post(self.auth+"/auth/v1/signup", headers={"apikey": self.values["ANON_KEY"]},
            json={"email": label+"@example.test", "password": secrets.token_urlsafe(24)})
        r.raise_for_status()
        assert r.json().get("access_token")
        return r.json()["access_token"]

    def upload(self, name, content):
        upload = self.api("POST", "/upload/init", json={"project_id": self.project,
            "files": [{"filename": name, "size": len(content), "content_type": "text/plain"}]})["files"][0]
        part = self.api("PUT", "/upload/part", params={"task_id": upload["task_id"], "part_number": 1}, content=content)
        return upload, part

    def scan(self, name, expected):
        raw = self.run(name, self.python, "-m", "src.infra.queue_cutover",
                      "--queues", "imports", "synchronize", "etl", expected=expected)
        return json.loads(raw)

    async def mcp_read(self, access):
        assert access["mcp_server_url"].startswith(self.base+"/")
        async with (
            streamablehttp_client(access["mcp_server_url"],
                headers={"Authorization": "Bearer "+access["mcp_api_key"]}) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            content = await session.call_tool("fs_cat", {"path": "data.md"})
            assert not content.isError
            escape = await session.call_tool("fs_cat", {"path": "../local-upload.txt"})
            assert escape.isError

    def exercise(self):
        self.token = self.signup("entrypoint-owner")
        self.private("session.json", json.dumps({"access_token": self.token}))
        org = self.api("POST", "/organizations/", expected=201, json={"name": "Local entrypoints"})
        project = self.api("POST", "/projects/", expected=201,
            headers={"Idempotency-Key": str(uuid.uuid4())}, json={"name": "Runtime acceptance", "org_id": org["id"]})
        self.project = project["id"]
        self.receipt["project_id"] = self.project
        self.check("real GoTrue JWT + Organization/Project creation")
        imported = self.cli("import", "create", SOURCE, "--folder", "snapshot", "--idempotency-key", "snapshot")
        job = self.wait("/imports/"+imported["job"]["id"], {"completed"})
        assert job["result_commit_id"]
        text = self.api("GET", f"/content/{self.project}/cat", params={"path": job["result_path"]})["content_text"]
        assert "Availing himself" in text
        assert self.api("GET", "/synchronize/bindings", params={"project_id": self.project}) == []
        self.check("CLI Import -> independent worker -> durable job + actual Git content; no binding", job=job)
        github = self.cli("import", "create", "https://github.com/octocat/Hello-World",
            "--provider", "github", "--folder", "github-snapshot",
            "--config", '{"max_files":10,"max_total_bytes":1048576}')
        github_job = self.wait("/imports/"+github["job"]["id"], {"completed"})
        github_text = self.http.get(self.base+f"/api/v1/content/{self.project}/raw",
            params={"path": "github-snapshot/README"}, headers={"Authorization": "Bearer "+self.token})
        assert github_text.status_code == 200 and b"Hello World" in github_text.content
        assert self.api("GET", "/synchronize/bindings", params={"project_id": self.project}) == []
        self.check("real public GitHub snapshot remains Import, not continuous Synchronize", job=github_job)
        binding = self.cli("synchronize", "add", "url", SOURCE, "--folder", "synchronized")
        binding_id = binding["binding"]["id"]
        run = self.wait("/synchronize/runs/"+binding["execution_result"]["synchronize_run_id"], {"success"})
        self.check("CLI Synchronize -> independent worker -> binding/run", run=run)
        content = b"Local multipart bytes survive process and infrastructure restart.\n"
        u, part = self.upload("local-upload.txt", content)
        completed = self.api("POST", "/upload/complete", json={k: u[k] for k in ("task_id", "s3_key", "upload_id")} | {"parts": [part]})
        assert completed["status"] == "completed"
        self.check("public Upload multipart completion", task=completed)
        cancelled_upload, _ = self.upload("cancelled-upload.txt", content)
        abort = self.api("POST", "/upload/abort", json={k: cancelled_upload[k] for k in ("task_id", "s3_key", "upload_id")})
        assert abort["cancelled"]
        self.wait("/ingest/tasks/"+cancelled_upload["task_id"]+"?source_type=file", {"cancelled"})
        self.check("public Upload abort preserves durable cancellation")
        scoped = self.cli("access", "add", "mcp", "Scoped MCP", "--scope", "snapshot")["access"]
        self.cli("access", "add", "mcp", "Root MCP")
        self.cli("access", "add", "sandbox", "Root sandbox")
        self.cli("access", "add", "sandbox", "Scoped sandbox", "--scope", "synchronized")
        self.check("actual MCP/Sandbox repositories: root and nonroot target creation")
        dashboard = self.cli("status")["dashboard"]
        assert {r["resource_kind"] for r in dashboard["resources"]} == {"access", "synchronize"}
        assert len(dashboard["resources"]) == 5
        targets = [r["target"]["kind"] for r in dashboard["resources"] if r["resource_kind"] == "access"]
        assert targets.count("project_root") == targets.count("scope") == 2
        self.cli("access", "pause", scoped["id"])
        assert self.api("GET", "/access/surfaces/"+scoped["id"])["status"] == "paused"
        self.cli("access", "resume", scoped["id"])
        wrong_id = self.http.post(self.base+"/api/v1/synchronize/bindings/"+scoped["id"]+"/refresh",
            headers={"Authorization": "Bearer "+self.token})
        assert wrong_id.status_code == 404
        stranger = self.signup("entrypoint-stranger")
        denied = self.http.get(self.base+f"/api/v1/projects/{self.project}/dashboard/resources",
                               headers={"Authorization": "Bearer "+stranger})
        assert denied.status_code in {403, 404}
        asyncio.run(self.mcp_read(scoped))
        self.check("CLI Dashboard + real foreign-user denial + MCP credential/scoped IO")
        self.stop("import")
        args = ("import", "create", SOURCE, "--folder", "restart", "--idempotency-key", "restart")
        pending = self.cli(*args)["job"]
        assert self.cli(*args)["job"]["id"] == pending["id"]
        cancelled = self.cli("import", "create", SOURCE, "--folder", "cancelled")["job"]
        assert self.cli("import", "cancel", cancelled["id"])["job"]["status"] == "cancelled"
        time.sleep(2)
        assert self.api("GET", "/imports/"+pending["id"])["status"] == "queued"
        self.check("queue ownership, Import idempotency and cancellation", scan=self.scan("drain-blocked", 2))
        stops = [self.stop(name) for name in ("api", "mcp", "synchronize", "upload")]
        try:
            self.http.get(self.base+"/live", timeout=1)
        except httpx.ConnectError:
            pass
        else:
            raise AssertionError("API still admits requests after stop")
        self.run("infra-restart", "docker", "restart", *[self.project_name+f"-{name}-1" for name in ("redis", "minio", "db")])
        time.sleep(5)
        for name in ("mcp", "api", "synchronize", "upload"):
            self.start(name)
        self.ready()
        assert self.api("GET", "/imports/"+pending["id"])["status"] == "queued"
        self.start("import")
        restored = self.wait("/imports/"+pending["id"], {"completed"})
        assert self.api("GET", "/imports/"+cancelled["id"])["status"] == "cancelled"
        raw = self.http.get(self.base+f"/api/v1/content/{self.project}/raw", params={"path": "local-upload.txt"},
                            headers={"Authorization": "Bearer "+self.token})
        assert raw.status_code == 200 and raw.content == content
        self.check("producer stop + API/workers/PostgreSQL/Redis/MinIO restart + durable recovery", stops=stops, job=restored)
        self.api("PATCH", "/synchronize/bindings/"+binding_id, json={"config": {"source": {"resource_url": BAD_SOURCE}, "options": {}}})
        failed_run = self.cli("synchronize", "refresh", binding_id)["result"]["results"][0]
        failed = self.wait("/synchronize/runs/"+failed_run["synchronize_run_id"], {"failed"})
        assert "404" in failed["error"]
        self.api("PATCH", "/synchronize/bindings/"+binding_id, json={"config": {"source": {"resource_url": SOURCE}, "options": {}}})
        retried = self.cli("synchronize", "refresh", binding_id)["result"]["results"][0]
        recovered = self.wait("/synchronize/runs/"+retried["synchronize_run_id"], {"success", "skipped"})
        assert recovered["id"] != failed["id"]
        self.check("durable provider failure + explicit Synchronize retry", failed=failed, recovered=recovered)
        # /complete currently finalizes inline. Exercise the registered queue
        # path explicitly, with actual multipart storage and the owned producer.
        u, part = self.upload("worker-upload.txt", content)
        s3 = boto3.client("s3", endpoint_url=self.env["S3_ENDPOINT_URL"], region_name="us-east-1",
            aws_access_key_id=self.values["S3_ACCESS_KEY"], aws_secret_access_key=self.values["S3_SECRET_KEY"], config=Config(proxies={}))
        s3.complete_multipart_upload(Bucket=self.values["S3_BUCKET"], Key=u["s3_key"], UploadId=u["upload_id"],
            MultipartUpload={"Parts": [{"PartNumber": 1, "ETag": part["etag"]}]})
        self.run("upload-enqueue", self.python, "-c",
            "import asyncio\nfrom src.platform.upload.arq_client import UploadArqClient\n"
            f"asyncio.run(UploadArqClient().enqueue_finalize_upload({u['task_id']!r}))")
        upload = self.wait("/ingest/tasks/"+u["task_id"]+"?source_type=file", {"completed"})
        raw = self.http.get(self.base+f"/api/v1/content/{self.project}/raw", params={"path": "worker-upload.txt"},
                            headers={"Authorization": "Bearer "+self.token})
        assert raw.status_code == 200 and raw.content == content
        self.check("real Upload queue consumer + persisted Git bytes", task=upload)
        time.sleep(1)
        self.check("final drain and readiness", scan=self.scan("drain-empty", 0), readiness=self.ready())

    def cleanup(self):
        for name in self.processes:
            self.stop(name)
        self.run("infra-stop", *self.compose, "down", env=dict(os.environ))
        # Deliberately retain project-owned volumes and private evidence. Never
        # prune Docker or delete any pre-existing/shared volume.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--supabase-bin", type=Path, required=True)
    parser.add_argument("--base-port", type=int, default=29600)
    parser.add_argument("--keep", action="store_true", help="Leave this isolated local stack running")
    args = parser.parse_args()
    stack = Stack(args.artifacts, args.base_port, args.supabase_bin)
    try:
        stack.setup()
        stack.exercise()
        print("Local runtime acceptance passed. Evidence:", stack.directory/"receipt.json")
    finally:
        if not args.keep:
            stack.cleanup()


if __name__ == "__main__":
    main()
