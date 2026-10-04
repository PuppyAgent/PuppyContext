"""Admitted production service against real PG and physical disk objects.

The SQL client shim replaces transport only; these are not real PostgREST/S3
claims. Separate actual-service tests run the same service with those backends.
"""

import json
import uuid
from types import SimpleNamespace

import pytest

from src.platform.authorization.models import (
    GrantSource,
    ProjectCapability,
    ProjectGrant,
    ProjectRole,
)
from src.version_engine.infrastructure.supabase.ref_authority_repository import (
    RefAuthorityRepository,
)
from src.version_engine.storage.object_store import FileSystemBackend
from src.version_engine.write_engine.git_object_format import encode_object
from src.version_engine.write_engine.ref_transaction import RefEdit, RefState, RefTransactionService
from tests.repository_hosting.harness.git import Git
from tests.repository_hosting.harness.postgres import literal
from tests.repository_hosting.harness.ref_authority import Authority

pytestmark = pytest.mark.hosting_live


class SQLClient:
    def __init__(self, pg):
        self.pg = pg

    def rpc(self, name, parameters):
        def execute():
            query = f"SET ROLE service_role; SELECT public.{name}(" + ",".join(
                key + "=>" + literal(value) for key, value in parameters.items()
            ) + ");"
            value = self.pg.value(query)
            return SimpleNamespace(data=json.loads(value) if value else None)
        return SimpleNamespace(execute=execute)


def grant(project, *, writable=True):
    return ProjectGrant(project, "fixture-org", "fixture-user", ProjectRole.EDITOR,
                        GrantSource.PROJECT_MEMBER,
                        frozenset({ProjectCapability.CONTENT_WRITE, ProjectCapability.CONTENT_READ} if writable else {ProjectCapability.CONTENT_READ}))


@pytest.fixture
def publisher(pg_project, tmp_path):
    pg, project = pg_project
    auth = Authority(pg, project)
    # No owner-forged receipt may satisfy these publication tests.
    pg.sql(f"DELETE FROM public.version_publication_receipts WHERE project_id={literal(project)}")
    backend = FileSystemBackend(tmp_path / "durable")
    control = RefAuthorityRepository(SQLClient(pg))
    service = RefTransactionService(control, backend, project_id=project)
    git = Git.init(tmp_path / "client")
    oid = git.commit({"file": b"durable bytes"})
    def prepare():
        for oid, (kind, body) in git.objects().items():
            backend.put(oid, encode_object(kind, body)[1])
    return pg, auth, backend, service, git, oid, prepare


def request(service, oid, prepare, *, key=None, old=None, target=b"refs/heads/main"):
    return service.submit(
        grant(service.project_id), request_key=key or str(uuid.uuid4()), generation=1,
        edits=[RefEdit(target, RefState(oid=old), RefState(oid=oid))],
        roots={oid: "commit"}, prepare=prepare,
    )


def test_production_service_issues_verified_receipt_then_publishes_exact_oid(publisher):
    pg, auth, backend, service, git, oid, prepare = publisher
    result = request(service, oid, prepare)
    assert result["status"] == "committed"
    assert auth.state()["oid"] == oid
    receipt = pg.value(f"SELECT roots FROM public.version_publication_receipts WHERE id={literal(result['receipt_id'])}")
    assert json.loads(receipt) == {oid: "commit"}
    assert service.control.snapshot(auth.project)["ref_sequence"] == 1
    assert backend.get_durable(oid) == encode_object("commit", git.run("cat-file", "commit", oid).stdout)[1]


def test_lost_ack_replays_same_result_after_pin_expiry_and_generation_fence(publisher):
    pg, auth, _backend, service, _git, oid, prepare = publisher
    key = str(uuid.uuid4())
    original = service.control.apply
    results = []
    def lose_ack(*args):
        results.append(original(*args))
        raise ConnectionError("acknowledgement lost")
    service.control.apply = lose_ack
    with pytest.raises(ConnectionError):
        request(service, oid, prepare, key=key)
    service.control.apply = original
    pg.sql(f"""
        UPDATE public.version_repositories SET generation=2,write_state='fenced' WHERE project_id={literal(auth.project)};
        UPDATE public.version_object_pins SET expires_at=clock_timestamp()-interval '1 second' WHERE project_id={literal(auth.project)};
        UPDATE public.version_publication_receipts SET verified_at=clock_timestamp()-interval '2 minutes',
            expires_at=clock_timestamp()-interval '1 second' WHERE project_id={literal(auth.project)};
    """)
    def must_not_upload():
        pytest.fail("committed replay must not upload or re-verify")
    assert request(service, oid, must_not_upload, key=key) == results[0]
    assert auth.count("version_ref_transactions") == 1
    changed = service.submit
    with pytest.raises(Exception, match="request_key_reused"):
        changed(grant(auth.project), request_key=key, generation=1,
                edits=[RefEdit(b"refs/heads/other", RefState(), RefState(oid=oid))],
                roots={oid: "commit"}, prepare=must_not_upload)


def test_missing_physical_dependency_prevents_receipt_and_ack(publisher):
    _pg, auth, backend, service, git, oid, prepare = publisher
    def incomplete():
        prepare()
        tree = git.text("rev-parse", "HEAD^{tree}")
        backend.delete(tree)
    with pytest.raises(RuntimeError, match="cannot verify"):
        request(service, oid, incomplete)
    assert auth.state() is None
    assert auth.count("version_publication_receipts") == 0
    assert auth.count("version_ref_transactions") == 0


def test_stale_old_oid_is_durable_rejection_not_success(publisher):
    _, auth, _, service, git, oid, prepare = publisher
    request(service, oid, prepare)
    newer = git.commit({"other": b"keep local proposal"})
    key = str(uuid.uuid4())
    result = request(service, newer, prepare, key=key)
    assert result["status"] == "rejected" and result["reason"] == "stale_ref"
    assert auth.state()["oid"] == oid
    assert request(service, newer, prepare, key=key) == result


@pytest.mark.parametrize("failure", ["viewer", "other_project", "invalid_name", "head_delete", "branch_type"])
def test_admission_failure_precedes_object_write(publisher, failure):
    _, auth, _, service, _, oid, _ = publisher
    admitted = grant(auth.project)
    target = b"refs/heads/main"
    state = RefState(oid=oid)
    roots = {oid: "commit"}
    if failure == "viewer":
        admitted = grant(auth.project, writable=False)
    elif failure == "other_project":
        admitted = grant("another-project")
    elif failure == "invalid_name":
        target = b"refs/heads/../escape"
    elif failure == "head_delete":
        target, state, roots = b"HEAD", RefState(), {}
    elif failure == "branch_type":
        roots = {oid: "blob"}
    def denied():
        pytest.fail("invalid admission reached object writer")
    with pytest.raises((PermissionError, ValueError)):
        service.submit(admitted, request_key=str(uuid.uuid4()), generation=1,
                       edits=[RefEdit(target, RefState(), state)], roots=roots, prepare=denied)
    assert auth.state() is None
