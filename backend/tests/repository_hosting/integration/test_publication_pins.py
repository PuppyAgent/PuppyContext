"""Real SQL interleavings; physical closure verification is tested separately."""

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from tests.repository_hosting.harness.postgres import literal
from tests.repository_hosting.harness.ref_authority import A, Authority, oid, update

pytestmark = pytest.mark.hosting_live


def rpc(pg, name, *args, check=True):
    return pg.sql(
        f"SET ROLE service_role; SELECT public.{name}("
        + ",".join(literal(v) for v in args) + ");", check=check,
    )


def begin(authority, pin=None, roots=None, generation=1):
    pin = pin or str(uuid.uuid4())
    result = rpc(authority.pg, "begin_version_object_publication", authority.project,
                 "test:writer", pin, generation, roots or {A: "commit"})
    return pin, json.loads(result.stdout)


def seal(authority, pin, digest="f" * 64):
    return rpc(authority.pg, "seal_version_object_publication", authority.project,
               "test:writer", pin, digest)


def gc(authority, token=None, check=True):
    return rpc(authority.pg, "begin_version_repository_gc", authority.project,
               token or str(uuid.uuid4()), check=check)


def test_pin_blocks_gc_until_expired_and_expired_pin_cannot_seal(pg_project):
    pg, project = pg_project
    auth = Authority(pg, project)
    pin, state = begin(auth)
    assert state["gc_epoch"] == 1
    assert "publication_in_progress" in gc(auth, check=False).stderr
    pg.sql(f"UPDATE public.version_object_pins SET expires_at=clock_timestamp()-interval '1 second' WHERE id={literal(pin)}")
    snapshot = json.loads(gc(auth).stdout)
    assert snapshot["gc_epoch"] == 2
    response = rpc(pg, "seal_version_object_publication", project, "test:writer", pin, "f" * 64, check=False)
    assert response.returncode and "publication_pin_unavailable" in response.stderr


def test_gc_fence_never_expires_and_wrong_worker_cannot_release(pg_project):
    pg, project = pg_project
    auth = Authority(pg, project)
    token = str(uuid.uuid4())
    gc(auth, token)
    for name, args in [
        ("begin_version_repository_gc", [project, token]),
        ("begin_version_object_publication", [project, "test:writer", str(uuid.uuid4()), 1, {A: "commit"}]),
        ("finish_version_repository_gc", [project, str(uuid.uuid4())]),
    ]:
        result = rpc(pg, name, *args, check=False)
        assert result.returncode
    assert pg.value(f"SELECT gc_token FROM public.version_repositories WHERE project_id={literal(project)}") == token
    rpc(pg, "finish_version_repository_gc", project, token)
    _, state = begin(auth)
    assert state["gc_epoch"] == 2


def test_seal_receipt_is_pin_bound_and_immutable(pg_project):
    pg, project = pg_project
    auth = Authority(pg, project)
    pin, _ = begin(auth)
    seal(auth, pin)
    assert seal(auth, pin).returncode == 0
    mismatch = rpc(pg, "seal_version_object_publication", project, "test:writer", pin, "0" * 64, check=False)
    assert mismatch.returncode and "receipt_mismatch" in mismatch.stderr
    stolen = rpc(pg, "seal_version_object_publication", project, "test:other", pin, "f" * 64, check=False)
    assert stolen.returncode
    auth.receipt = pin
    assert auth.apply([update(new=oid(A))])["status"] == "committed"
    rpc(pg, "release_version_object_publication", project, "test:writer", pin)
    snapshot = json.loads(gc(auth).stdout)
    assert A in snapshot["roots"]


def test_pin_key_cannot_be_rebound_to_another_proposal(pg_project):
    pg, project = pg_project
    auth = Authority(pg, project)
    pin, first = begin(auth)
    assert begin(auth, pin)[1] == first
    changed = rpc(pg, "begin_version_object_publication", project, "test:writer", pin, 1, {"b"*40: "commit"}, check=False)
    assert changed.returncode and "publication_pin_reused" in changed.stderr


def test_gc_and_publication_admission_have_exactly_one_winner(pg_project):
    pg, project = pg_project
    auth = Authority(pg, project)
    gate = Barrier(2)
    def attempt(kind):
        gate.wait(timeout=5)
        if kind == "gc":
            return gc(auth, check=False)
        return rpc(pg, "begin_version_object_publication", project, "test:writer", str(uuid.uuid4()), 1, {A: "commit"}, check=False)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ["gc", "publish"]))
    assert sum(r.returncode == 0 for r in results) == 1, [r.stderr for r in results]


def test_expired_pin_receipt_cannot_publish_after_gc_advances_epoch(pg_project):
    pg, project = pg_project
    auth = Authority(pg, project)
    pin, _ = begin(auth)
    seal(auth, pin)
    # Releasing a producer does not extend its receipt across a later GC epoch.
    rpc(pg, "release_version_object_publication", project, "test:writer", pin)
    gc(auth)
    auth.receipt = pin
    result = pg.sql(auth.query([update(new=oid(A))]), check=False)
    assert result.returncode and "invalid_receipt" in result.stderr
    assert auth.state() is None


@pytest.mark.parametrize("role", ["anon", "authenticated", "service_role"])
def test_direct_pin_and_gc_mutation_is_denied(pg_project, role):
    pg, project = pg_project
    Authority(pg, project)
    for table in ("version_object_pins", "version_repository_gc_runs"):
        result = pg.sql(f"SET ROLE {role}; DELETE FROM public.{table} WHERE project_id={literal(project)};", check=False)
        assert result.returncode and "permission denied" in result.stderr


@pytest.mark.parametrize("role", ["anon", "authenticated"])
def test_client_role_cannot_issue_publication_pin(pg_project, role):
    pg, project = pg_project
    Authority(pg, project)
    result = pg.sql(f"SET ROLE {role}; SELECT public.begin_version_object_publication({literal(project)},'test:writer',{literal(str(uuid.uuid4()))},1,{literal({A:'commit'})});", check=False)
    assert result.returncode and "permission denied" in result.stderr
