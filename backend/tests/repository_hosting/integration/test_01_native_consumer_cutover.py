"""Actual PG/S3 acceptance of migrated Product consumers and private recovery."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from supabase import ClientOptions, create_client

from src.platform.project.write_lease import ProjectWriteLease, ProjectWriteLeaseRepository
from src.version_engine.adapters.product.operation_adapter import ProductOperationAdapter
from src.version_engine.domain.intents import ProjectWriteState
from src.version_engine.infrastructure.supabase.repo_manager import VersionRepoManager
from src.version_engine.read.native_history import NativeHistory
from src.version_engine.write_engine.errors import NativeRevisionConflictError
from tests.repository_hosting.harness.git import Git
from tests.repository_hosting.harness.postgres import Postgres, literal
from tests.repository_hosting.harness.s3_service import owned_s3
from tests.repository_hosting.integration.test_native_inventory_migration import (
    activate,
    fixture,
    operator,
    upload_git,
)

pytestmark = [pytest.mark.hosting_s3, pytest.mark.hosting_migration]


def test_product_producer_retry_history_and_stale_cas(tmp_path):
    pg = Postgres()
    a = fixture(pg)
    git = Git.init(tmp_path / "source")
    git.commit({"before.txt": b"before"})
    try:
        with owned_s3() as (s3, api), httpx.Client(timeout=30, trust_env=False) as http:
            old, _ = upload_git(a, s3, git)
            operator(a, s3).prepare()
            activate(a)
            sdk = create_client(
                str(api.client.base_url),
                api.headers("service_role")["apikey"],
                ClientOptions(httpx_client=http, auto_refresh_token=False, persist_session=False),
            )
            manager = VersionRepoManager(s3, SimpleNamespace(client=sdk))
            ops = ProductOperationAdapter(manager).for_grant(a.grant)
            producer = ops.for_grant(a.grant, operation_key="upload:fixture-stable-task")
            with pytest.raises(PermissionError):
                ProductOperationAdapter(manager).read_file(a.project, "before.txt")
            with ops.open_read(a.project, a.grant) as reader:
                base = reader.get_read_revision(a.project)

            async def write():
                # Uses the same explicit local DB client as the real manager.
                async with ProjectWriteLease(
                    a.project, "acceptance", repository=ProjectWriteLeaseRepository(sdk)
                ):
                    first = await producer.write_file(
                        a.project, "new.txt", b"new", message="upload step"
                    )
                    again = await producer.write_file(
                        a.project, "new.txt", b"new", message="upload step"
                    )
                    assert first.commit_id == again.commit_id
                    with pytest.raises(Exception, match="request_key_reused"):
                        await producer.write_file(
                            a.project, "new.txt", b"changed", message="upload step"
                        )
                    stale = ProjectWriteState(
                        a.project, "", repository_grant=a.grant, repository_revision=base
                    )
                    with pytest.raises(NativeRevisionConflictError):
                        await ops.write_file(
                            a.project, "lost.txt", b"no", project_write_state=stale
                        )
                    assert ops.read_file(a.project, "new.txt") == b"new"
                    with ops.open_read(a.project, a.grant) as reader:
                        history = NativeHistory(reader.snapshot)
                        assert history.head == first.commit_id
                        assert history.content(old, "before.txt") == b"before"
                        assert len(history.linear(10)) == 2
                    restored = await ops.restore_commit(a.project, old)
                    assert restored.commit_id not in {old, first.commit_id}
                    with pytest.raises(FileNotFoundError):
                        ops.read_file(a.project, "new.txt")
                    assert ops.read_file(a.project, "before.txt") == b"before"
                    await ops.bulk_write(a.project, {"two.txt": b"two"}, deleted=["before.txt"])
                    assert ops.read_file(a.project, "two.txt") == b"two"

            asyncio.run(write())
            import uuid

            from src.version_engine.derived.native_events import rebuild_projection

            token = str(uuid.uuid4())
            rows = (
                sdk.rpc("claim_native_projection_events", dict(p_token=token, p_limit=20))
                .execute()
                .data
            )
            assert [row["project_id"] for row in rows] == [a.project]
            assert (
                sdk.rpc(
                    "claim_native_projection_events", dict(p_token=str(uuid.uuid4()), p_limit=20)
                )
                .execute()
                .data
                == []
            )
            with pytest.raises(Exception, match="projection_claim_expired"):
                rebuild_projection(manager, a.project, str(uuid.uuid4()))
            _head, sequence = rebuild_projection(manager, a.project, token)
            assert (
                pg.value(
                    "SELECT full_path FROM public.fs_path_index WHERE project_id="
                    + literal(a.project)
                )
                == "two.txt"
            )
            sdk.rpc(
                "finish_native_projection_events",
                dict(p_project=a.project, p_token=token, p_sequence=sequence),
            ).execute()
            assert (
                sdk.rpc(
                    "claim_native_projection_events", dict(p_token=str(uuid.uuid4()), p_limit=20)
                )
                .execute()
                .data
                == []
            )
            assert (
                pg.value(
                    "SELECT count(*) FROM public.version_commits WHERE project_id="
                    + literal(a.project)
                )
                == "1"
            )
    finally:
        pg.sql("DELETE FROM public.projects WHERE id=" + literal(a.project))


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_seeded_project_initialization_is_native_and_hidden(object_format):
    import uuid
    from dataclasses import replace

    pg = Postgres()
    a = fixture(pg)
    pg.sql("DELETE FROM public.projects WHERE id=" + literal(a.project))
    project, key = "native-seed-" + uuid.uuid4().hex, str(uuid.uuid4())
    try:
        with owned_s3() as (s3, api), httpx.Client(timeout=30, trust_env=False) as http:
            sdk = create_client(
                str(api.client.base_url),
                api.headers("service_role")["apikey"],
                ClientOptions(httpx_client=http, auto_refresh_token=False, persist_session=False),
            )
            outcome = (
                sdk.rpc(
                    "create_native_project_repository",
                    dict(
                        p_operation_key=key,
                        p_payload_hash="a" * 64,
                        p_project_id=project,
                        p_name="Seed acceptance",
                        p_description="",
                        p_org_id=a.org,
                        p_created_by=a.user,
                        p_share_token="seed-" + key,
                        p_publication_mode="deferred",
                        p_object_format=object_format,
                        p_default_branch="trunk",
                    ),
                )
                .execute()
                .data
            )
            assert outcome["outcome"] == "initializing_created"
            manager = VersionRepoManager(s3, SimpleNamespace(client=sdk))
            ordinary = replace(a.grant, project_id=project)
            ops = ProductOperationAdapter(manager)
            with (
                pytest.raises(Exception, match="repository_action_denied"),
                ops.open_read(project, ordinary),
            ):
                pytest.fail("hidden project was publicly readable")

            async def seed():
                async with ProjectWriteLease(
                    project,
                    "project.initialize",
                    initialization_operation_key=key,
                    initialization_actor=a.user,
                    repository=ProjectWriteLeaseRepository(sdk),
                ):
                    bound = ops.for_user(project, a.user)
                    first = await bound.bulk_write(project, {"welcome.md": b"welcome"})
                    assert len(first.commit_id) == (40 if object_format == "sha1" else 64)
                    assert bound.read_file(project, "welcome.md") == b"welcome"
                    completed = (
                        sdk.rpc(
                            "complete_project_initialization",
                            dict(p_project_id=project, p_operation_key=key, p_actor_user_id=a.user),
                        )
                        .execute()
                        .data
                    )
                    assert completed["outcome"] == "completed"
                assert ops.for_grant(ordinary).read_file(project, "welcome.md") == b"welcome"

            asyncio.run(seed())
            assert (
                pg.value(
                    "SELECT version_root_hash FROM public.projects WHERE id=" + literal(project)
                )
                == ""
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
