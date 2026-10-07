"""Final release schema is committed, then cold native application writes run.

Must run last in the dedicated migration stack: earlier cases intentionally
construct pre-migration fixtures. The stack is destroyed after this test.
"""

import asyncio
import json
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from supabase import ClientOptions, create_client

from src.platform.project.write_lease import ProjectWriteLease, ProjectWriteLeaseRepository
from src.version_engine.adapters.product.operation_adapter import ProductOperationAdapter
from src.version_engine.infrastructure.supabase.repo_manager import VersionRepoManager
from tests.repository_hosting.harness.postgres import Postgres, literal
from tests.repository_hosting.harness.s3_service import owned_s3
from tests.repository_hosting.integration.test_native_inventory_migration import fixture

pytestmark = [
    pytest.mark.hosting_s3,
    pytest.mark.hosting_migration,
    pytest.mark.hosting_final_contract,
]


def test_final_contract_commit_then_native_creation_and_write():
    pg = Postgres()
    a = fixture(pg)
    pg.sql("DELETE FROM public.projects WHERE id=" + literal(a.project))
    contract = (
        Path(__file__).resolve().parents[4]
        / "supabase/data_migrations/20261007_repository_recovery_archive/contract.pending.sql"
    )
    pg.sql(contract.read_text())
    rejected = pg.sql(
        "SELECT public.create_project_idempotent("
        + ",".join(
            map(
                literal,
                (
                    str(uuid.uuid4()),
                    "b" * 64,
                    "unregistered-" + uuid.uuid4().hex,
                    "old creator",
                    "",
                    a.org,
                    a.user,
                    uuid.uuid4().hex,
                    "empty",
                ),
            )
        )
        + ");",
        check=False,
    )
    assert rejected.returncode and "native_repository_required" in rejected.stderr
    project = "contract-native-" + uuid.uuid4().hex
    try:
        with owned_s3() as (s3, api), httpx.Client(timeout=30, trust_env=False) as http:
            sdk = create_client(
                str(api.client.base_url),
                api.headers("service_role")["apikey"],
                ClientOptions(httpx_client=http, auto_refresh_token=False, persist_session=False),
            )
            result = (
                sdk.rpc(
                    "create_native_project_repository",
                    dict(
                        p_project_id=project,
                        p_operation_key=str(uuid.uuid4()),
                        p_payload_hash="a" * 64,
                        p_name="Final native",
                        p_description="",
                        p_org_id=a.org,
                        p_created_by=a.user,
                        p_share_token=uuid.uuid4().hex,
                        p_publication_mode="empty",
                    ),
                )
                .execute()
                .data
            )
            assert result["outcome"] == "native_created"
            prompt = pg.value(
                "SELECT prompt_template FROM public.projects WHERE id=" + literal(project)
            )
            assert "standard Git" in prompt and "mut " not in prompt
            ops = ProductOperationAdapter(
                VersionRepoManager(s3, SimpleNamespace(client=sdk))
            ).for_grant(replace(a.grant, project_id=project))

            async def write():
                async with ProjectWriteLease(
                    project, "contract-test", repository=ProjectWriteLeaseRepository(sdk)
                ):
                    result = await ops.write_file(project, "after.txt", b"after final SQL contract")
                    assert result.commit_id
                assert ops.read_file(project, "after.txt") == b"after final SQL contract"

            asyncio.run(write())
            readiness = (
                sdk.rpc("get_native_project_readiness", {"p_project_id": project}).execute().data
            )
            assert readiness["project_head_commit_id"]
            assert readiness["project_git_push_accepted"] is False
            prefixes = json.loads(
                pg.value(
                    "SELECT public._project_deletion_object_prefixes("
                    + literal(project)
                    + ","
                    + literal([a.user])
                    + "::jsonb)"
                )
            )
            assert prefixes[:3] == [
                f"version/{project}/",
                f"projects/{project}/",
                f"shadow-snapshots/{project}/",
            ]
            assert len(prefixes) == 6
            assert (
                pg.value(
                    "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.proname LIKE 'publish_version_project_update%'"
                )
                == "0"
            )
            assert (
                pg.value(
                    "SELECT count(*) FROM public.version_commits WHERE project_id="
                    + literal(project)
                )
                == "0"
            )
    finally:
        pg.sql("DELETE FROM public.projects WHERE id=" + literal(project))
