"""Real PG/PostgREST/S3 counts for shared reads; owned synthetic fixtures only."""

import asyncio
from math import ceil
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.platform.authorization.models import ProjectAction
from src.platform.authorization.repository import AuthorizationRepository
from src.platform.authorization.service import AuthorizationService
from src.platform.project.write_lease import ProjectWriteLease
from src.version_engine.entrypoints.http import content_read
from src.version_engine.storage.backends.s3 import S3StorageBackend
from src.version_engine.write_engine.git_object_format import encode_object
from tests.agent.runtime import conftest as owned
from tests.agent.runtime.test_data_access import measured
from tests.agent.runtime.test_supervisor import prepared as prepared_fixture

postgres = owned.postgres
control_api = owned.control_api
object_endpoint = owned.object_endpoint
services = owned.services
submitted = owned.submitted
prepared = prepared_fixture

# The ordinary hosting component job has no PG/PostgREST/S3 services. The
# Agent runtime job provisions these owned fixtures and opts into both layers.
pytestmark = [pytest.mark.integration, pytest.mark.hosting_live, pytest.mark.hosting_s3]


def test_batch_migration_upgrades_populated_authorization(postgres, submitted):
    from pathlib import Path

    args, _ = submitted
    query = f"SELECT authorization_project_facts('{args['project']}', '{args['user']}')"
    before = postgres.sql(query)
    # Remove only this release's new function in the disposable test cluster,
    # then apply the immutable release over existing projects and memberships.
    postgres.sql("DROP FUNCTION authorization_project_facts_batch(text[], uuid)")
    migration = (
        Path(__file__).resolve().parents[4]
        / "supabase/migrations/20261008040000_batch_project_authorization_facts.sql"
    )
    postgres.sql(migration.read_text())
    assert postgres.sql(query) == before
    assert (
        postgres.sql(
            f"SELECT authorization_project_facts_batch(ARRAY['{args['project']}'], '{args['user']}')"
        )
        == before
    )


@pytest.mark.parametrize("count", [0, 1, 99, 100, 101])
def test_authorization_batch_is_set_based_and_bounded(control_api, postgres, submitted, count):
    args, _ = submitted
    org = postgres.row(f"SELECT org_id FROM projects WHERE id='{args['project']}'")["org_id"]
    prefix = str(uuid4())
    postgres.sql(f"""
        BEGIN;
        INSERT INTO projects(id, name, org_id, created_by, lifecycle_status)
        SELECT '{prefix}-' || i, 'Budget project', '{org}', '{args["user"]}', 'ready'
        FROM generate_series(1, {count}) i;
        INSERT INTO project_members(project_id, org_id, user_id, role)
        SELECT '{prefix}-' || i, '{org}', '{args["user"]}', 'admin'
        FROM generate_series(1, {count}) i;
        COMMIT;
    """)
    ids = [f"{prefix}-{i}" for i in range(1, count + 1)]
    repository = AuthorizationRepository(control_api)
    facts, report = measured(
        "authorization.batch", lambda: repository.load_project_facts_batch(ids + ids, args["user"])
    )
    assert set(facts) == set(ids)
    assert report["attempts"] == ceil(count / 100), report
    assert report["failures"] == 0
    assert all(value.org_role == "owner" for value in facts.values())


def test_authorization_single_matches_batch_and_observes_revocation(
    control_api, postgres, submitted
):
    args, _ = submitted
    repo = AuthorizationRepository(control_api)
    project, user = args["project"], str(uuid4())
    org = postgres.row(f"SELECT org_id FROM projects WHERE id='{project}'")["org_id"]
    postgres.sql(
        f"INSERT INTO auth.users(id) VALUES('{user}'); INSERT INTO org_members(org_id,user_id,role) VALUES('{org}','{user}','member'); INSERT INTO project_members(project_id,org_id,user_id,role) VALUES('{project}','{org}','{user}','editor');"
    )
    single, report = measured(
        "authorization.single", lambda: repo.load_project_facts(project, user)
    )
    assert report["attempts"] == 1, report
    assert single == repo.load_project_facts_batch([project], user)[project]
    postgres.sql(
        f"DELETE FROM project_members WHERE project_id='{project}' AND user_id='{user}'; DELETE FROM org_members WHERE user_id='{user}';"
    )
    facts, report = measured(
        "authorization.revoked", lambda: repo.load_project_facts(project, user)
    )
    assert report["attempts"] == 1 and facts.org_role is None, report
    from src.exceptions import NotFoundException

    with pytest.raises(NotFoundException):
        AuthorizationService.authorize_facts(facts, project, user, ProjectAction.CONTENT_READ)
    assert repo.load_project_facts(str(uuid4()), user) is None
    assert repo.load_project_facts_batch([str(uuid4())], user) == {}


def test_authorization_batch_is_backend_only_and_rejects_oversize(postgres, submitted):
    args, _ = submitted
    for role in ("anon", "authenticated"):
        with pytest.raises(RuntimeError, match="permission denied"):
            postgres.sql(
                f"SET ROLE {role}; SELECT authorization_project_facts_batch(ARRAY['{args['project']}'], '{args['user']}');"
            )
    with pytest.raises(RuntimeError, match="invalid_authorization_batch"):
        postgres.sql(
            f"SELECT authorization_project_facts_batch(array_fill('p'::text, ARRAY[101]), '{args['user']}');"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 101])
async def test_bulk_s3_reads_reuse_missing_location_results(services, submitted, count):
    client, storage, _ = services
    project = submitted[0]["project"]
    backend = S3StorageBackend(storage, project, supabase=SimpleNamespace(client=client))
    objects = dict(encode_object("blob", f"object-{i}".encode()) for i in range(count))
    async with ProjectWriteLease(project, "fixture.objects"):
        for oid, data in objects.items():
            await storage.upload_file(backend._key_for(oid), data)
    result, report = await asyncio.to_thread(
        measured, "objects.batch", lambda: backend.get_many([*objects, *objects])
    )
    assert result == objects
    assert report["attempts"] == ceil(count / 100), report
    # A subsequent attempt must consult current locations; no global negative cache.
    oid = next(iter(objects))
    result, report = await asyncio.to_thread(
        measured, "object.durable", lambda: backend.get_durable(oid)
    )
    assert result == objects[oid] and report["attempts"] == 1, report


@pytest.mark.asyncio
async def test_content_reads_and_same_snapshot_reuse_have_real_budgets(prepared, services):
    c = prepared
    client, storage, _ = services
    async with ProjectWriteLease(c.project, "fixture.read-budget"):
        await c.ops.bulk_write(
            c.project, {"note.md": b"hello", "other.md": b"world"}, who="user:" + c.user
        )
    authorization = AuthorizationService(AuthorizationRepository(client))
    s3_requests = []
    storage.client.meta.events.register(
        "before-send.s3", lambda **kw: s3_requests.append(kw["event_name"])
    )
    for method, path, budget, reads in [
        (content_read.read_file, "note.md", 6, 3),
        (content_read.list_dir, "", 5, 2),
    ]:
        s3_requests.clear()
        response, report = await asyncio.to_thread(
            measured,
            method.__name__,
            lambda method=method, path=path: method(
                c.project,
                path=path,
                ops=c.ops,
                authorization=authorization,
                current_user=SimpleNamespace(user_id=c.user),
            ),
        )
        assert response.code == 0
        if path:
            assert response.data.content_text == "hello"
        else:
            assert {entry.name for entry in response.data.entries} == {"note.md", "other.md"}
        assert report["attempts"] == budget, report
        assert report["operations"].get("POST rpc/begin_admitted_version_repository_read") == 1, (
            report
        )
        assert "POST rpc/get_version_repository_snapshot" not in report["operations"], report
        assert "POST rpc/get_version_pinned_object_locations" not in report["operations"], report
        assert len(s3_requests) == reads, s3_requests

    grant = authorization.authorize(c.project, c.user, ProjectAction.CONTENT_READ)

    def repeated_read():
        with c.ops.open_read(c.project, grant) as reader:
            for _ in range(10):
                assert reader.read_file(c.project, "note.md") == b"hello"
        return reader

    reader, report = await asyncio.to_thread(measured, "snapshot.reuse", repeated_read)
    assert report["attempts"] == 5, report  # begin + commit/tree/blob + release
    with pytest.raises(RuntimeError, match="closed"):
        reader.read_file(c.project, "note.md")
