"""Actual native PG/S3 selection and metadata discovery, with synthetic grants."""
import asyncio
import base64
import uuid

import pytest
from postgrest.exceptions import APIError

from src.platform.project.write_lease import ProjectWriteLease, ProjectWriteLeaseRepository
from src.version_engine.adapters.product.operation_adapter import ProductOperationAdapter
from src.version_engine.adapters.product.tree_patch import splice_batch
from src.version_engine.write_engine.native_operation_writer import NativeOperationWriter
from src.version_engine.write_engine.ref_transaction import RefEdit, RefState
from tests.repository_hosting.harness.postgres import literal
from tests.repository_hosting.integration.test_native_s3_product_attempts import setup
from tests.repository_hosting.integration.test_s3_publication import (
    publication as publication_fixture,
)

pytestmark = [pytest.mark.hosting_s3, pytest.mark.asyncio]
publication = publication_fixture


@pytest.mark.parametrize("publication", ["sha1", "sha256"], indirect=True)
async def test_native_ref_reads_keep_original_selection_while_ref_moves(publication, monkeypatch):
    pg, auth, s3, db, grant, _, manager, service, initial = setup(publication)
    writer = NativeOperationWriter(service)
    def lease():
        return ProjectWriteLease(auth.project, "ref-read-fixture", repository=ProjectWriteLeaseRepository(db.client))
    async with lease():
        first = await asyncio.to_thread(writer.apply, grant, **initial)
        oid, tree = first["product"]["commit_oid"], first["product"]["tree_oid"]
        name = b"refs/heads/raw-\xff"
        result = await asyncio.to_thread(service.submit, grant, request_key=str(uuid.uuid4()), generation=1,
            edits=[RefEdit(name, RefState(), RefState(oid=oid)),
                   RefEdit(b"refs/tags/tree", RefState(), RefState(oid=tree))],
            roots={oid: "commit", tree: "tree"}, prepare=lambda: None)
        assert result["status"] == "committed"
    ops = ProductOperationAdapter(manager)
    context = ops.open_read(auth.project, grant, selector=name)
    reader = await asyncio.to_thread(context.__enter__)
    try:
        original = reader.get_read_revision(auth.project)
        assert original["target_ref"] is None and original["target_ref_b64"] == base64.b64encode(name).decode()
        assert original["head_guard"] is None
        async with lease():
            second = await asyncio.to_thread(writer.apply, grant, request_key=str(uuid.uuid4()), base=original,
                input_sha256="c" * 64, message="explicit branch", splice=lambda store, root: splice_batch(store, root, [("put", "file", b"branch data")]))
        assert second["status"] == "committed"
        assert await asyncio.to_thread(reader.read_file, auth.project, "file") == b"data"
        assert reader.get_read_revision(auth.project) == original
    finally:
        await asyncio.to_thread(context.__exit__, None, None, None)

    def read(selector):
        with ops.open_read(auth.project, grant, selector=selector) as selected:
            return selected.read_file(auth.project, "file"), selected.get_read_revision(auth.project)
    assert (await asyncio.to_thread(read, b"HEAD"))[0] == b"data"
    assert (await asyncio.to_thread(read, name))[0] == b"branch data"
    assert (await asyncio.to_thread(read, b"refs/tags/tree"))[0] == b"data"
    before = pg.value(f"SELECT count(*) FROM public.version_object_pins WHERE project_id={literal(auth.project)}")
    # No storage service, snapshot/read pin, lease, or current write entitlement.
    pg.sql(f"UPDATE public.version_repository_file_policies SET initialized=false WHERE project_id={literal(auth.project)};"
           f"UPDATE public.access_surface_credentials SET grant_mode='r' WHERE id={literal(grant.principal.principal_id)}")
    with monkeypatch.context() as patch:
        patch.setattr(manager, "get_native_service", lambda *_: pytest.fail("metadata constructed storage"))
        for method in ("get_object", "head_object", "put_object"):
            patch.setattr(s3.client, method, lambda **_: pytest.fail("metadata touched objects"))
        metadata = await ops.native_ref_metadata(auth.project, grant)
        refs = {base64.b64decode(row["name_b64"]): row for row in metadata["refs"]}
        assert refs[name]["state"]["oid"] == second["product"]["commit_oid"]
        assert refs[b"refs/tags/tree"]["object_kind"] == "tree"
        assert pg.value(f"SELECT count(*) FROM public.version_object_pins WHERE project_id={literal(auth.project)}") == before
        pg.sql(f"UPDATE public.access_surface_credentials SET status='revoked' WHERE id={literal(grant.principal.principal_id)}")
        with pytest.raises(APIError, match="repository_action_denied"):
            await ops.native_ref_metadata(auth.project, grant)
