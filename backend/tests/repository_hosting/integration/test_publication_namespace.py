import pytest

from src.version_engine.domain.errors import ObjectNotFoundError, StorageWriteError
from src.version_engine.storage.backends.s3 import ObjectStorageLayout, S3StorageBackend
from src.version_engine.write_engine.git_object_format import encode_object
from src.version_engine.write_engine.ref_transaction import RefTransactionService
from tests.repository_hosting.harness.postgres import literal
from tests.repository_hosting.integration.test_s3_publication import (
    publication as publication_fixture,
    seed_objects,
)

pytestmark = pytest.mark.hosting_s3
publication = publication_fixture


def test_backend_project_binding_precedes_any_publication(publication):
    pg, auth, _s3, _db, backend, service, _git, _oid, _prepare = publication
    with pytest.raises(ValueError, match="another Project"):
        RefTransactionService(service.control, backend, project_id="another-project")
    assert pg.value(f"SELECT count(*) FROM public.version_object_pins WHERE project_id={literal(auth.project)}") == "0"


def test_noncanonical_storage_remains_readable_but_cannot_issue_native_proof(publication):
    _pg, auth, s3, db, _backend, service, _git, _oid, _prepare = publication
    alternate = S3StorageBackend(s3, auth.project, supabase=db,
                                storage_layout=ObjectStorageLayout(auth.project, primary_namespace="noncanonical-fixture"))
    oid, loose = encode_object("blob", b"compatibility bytes")
    alternate.put(oid, loose)
    assert alternate.get(oid) == loose
    with pytest.raises(ValueError, match="canonical object namespace"):
        RefTransactionService(service.control, alternate, project_id=auth.project)
    with pytest.raises(ObjectNotFoundError):
        alternate.get_durable(oid)
    assert auth.count("version_publication_receipts") == 0


def test_foreign_location_metadata_is_rejected_before_object_io(publication, monkeypatch):
    pg, auth, s3, db, _backend, _service, _git, oid, prepare = publication
    with seed_objects(publication, {oid: "commit"}):
        prepare()
    pg.sql(f"UPDATE public.version_object_locations SET pack_key='version/another-project/object-bundles/foreign.pob' WHERE project_id={literal(auth.project)} AND object_id={literal(oid)}")
    def forbidden(*args, **kwargs):
        pytest.fail("foreign object namespace was accessed")
    monkeypatch.setattr(s3, "download_file", forbidden)
    monkeypatch.setattr(s3, "download_file_range", forbidden)
    fresh = S3StorageBackend(s3, auth.project, supabase=db)
    with pytest.raises(StorageWriteError, match="canonical Project namespace"):
        fresh.get_durable(oid)
