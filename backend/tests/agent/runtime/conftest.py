"""Owned real PostgreSQL with the complete application migration chain."""

import importlib.util
import json
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[4]
PSQL = "/opt/homebrew/opt/postgresql@17/bin/psql"


def literal(value):
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (dict, list)):
        return "'" + json.dumps(value).replace("'", "''") + "'::jsonb"
    return "'" + str(value).replace("'", "''") + "'"


class Database:
    def __init__(self, url):
        self.url = url

    def sql(self, value):
        result = subprocess.run(
            [PSQL, self.url, "-X", "-qAt", "-v", "ON_ERROR_STOP=1"],
            input=value,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode:
            raise RuntimeError(result.stderr)
        return result.stdout.strip()

    def rpc(self, function, **values):
        args = ",".join("p_" + key + "=>" + literal(value) for key, value in values.items())
        output = self.sql(f"SELECT to_jsonb(public.agent_run_{function}({args}));")
        return json.loads(output) if output else None

    def row(self, query):
        value = self.sql("SELECT to_jsonb(value) FROM (" + query + ") value;")
        return json.loads(value) if value else None


@pytest.fixture(scope="session")
def postgres():
    spec = importlib.util.spec_from_file_location(
        "agent_native_postgres", ROOT / "scripts/testing/native_postgres.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with module.native_postgres() as cluster:
        yield Database(cluster["url"])


@pytest.fixture
def submitted(postgres):
    uid, project, org, agent, request = [str(uuid4()) for _ in range(5)]
    postgres.sql(f"""
      BEGIN;
      INSERT INTO auth.users(id) VALUES ('{uid}');
      INSERT INTO public.organizations(id,name,slug,created_by) VALUES ('{org}','Agent fixture','agent-{org}','{uid}');
      INSERT INTO public.org_members(org_id,user_id,role) VALUES ('{org}','{uid}','owner');
      INSERT INTO public.projects(id,name,org_id,created_by,lifecycle_status) VALUES ('{project}','Agent fixture','{org}','{uid}','ready');
      INSERT INTO public.project_members(project_id,org_id,user_id,role) VALUES ('{project}','{org}','{uid}','admin');
      INSERT INTO public.access_surfaces(id,project_id,org_id,kind,name,created_by) VALUES ('{agent}','{project}','{org}','agent','Agent fixture','{uid}');
      COMMIT;
    """)
    policy = {
        "model": "fixture",
        "surface_updated_at": postgres.row(
            f"SELECT updated_at FROM access_surfaces WHERE id='{agent}'"
        )["updated_at"],
    }
    arguments = dict(
        user=uid,
        project=project,
        agent=agent,
        session=None,
        request=request,
        digest="a" * 64,
        prompt="test durable task",
        policy=policy,
        timeout=600,
    )
    yield arguments, postgres.rpc("submit", **arguments)
    # Stop remaining active rows so claims in later cases cannot select them.
    postgres.sql(f"UPDATE agent_runs SET state='failed' WHERE project_id='{project}';")


@pytest.fixture(scope="session")
def control_api(postgres):
    """Actual PostgREST against the owned PostgreSQL, never a remote service."""
    import time
    from urllib.parse import urlparse

    import httpx
    from postgrest import SyncPostgrestClient

    name = "agent-test-rest-" + str(uuid4())
    port = urlparse(postgres.url).port
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "-p",
            "127.0.0.1::3000",
            "-e",
            f"PGRST_DB_URI=postgresql://postgres@host.docker.internal:{port}/postgres",
            "-e",
            "PGRST_DB_SCHEMAS=public",
            "-e",
            "PGRST_DB_ANON_ROLE=service_role",
            "postgrest/postgrest:v14.13",
        ],
        check=True,
        capture_output=True,
    )
    try:
        address = subprocess.check_output(["docker", "port", name, "3000/tcp"], text=True).strip()
        url = "http://" + address
        with httpx.Client(trust_env=False) as probe:
            for _ in range(100):
                try:
                    if probe.get(url + "/agent_runs?select=id&limit=1").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError(
                    subprocess.check_output(
                        ["docker", "logs", name], stderr=subprocess.STDOUT, text=True
                    )[-2000:]
                )
        client = SyncPostgrestClient(url, http_client=httpx.Client(trust_env=False))

        class Adapter:
            def table(self, name):
                return client.from_(name)

            def rpc(self, *args, **kwargs):
                return client.rpc(*args, **kwargs)

        yield Adapter()
        client.aclose() if hasattr(client, "aclose") else None
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


@pytest.fixture(scope="session")
def object_endpoint():
    import time

    import httpx

    name = "agent-test-s3-" + str(uuid4())
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--entrypoint",
            "/go/bin/minio",
            "-p",
            "127.0.0.1::9000",
            "-e",
            "MINIO_ROOT_USER=agent-fixture",
            "-e",
            "MINIO_ROOT_PASSWORD=agent-fixture-password",
            "puppyone-entrypoint-minio-build:local",
            "server",
            "/data",
            "--address",
            ":9000",
        ],
        check=True,
        capture_output=True,
    )
    try:
        address = subprocess.check_output(["docker", "port", name, "9000/tcp"], text=True).strip()
        url = "http://" + address
        with httpx.Client(trust_env=False) as probe:
            for _ in range(100):
                try:
                    if probe.get(url + "/minio/health/ready").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError("Owned MinIO did not become ready")
        yield url
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


@pytest.fixture
def services(control_api, object_endpoint, monkeypatch):
    from src.config import settings
    from src.infra.s3.config import s3_settings
    from src.infra.s3.service import S3Service
    from src.infra.supabase import dependencies
    from src.infra.supabase.client import SupabaseClient
    from src.version_engine.bootstrap.container import build_version_engine_container

    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    for key, value in {
        "S3_ENDPOINT_URL": object_endpoint,
        "S3_ACCESS_KEY_ID": "agent-fixture",
        "S3_SECRET_ACCESS_KEY": "agent-fixture-password",
        "S3_REGION": "us-east-1",
        "S3_BUCKET_NAME": "agent-" + str(uuid4()),
    }.items():
        monkeypatch.setattr(s3_settings, key, value)
    monkeypatch.setattr(SupabaseClient(), "_client", control_api)
    monkeypatch.setattr(dependencies, "_supabase_client", control_api)
    monkeypatch.setattr(dependencies, "_supabase_repository", None)
    monkeypatch.setattr(settings, "MANAGED_AI_ENABLED", True)
    monkeypatch.setattr(settings, "RUNTIME_METERING_MODE", "disabled")
    storage = S3Service()
    storage.client.create_bucket(Bucket=storage.bucket_name)
    container = build_version_engine_container(s3=storage, supabase=SupabaseClient(), probe=True)
    return control_api, storage, container
