import pytest

from src.version_engine.write_engine.git_object_format import encode_object
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


def copy_objects(service, git):
    for oid, (kind, body) in git.objects().items():
        service.backend.put_durable(oid, encode_object(kind, body, object_format=service.object_format)[1])


@pytest.mark.parametrize("publication", ["sha1", "sha256"], indirect=True)
def test_empty_http_clone_preserves_format_and_unborn_head(native_http, tmp_path):
    source, remote, _auth, transport = native_http
    source.run("clone", "--mirror", remote, tmp_path / "empty.git")
    empty = Git(tmp_path / "empty.git")
    assert empty.text("rev-parse", "--show-object-format") == transport.format
    assert empty.text("symbolic-ref", "HEAD") == "refs/heads/main"
    assert empty.refs() == {} and empty.objects() == {}
    empty.run("fsck", "--full", "--strict")


def test_fetch_honors_advertisement_even_after_unrelated_force_update(native_http, tmp_path, monkeypatch):
    source, remote, auth, transport = native_http
    source.run("push", remote, "main")
    old = source.text("rev-parse", "HEAD")
    unrelated = Git.init(tmp_path / "unrelated")
    new = unrelated.commit({"other.txt": b"unrelated history"})
    advertised = transport.info_refs
    interrupted = []
    def race(*args, **kwargs):
        response = advertised(*args, **kwargs)
        if not interrupted:
            interrupted.append(True)
            assert request(transport.service, new, lambda: copy_objects(transport.service, unrelated), old=old)["status"] == "committed"
        return response
    with monkeypatch.context() as patch:
        patch.setattr(transport, "info_refs", race)
        source.run("-c", "protocol.version=0", "clone", "--mirror", remote, tmp_path / "advertised.git")
    assert interrupted == [True] and auth.state()["oid"] == new
    fetched = Git(tmp_path / "advertised.git")
    fetched.run("fsck", "--full", "--strict")
    assert fetched.refs() == {"refs/heads/main": old}
    assert fetched.objects() == source.objects()


def test_verified_but_rejected_proposal_is_not_a_readable_root(native_http, tmp_path):
    source, remote, auth, transport = native_http
    source.run("push", remote, "main")
    old = source.text("rev-parse", "HEAD")
    proposal = source.commit({"unpublished": b"not an advertised or historical ref"})
    result = request(transport.service, proposal, lambda: copy_objects(transport.service, source))
    assert result["status"] == "rejected" and result["reason"] == "stale_ref"
    assert auth.state()["oid"] == old
    assert auth.count("version_publication_receipts") == 2
    reader = Git.init(tmp_path / "reader")
    assert reader.run("fetch", remote, proposal, check=False).returncode != 0
    assert reader.run("cat-file", "-e", proposal, check=False).returncode != 0
    assert reader.refs() == {} and reader.objects() == {}
