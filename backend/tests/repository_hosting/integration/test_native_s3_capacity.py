"""Real S3/PG technical quota with admitted credentials, not canonical HTTP/billing."""
from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest

from src.platform.authorization.models import RuntimeGrant, RuntimeMode, RuntimePrincipal
from src.platform.project.write_lease import (
    ProjectWriteLease,
    ProjectWriteLeaseRepository,
    active_project_write_lease,
)
from src.platform.repository_target.models import ProjectRootTarget, ResolvedRepositoryView
from src.version_engine.derived.repository_gc import RepositoryCollector
from src.version_engine.domain.errors import StorageWriteError
from src.version_engine.infrastructure.supabase.capacity_repository import RepositoryCapacity
from src.version_engine.infrastructure.supabase.ref_authority_repository import (
    AdmittedRefAuthorityRepository,
)
from src.version_engine.infrastructure.supabase.repo_manager import VersionRepoManager
from src.version_engine.storage.backends.s3 import S3StorageBackend
from src.version_engine.storage.mutation_context import publication_storage
from src.version_engine.storage.publication import ClosureVerifier
from src.version_engine.write_engine.git_object_format import encode_object
from src.version_engine.write_engine.ref_transaction import (
    RefEdit,
    RefState,
    RefTransactionService,
    admitted_actor,
)
from tests.repository_hosting.harness.postgres import literal
from tests.repository_hosting.integration.test_repository_write_admission import seed_credential
from tests.repository_hosting.integration.test_s3_publication import (
    publication as publication_fixture,
)

pytestmark = [pytest.mark.hosting_s3, pytest.mark.asyncio]
publication = publication_fixture


def capacity_usage(pg, project):
    return json.loads(pg.value(f"SELECT jsonb_build_array(used_body_bytes,used_objects) FROM public.version_repository_capacity "
                              f"WHERE project_id={literal(project)}"))


def enroll(publication):
    pg, auth, _s3, db, backend, original, _git, _oid, _prepare = publication
    user = pg.value(f"SELECT created_by FROM public.projects WHERE id={literal(auth.project)}")
    org = pg.value(f"SELECT org_id FROM public.projects WHERE id={literal(auth.project)}")
    _surface, credential, _actor = seed_credential(SimpleNamespace(pg=pg, project=auth.project, org=org, user=user))
    target = ProjectRootTarget(auth.project)
    grant = RuntimeGrant(RuntimePrincipal(credential, 'git_http_token'), target,
                         ResolvedRepositoryView(target, '', (), 'rw'), RuntimeMode.READ_WRITE)
    # Explicit, synthetic, empty-repository enrollment. No live-data backfill.
    pg.sql(f"INSERT INTO public.version_organization_capacity(org_id,initialized,max_body_bytes,max_objects) "
           f"VALUES({literal(org)},true,1000000,10000); "
           f"INSERT INTO public.version_repository_capacity(project_id,org_id,initialized,max_body_bytes,max_objects) "
           f"VALUES({literal(auth.project)},{literal(org)},true,1000000,10000)")
    control = AdmittedRefAuthorityRepository(db.client, lease_provider=active_project_write_lease)
    service = RefTransactionService(control, backend, project_id=auth.project, object_format=original.object_format,
                                    capacity=RepositoryCapacity(control))
    return service, grant, org


@pytest.mark.parametrize('publication', ['sha1', 'sha256'], indirect=True)
@pytest.mark.parametrize('limit', ['body_bytes', 'objects'])
async def test_named_ref_capacity_denial_precedes_s3_put_and_preserves_ack(publication, limit, monkeypatch):
    pg, auth, s3, db, backend, _original, git, oid, prepare = publication
    service, grant, org = enroll(publication)
    control = service.control
    billing_before = pg.value(f"SELECT COALESCE(jsonb_agg(to_jsonb(c)),'[]') FROM public.organization_usage_counters c WHERE org_id={literal(org)}")
    first_args = dict(request_key=str(uuid.uuid4()), generation=1,
                      edits=[RefEdit(b'refs/heads/main', RefState(), RefState(oid=oid))],
                      roots={oid: 'commit'}, prepare=prepare)
    async with ProjectWriteLease(auth.project, 'native-capacity', repository=ProjectWriteLeaseRepository(db.client)):
        first = await asyncio.to_thread(service.submit, grant, **first_args)
        assert first['status'] == 'committed'
        before = control.snapshot(auth.project)
        used = capacity_usage(pg, auth.project)
        assert used[0] == sum(len(body) for _kind, body in git.objects().values())
        # Intrinsic empty tree is physically admitted in addition to the graph.
        assert used[1] == len(git.objects()) + 1
        ceiling = used[0] + 64 if limit == 'body_bytes' else used[1]
        pg.sql(f"UPDATE public.version_repository_capacity SET max_{limit}={ceiling} WHERE project_id={literal(auth.project)}")
        new_oid, loose = encode_object('blob', b'A' * 4096, object_format=service.object_format)
        assert len(loose) < 64  # Compressed size alone would incorrectly fit.
        puts = []
        original_put = s3.upload_file

        async def record_put(*args, **kwargs):
            puts.append(args[0])
            return await original_put(*args, **kwargs)

        monkeypatch.setattr(s3, 'upload_file', record_put)
        with pytest.raises(Exception, match='repository_capacity_exceeded'):
            await asyncio.to_thread(service.submit, grant, request_key=str(uuid.uuid4()), generation=1,
                                    edits=[RefEdit(b'refs/tags/large-blob', RefState(), RefState(oid=new_oid))],
                                    roots={new_oid: 'blob'}, prepare=lambda: backend.put_durable(new_oid, loose))
        assert puts == []
        assert capacity_usage(pg, auth.project) == used
        after = control.snapshot(auth.project)
        assert (after['refs'], after['ref_sequence']) == (before['refs'], before['ref_sequence'])
        fresh = S3StorageBackend(s3, auth.project, supabase=db)
        assert ClosureVerifier(fresh, object_format=service.object_format).verify({oid: 'commit'})
        assert auth.count('version_ref_transactions') == 1
    # Original result remains a read even when capacity is suspended. It neither
    # reserves again nor needs the expired/released write lease.
    pg.sql(f"UPDATE public.version_repository_capacity SET initialized=false WHERE project_id={literal(auth.project)}")
    first_args['prepare'] = lambda: pytest.fail('result replay uploaded again')
    read_grant = RuntimeGrant(grant.principal, grant.target, grant.repository_view, RuntimeMode.READ)
    assert await asyncio.to_thread(service.submit, read_grant, **first_args) == first
    assert capacity_usage(pg, auth.project) == used
    assert pg.value(f"SELECT COALESCE(jsonb_agg(to_jsonb(c)),'[]') FROM public.organization_usage_counters c WHERE org_id={literal(org)}") == billing_before


@pytest.mark.parametrize('publication', ['sha1', 'sha256'], indirect=True)
async def test_same_pin_retry_cannot_settle_an_earlier_s3_invocation(publication, monkeypatch):
    pg, auth, s3, db, backend, _original, _git, oid, prepare = publication
    service, grant, _org = enroll(publication)
    control = service.control
    actor = admitted_actor(grant, auth.project, write=True)
    proposed, loose = encode_object('blob', b'retry with unsettled IO', object_format=service.object_format)
    pending = []
    original_settle = S3StorageBackend._async_settle_capacity

    async def lose_settlement(self, io_id):
        if not pending:
            pending.append(io_id)
            raise RuntimeError('injected post-PUT settlement outage')
        await original_settle(self, io_id)

    async with ProjectWriteLease(auth.project, 'same-pin-recovery', repository=ProjectWriteLeaseRepository(db.client)):
        await asyncio.to_thread(service.submit, grant, request_key=str(uuid.uuid4()), generation=1,
                                edits=[RefEdit(b'refs/heads/main', RefState(), RefState(oid=oid))],
                                roots={oid: 'commit'}, prepare=prepare)
        before = control.snapshot(auth.project)
        monkeypatch.setattr(S3StorageBackend, '_async_settle_capacity', lose_settlement)
        args = dict(request_key=str(uuid.uuid4()), generation=1,
                    edits=[RefEdit(b'refs/tags/recovery', RefState(), RefState(oid=proposed))],
                    roots={proposed: 'blob'}, prepare=lambda: backend.put_durable(proposed, loose))
        with pytest.raises(StorageWriteError, match='post-PUT settlement outage'):
            await asyncio.to_thread(service.submit, grant, **args)
        allocated = capacity_usage(pg, auth.project)
        with pytest.raises(Exception, match='repository_capacity_io_unsettled'):
            await asyncio.to_thread(service.submit, grant, **args)
        assert capacity_usage(pg, auth.project) == allocated
        assert control.snapshot(auth.project)['refs'] == before['refs']
        rows = json.loads(pg.value(f"SELECT jsonb_agg(jsonb_build_object('pin',pin_id,'io',io_id)) "
                                  f"FROM public.version_repository_capacity_inflight WHERE project_id={literal(auth.project)}"))
        assert rows == [dict(pin=rows[0]['pin'], io=pending[0])]
        fresh = S3StorageBackend(s3, auth.project, supabase=db)
        assert ClosureVerifier(fresh, object_format=service.object_format).verify({oid: 'commit'})
        # The injected failure occurred after awaited S3/index completion but
        # before sending SQL. We know THIS invocation's I/O is quiescent; it is
        # not inferred from pin state, another retry's success, or elapsed time.
        control.call('settle_version_object_capacity_io', p_project_id=auth.project, p_actor=actor,
                     p_pin_id=rows[0]['pin'], p_io_id=pending[0])
        recovered = await asyncio.to_thread(service.submit, grant, **args)
        assert recovered['status'] == 'committed'
        assert capacity_usage(pg, auth.project) == allocated
        assert ClosureVerifier(fresh, object_format=service.object_format).verify({oid: 'commit', proposed: 'blob'})


@pytest.mark.parametrize('publication', ['sha1', 'sha256'], indirect=True)
async def test_capacity_collection_recovers_absent_objects_and_post_delete_sql_failure(publication, monkeypatch):
    pg, auth, s3, db, backend, _original, _git, oid, prepare = publication
    service, grant, _org = enroll(publication)
    control = service.control
    actor = admitted_actor(grant, auth.project, write=True)
    orphan, loose = encode_object('blob', b'orphan uploaded completely', object_format=service.object_format)
    absent, _ = encode_object('blob', b'reserved without a PUT', object_format=service.object_format)
    async with ProjectWriteLease(auth.project, 'capacity-collection', repository=ProjectWriteLeaseRepository(db.client)):
        first = await asyncio.to_thread(service.submit, grant, request_key=str(uuid.uuid4()), generation=1,
                                       edits=[RefEdit(b'refs/heads/main', RefState(), RefState(oid=oid))],
                                       roots={oid: 'commit'}, prepare=prepare)
        assert first['status'] == 'committed'
        baseline = capacity_usage(pg, auth.project)
        pin = str(uuid.uuid4())
        control.begin(auth.project, actor, pin, 1, {orphan: 'blob', absent: 'blob'})
        with publication_storage(auth.project, actor, pin, require_capacity=True):
            await asyncio.to_thread(backend.put_durable, orphan, loose)
        io_id = str(uuid.uuid4())
        control.call('reserve_version_object_capacity', p_project_id=auth.project, p_actor=actor, p_pin_id=pin,
                     p_objects=[dict(object_id=absent, object_kind='blob', body_bytes=len(b'reserved without a PUT'))],
                     p_io_id=io_id)
        # All actual physical/index I/O was awaited; this SQL-only reservation
        # issued no PUT and can be explicitly settled independently of the pin.
        control.call('settle_version_object_capacity_io', p_project_id=auth.project, p_actor=actor,
                     p_pin_id=pin, p_io_id=io_id)
        control.release(auth.project, actor, pin)
    original_collect = S3StorageBackend._collect_capacity
    failed = False

    def fail_after_physical_delete(self, objects):
        nonlocal failed
        if orphan in objects and not failed:
            failed = True
            raise RuntimeError('injected post-delete quota RPC failure')
        return original_collect(self, objects)

    monkeypatch.setattr(S3StorageBackend, '_collect_capacity', fail_after_physical_delete)
    repo = VersionRepoManager(s3, db).get_gc_repo(auth.project)
    result = await asyncio.to_thread(RepositoryCollector(control).run, repo, dry_run=False, retention_seconds=0)
    assert failed and result.errors
    token = pg.value(f"SELECT gc_token FROM public.version_repositories WHERE project_id={literal(auth.project)}")
    assert token
    fresh = S3StorageBackend(s3, auth.project, supabase=db)
    assert not fresh.exists(orphan)
    assert ClosureVerifier(fresh, object_format=service.object_format).verify({oid: 'commit'})
    # The failed RPC was injected before sending any SQL; all actual DELETEs and
    # the worker have returned. This is explicit quiescence, never TTL-based unlock.
    monkeypatch.setattr(S3StorageBackend, '_collect_capacity', original_collect)
    control.finish_gc(auth.project, token)
    cold = VersionRepoManager(s3, db).get_gc_repo(auth.project)
    recovered = await asyncio.to_thread(RepositoryCollector(control).run, cold, dry_run=False)
    assert not recovered.errors and not recovered.sweep_skipped_for_safety
    assert capacity_usage(pg, auth.project) == baseline
    assert ClosureVerifier(fresh, object_format=service.object_format).verify({oid: 'commit'})


@pytest.mark.parametrize('publication', ['sha1', 'sha256'], indirect=True)
async def test_expired_unsettled_capacity_keeps_gc_fenced_until_explicit_quiescence(publication):
    pg, auth, s3, db, _backend, _original, _git, oid, prepare = publication
    service, grant, _org = enroll(publication)
    control = service.control
    actor = admitted_actor(grant, auth.project, write=True)
    orphan, _ = encode_object('blob', b'unknown upload outcome', object_format=service.object_format)
    async with ProjectWriteLease(auth.project, 'unsettled-capacity', repository=ProjectWriteLeaseRepository(db.client)):
        await asyncio.to_thread(service.submit, grant, request_key=str(uuid.uuid4()), generation=1,
                                edits=[RefEdit(b'refs/heads/main', RefState(), RefState(oid=oid))],
                                roots={oid: 'commit'}, prepare=prepare)
        baseline = capacity_usage(pg, auth.project)
        pin = str(uuid.uuid4())
        control.begin(auth.project, actor, pin, 1, {orphan: 'blob'})
        io_id = str(uuid.uuid4())
        control.call('reserve_version_object_capacity', p_project_id=auth.project, p_actor=actor, p_pin_id=pin,
                     p_objects=[dict(object_id=orphan, object_kind='blob', body_bytes=len(b'unknown upload outcome'))],
                     p_io_id=io_id)
        pg.sql(f"UPDATE public.version_object_pins SET expires_at=clock_timestamp()-interval '1 second' WHERE id={literal(pin)}")
    before = capacity_usage(pg, auth.project)
    repo = VersionRepoManager(s3, db).get_gc_repo(auth.project)
    result = await asyncio.to_thread(RepositoryCollector(control).run, repo, dry_run=False, retention_seconds=0)
    assert result.errors and result.sweep_skipped_for_safety
    assert capacity_usage(pg, auth.project) == before
    token = pg.value(f"SELECT gc_token FROM public.version_repositories WHERE project_id={literal(auth.project)}")
    assert token
    assert ClosureVerifier(S3StorageBackend(s3, auth.project, supabase=db), object_format=service.object_format).verify({oid: 'commit'})
    # The test knows this producer issued no PUT. A real recovery must establish
    # worker AND remote-I/O quiescence before making either transition.
    control.release(auth.project, actor, pin)
    control.call('settle_version_object_capacity_io', p_project_id=auth.project, p_actor=actor,
                 p_pin_id=pin, p_io_id=io_id)
    control.finish_gc(auth.project, token)
    recovered = await asyncio.to_thread(RepositoryCollector(control).run, repo, dry_run=False)
    assert not recovered.errors and not recovered.sweep_skipped_for_safety
    assert capacity_usage(pg, auth.project) == baseline
