"""Server publication races, after stock Git admission (not client preflight)."""

import pytest

from src.version_engine.infrastructure.supabase.ref_authority_repository import (
    RefAuthorityRepository,
)
from src.version_engine.write_engine.git_object_format import encode_object
from src.version_engine.write_engine.ref_transaction import RefTransactionService
from tests.repository_hosting.harness.conformance import remote_snapshot
from tests.repository_hosting.harness.git import Git
from tests.repository_hosting.integration.test_native_s3_transport import (
    native_http as native_http_fixture,
)
from tests.repository_hosting.integration.test_ref_transaction_service import request
from tests.repository_hosting.integration.test_s3_publication import (
    publication as publication_fixture,
)

pytestmark = pytest.mark.hosting_s3
publication = publication_fixture
native_http = native_http_fixture


@pytest.mark.parametrize("atomic", [True, False], ids=["atomic", "non-atomic"])
def test_actual_server_batch_rejects_stale_head_without_false_ack(native_http, tmp_path, monkeypatch, atomic):
    client, remote, auth, transport = native_http
    client.run("remote", "add", "origin", remote)
    client.run("push", "origin", "main")
    old = client.text("rev-parse", "HEAD")
    client.run("clone", "--no-hardlinks", client.path, tmp_path / "winner")
    winner = Git(tmp_path / "winner")
    winning_oid = winner.commit({"winner.txt": b"concurrent winner"})
    proposed = client.commit({"proposal.txt": b"keep local work"})
    other = RefTransactionService(RefAuthorityRepository(transport.control.client), transport.service.backend,
                                  project_id=auth.project)
    def prepare():
        for oid, (kind, body) in winner.objects().items():
            other.backend.put_durable(oid, encode_object(kind, body)[1])
    apply = transport.control.apply
    intercepted = []
    def race(*args):
        if not intercepted:
            intercepted.append(args[4])
            assert request(other, winning_oid, prepare, old=old)["status"] == "committed"
        return apply(*args)
    with monkeypatch.context() as patch:
        patch.setattr(transport.control, "apply", race)
        result = client.run("push", *(["--atomic"] if atomic else []), "origin", "main", "main:refs/heads/side", check=False)
    assert result.returncode != 0
    assert len(intercepted) == 1
    assert len(intercepted[0]) == (2 if atomic else 1)
    assert client.text("rev-parse", "HEAD") == proposed
    assert (client.path / "proposal.txt").read_bytes() == b"keep local work"
    assert auth.state()["oid"] == winning_oid
    actual = remote_snapshot(client, remote, tmp_path / "cold.git")
    expected_refs = {"refs/heads/main": winning_oid}
    expected_objects = winner.objects()
    if atomic:
        assert auth.state(b"refs/heads/side") is None
    else:
        assert auth.state(b"refs/heads/side")["oid"] == proposed
        expected_refs["refs/heads/side"] = proposed
        expected_objects |= client.objects()
    assert Git(tmp_path / "cold.git").refs() == expected_refs
    assert actual.objects == expected_objects
