"""Owned S3 loses the ACK after real PUT/DELETE; native SDK must not retry."""
from __future__ import annotations

import asyncio
import json
import uuid
from urllib.parse import urlsplit

import pytest
from botocore.exceptions import ReadTimeoutError

from src.platform.project.write_lease import ProjectWriteLease, ProjectWriteLeaseRepository
from src.version_engine.derived.repository_gc import RepositoryCollector
from src.version_engine.domain.errors import StorageWriteError
from src.version_engine.infrastructure.supabase.repo_manager import VersionRepoManager
from src.version_engine.storage.backends.s3 import S3StorageBackend
from src.version_engine.storage.mutation_context import publication_storage
from src.version_engine.storage.publication import ClosureVerifier
from src.version_engine.write_engine.git_object_format import encode_object
from src.version_engine.write_engine.ref_transaction import RefEdit, RefState, admitted_actor
from tests.repository_hosting.harness.postgres import literal
from tests.repository_hosting.integration.test_native_s3_capacity import capacity_usage, enroll
from tests.repository_hosting.integration.test_s3_publication import (
    publication as publication_fixture,
)

pytestmark = [pytest.mark.hosting_s3, pytest.mark.asyncio]
publication = publication_fixture


@pytest.mark.parametrize('publication', ['sha1', 'sha256'], indirect=True)
async def test_native_put_delete_lost_ack_is_single_attempt_and_keeps_uncertainty(publication, monkeypatch):
    pg, auth, s3, db, _backend, _old, _git, oid, prepare = publication
    service, grant, _org = enroll(publication)
    control = service.control
    actor = admitted_actor(grant, auth.project, write=True)
    physical = S3StorageBackend(s3, auth.project, supabase=db)
    orphan, loose = encode_object('blob', b'uncertain transport acknowledgement', object_format=service.object_format)
    key = physical._key_for(orphan)
    strict = s3.for_single_attempt_io().client
    assert strict.meta.config.retries == {'total_max_attempts': 1, 'mode': 'standard'}
    shared_config = dict(s3.client.meta.config.retries)
    send = strict._endpoint.http_session.send
    method = ['PUT']
    sent = []

    def lose_ack(request):
        response = send(request)
        if request.method == method[0] and urlsplit(request.url).path.endswith('/' + key):
            # The owned service really completed the operation; the caller only
            # sees a timeout. This controlled observation is also our later
            # quiescence proof, NOT a generic lease/process-death heuristic.
            assert response.status_code in (200, 204)
            sent.append(request.method)
            response.raw.close()
            raise ReadTimeoutError(endpoint_url=s3.endpoint_url)
        return response

    monkeypatch.setattr(strict._endpoint.http_session, 'send', lose_ack)
    async with ProjectWriteLease(auth.project, 'single-attempt', repository=ProjectWriteLeaseRepository(db.client)):
        result = await asyncio.to_thread(service.submit, grant, request_key=str(uuid.uuid4()), generation=1,
                                        edits=[RefEdit(b'refs/heads/main', RefState(), RefState(oid=oid))],
                                        roots={oid: 'commit'}, prepare=prepare)
        assert result['status'] == 'committed'
        baseline = capacity_usage(pg, auth.project)
        pin = str(uuid.uuid4())
        control.begin(auth.project, actor, pin, 1, {orphan: 'blob'})
        with (publication_storage(auth.project, actor, pin, require_capacity=True),
              pytest.raises(StorageWriteError, match='Read timeout')):
            await asyncio.to_thread(physical.put_durable, orphan, loose)
        assert sent == ['PUT'] and physical.get_durable(orphan) == loose
        rows = json.loads(pg.value(f"SELECT jsonb_agg(io_id) FROM public.version_repository_capacity_inflight "
                                  f"WHERE project_id={literal(auth.project)} AND pin_id={literal(pin)}"))
        assert len(rows) == 1
        control.release(auth.project, actor, pin)
        assert pg.value(f"SELECT count(*) FROM public.version_repository_capacity_inflight "
                        f"WHERE project_id={literal(auth.project)} AND pin_id={literal(pin)}") == '1'
        # Only this invocation's completed request was observed above.
        control.call('settle_version_object_capacity_io', p_project_id=auth.project, p_actor=actor,
                     p_pin_id=pin, p_io_id=rows[0])
    method[0] = 'DELETE'
    before = capacity_usage(pg, auth.project)
    repo = VersionRepoManager(s3, db).get_gc_repo(auth.project)
    result = await asyncio.to_thread(RepositoryCollector(control).run, repo, dry_run=False, retention_seconds=0)
    assert result.errors and sent == ['PUT', 'DELETE']
    token = pg.value(f"SELECT gc_token FROM public.version_repositories WHERE project_id={literal(auth.project)}")
    assert token and capacity_usage(pg, auth.project) == before
    assert ClosureVerifier(physical, object_format=service.object_format).verify({oid: 'commit'})
    # Worker and the actual DELETE are both known complete by this controlled
    # injection. Missing-placement reconciliation now safely releases capacity.
    method[0] = ''
    control.finish_gc(auth.project, token)
    recovered = await asyncio.to_thread(RepositoryCollector(control).run, repo, dry_run=False)
    assert not recovered.errors and not recovered.sweep_skipped_for_safety
    assert capacity_usage(pg, auth.project) == baseline
    assert s3.client.meta.config.retries == shared_config
    assert ClosureVerifier(physical, object_format=service.object_format).verify({oid: 'commit'})
