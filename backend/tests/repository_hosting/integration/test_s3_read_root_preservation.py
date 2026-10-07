"""Migrated native refs survive missing physical bytes without root repair."""

from types import SimpleNamespace

import httpx
import pytest
from supabase import ClientOptions, create_client

from src.version_engine.adapters.product.operation_adapter import ProductOperationAdapter
from src.version_engine.domain.errors import NativeObjectNotFoundError
from src.version_engine.infrastructure.supabase.repo_manager import VersionRepoManager
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


@pytest.mark.parametrize("damage", ["tree", "blob"])
def test_s3_listing_damage_preserves_acknowledged_root_and_recovers_without_metadata_repair(
    tmp_path, damage
):
    pg, git = Postgres(), Git.init(tmp_path / "client")
    a = fixture(pg)
    git.commit({"docs/good.md": b"healthy", "broken/lost.md": b"acknowledged"})
    missing = git.text("rev-parse", "HEAD:broken" if damage == "tree" else "HEAD:broken/lost.md")
    try:
        with owned_s3() as (s3, api), httpx.Client(timeout=30, trust_env=False) as http:
            upload_git(a, s3, git)
            operator(a, s3).prepare()
            activate(a)
            sdk = create_client(
                str(api.client.base_url),
                api.headers("service_role")["apikey"],
                ClientOptions(httpx_client=http, auto_refresh_token=False, persist_session=False),
            )
            ops = ProductOperationAdapter(
                VersionRepoManager(s3, SimpleNamespace(client=sdk))
            ).for_grant(a.grant)
            before = pg.value(
                "SELECT jsonb_agg(to_jsonb(r) ORDER BY name) FROM public.version_repository_refs r WHERE project_id="
                + literal(a.project)
            )
            key = f"version/{a.project}/objects/{missing[:2]}/{missing[2:]}"
            raw = s3.client.get_object(Bucket=s3.bucket_name, Key=key)["Body"].read()
            s3.client.delete_object(Bucket=s3.bucket_name, Key=key)
            with pytest.raises(NativeObjectNotFoundError):
                ops.read_file(a.project, "broken/lost.md")
            assert ops.read_file(a.project, "docs/good.md") == b"healthy"
            s3.client.put_object(Bucket=s3.bucket_name, Key=key, Body=raw)
            assert ops.read_file(a.project, "broken/lost.md") == b"acknowledged"
            assert (
                pg.value(
                    "SELECT jsonb_agg(to_jsonb(r) ORDER BY name) FROM public.version_repository_refs r WHERE project_id="
                    + literal(a.project)
                )
                == before
            )
    finally:
        pg.sql("DELETE FROM public.projects WHERE id=" + literal(a.project))
