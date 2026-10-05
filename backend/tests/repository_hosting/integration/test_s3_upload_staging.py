"""Owned PG/PostgREST/S3 prestaging boundary; not native producer enrollment."""
import asyncio
import zlib
from functools import partial
from types import SimpleNamespace

import httpx
import pytest
from supabase import ClientOptions, create_client

from src.platform.project.write_lease import ProjectWriteLease, ProjectWriteLeaseRepository
from src.platform.upload.jobs import stage_blob_from_s3
from src.version_engine.infrastructure.supabase.repo_manager import VersionRepoManager
from src.version_engine.write_engine.git_object_format import encode_object
from tests.repository_hosting.harness.git import Git
from tests.repository_hosting.harness.postgres import literal
from tests.repository_hosting.harness.s3_service import owned_s3
from tests.repository_hosting.integration.test_ref_transaction_service import request
from tests.repository_hosting.integration.test_s3_publication import (
    publication as publication_fixture,
)

pytestmark = [pytest.mark.hosting_s3, pytest.mark.asyncio]
publication = publication_fixture


@pytest.mark.parametrize("publication", ["sha1", "sha256"], indirect=True)
async def test_native_upload_staging_cannot_touch_acknowledged_repository(publication, monkeypatch):
    pg, auth, s3, db, _, service, _, oid, prepare = publication
    acknowledged = await asyncio.to_thread(request, service, oid, prepare)
    assert acknowledged["status"] == "committed"
    before = service.control.snapshot(auth.project)
    result_count = auth.count("version_ref_transactions")
    objects = s3.client.list_objects_v2(Bucket=s3.bucket_name, Prefix=f"version/{auth.project}/").get("Contents", [])
    retained = {row["Key"]: row["ETag"] for row in objects}
    lease = partial(ProjectWriteLease, repository=ProjectWriteLeaseRepository(db.client))
    with monkeypatch.context() as patch:
        for method in ("head_object", "get_object", "put_object"):
            patch.setattr(s3.client, method, lambda **_: pytest.fail("native upload performed storage I/O"))
        with pytest.raises(RuntimeError, match=r"native.*staging"):
            await stage_blob_from_s3(s3, project_id=auth.project, src_key="unused-input",
                                     repo_manager=VersionRepoManager(s3, db), write_lease_factory=lease)
    assert service.control.snapshot(auth.project) == before
    assert auth.count("version_ref_transactions") == result_count
    after = s3.client.list_objects_v2(Bucket=s3.bucket_name, Prefix=f"version/{auth.project}/").get("Contents", [])
    assert {row["Key"]: row["ETag"] for row in after} == retained
    assert pg.value(f"SELECT count(*) FROM public.version_product_operations WHERE project_id={literal(auth.project)}") == "0"


@pytest.mark.parametrize("resident", ["absent", "same_length_corrupt", "valid_alternate"])
async def test_legacy_upload_staging_actual_content_proof(pg_project, tmp_path, resident, monkeypatch):
    pg, project = pg_project
    raw = b"uploaded original bytes\n" * 100
    oid, loose = encode_object("blob", raw)
    destination = f"version/{project}/objects/{oid[:2]}/{oid[2:]}"
    source = f"projects/{project}/upload-input"
    with owned_s3() as (s3, api), httpx.Client(timeout=15, trust_env=False) as http:
        sdk = create_client(str(api.client.base_url), api.headers("service_role")["apikey"],
                            ClientOptions(httpx_client=http, auto_refresh_token=False, persist_session=False))
        db = SimpleNamespace(client=sdk)
        manager = VersionRepoManager(s3, db)
        assert manager.repository_metadata(project) is None
        before = pg.value(f"SELECT row_to_json(p) FROM public.projects p WHERE id={literal(project)}")
        # Fixture-owned raw input/corruption; all writes under test use the real service.
        s3.client.put_object(Bucket=s3.bucket_name, Key=source, Body=raw)
        alternate = zlib.compress(f"blob {len(raw)}\0".encode() + raw, level=0)
        if resident != "absent":
            body = alternate if resident == "valid_alternate" else b"x" * len(loose)
            s3.client.put_object(Bucket=s3.bucket_name, Key=destination, Body=body)
        lease = partial(ProjectWriteLease, repository=ProjectWriteLeaseRepository(sdk))
        with monkeypatch.context() as patch:
            if resident == "valid_alternate":
                patch.setattr(s3.client, "put_object", lambda **_: pytest.fail("valid object was replaced"))
            ref = await stage_blob_from_s3(s3, project_id=project, src_key=source,
                                           repo_manager=manager, write_lease_factory=lease)
        assert ref.hash == oid and ref.size == len(raw)
        physical = await s3.download_file(destination)
        assert physical == (alternate if resident == "valid_alternate" else loose)
        # Stock bare Git verifies the actual S3 loose object, without a worktree/cache.
        cold = Git.init(tmp_path / "cold.git", bare=True)
        path = cold.path / "objects" / oid[:2] / oid[2:]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(physical)
        assert cold.run("cat-file", "blob", oid).stdout == raw
        cold.run("fsck", "--full", "--strict")
        assert await s3.download_file(source) == raw
        assert pg.value(f"SELECT row_to_json(p) FROM public.projects p WHERE id={literal(project)}") == before
        assert pg.value(f"SELECT count(*) FROM public.version_ref_transactions WHERE project_id={literal(project)}") == "0"
