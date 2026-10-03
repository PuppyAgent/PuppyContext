"""Component guards; live SQL/S3 tests establish actual coordination behavior."""
from types import SimpleNamespace

import pytest

from src.version_engine.derived.repository_gc import RepositoryCollector
from src.version_engine.domain.errors import StorageWriteError
from src.version_engine.storage.backends.s3 import S3StorageBackend, _run_async
from src.version_engine.storage.mutation_context import publication_storage
from src.version_engine.write_engine.git_object_format import encode_object

pytestmark = pytest.mark.hosting_component


class NoIO:
    def __getattr__(self, name):
        pytest.fail(f"unexpected storage/control I/O: {name}")


@pytest.mark.parametrize("batch", [False, True])
def test_foreign_publication_context_rejects_before_any_storage_io(batch):
    backend = S3StorageBackend(NoIO(), "target-project")
    oid, loose = encode_object("blob", b"valid bytes")
    with publication_storage("other-project", "actor", "pin"), pytest.raises(StorageWriteError, match="another Project"):
        _run_async(backend.async_put_many({oid: loose}) if batch else backend.async_put(oid, loose))


@pytest.mark.parametrize("batch", [False, True])
def test_incorrect_object_identity_rejects_before_any_storage_io(batch):
    backend = S3StorageBackend(NoIO(), "project")
    _oid, loose = encode_object("blob", b"valid bytes, wrong claimed identity")
    with pytest.raises(StorageWriteError):
        _run_async(backend.async_put_many({"f" * 40: loose}) if batch else backend.async_put("f" * 40, loose))


def test_missing_registration_capability_never_falls_back_to_table_upsert():
    class Client:
        def rpc(self, name, params):
            assert name == "register_version_object_locations"
            assert params["p_project_id"] == "project" and params["p_pin_id"] == "pin"
            raise RuntimeError("PGRST202 missing registration capability")

        def table(self, *_args):
            pytest.fail("native location registration downgraded to direct DML")

    backend = S3StorageBackend(NoIO(), "project", supabase=SimpleNamespace(client=Client()))
    with publication_storage("project", "actor", "pin"), pytest.raises(StorageWriteError, match="PGRST202"):
        _run_async(backend._async_upsert_object_locations([{"object_id": "a" * 40}]))


def test_missing_deletion_capability_rejects_before_physical_delete():
    class Client:
        def rpc(self, name, _params):
            assert name == "authorize_version_object_deletion"
            raise RuntimeError("PGRST202 missing deletion capability")

    backend = S3StorageBackend(NoIO(), "project", supabase=SimpleNamespace(client=Client()))
    with pytest.raises(StorageWriteError, match="PGRST202"):
        backend.delete("a" * 40)


def test_collector_checks_project_binding_before_gc_admission():
    backend = S3StorageBackend(NoIO(), "other-project")
    repo = SimpleNamespace(_project_id="project", store=SimpleNamespace(_backend=backend))
    with pytest.raises(ValueError, match="another Project"):
        RepositoryCollector(NoIO()).run(repo, dry_run=False)
