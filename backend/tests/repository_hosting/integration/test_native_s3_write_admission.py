"""S3/PG current-fact checks with an explicitly admitted fixture credential.

This does not authenticate an end-user Git HTTP request or enforce quota.
"""
from __future__ import annotations

import asyncio
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
from src.version_engine.infrastructure.supabase.ref_authority_repository import (
    AdmittedRefAuthorityRepository,
)
from src.version_engine.storage.backends.s3 import S3StorageBackend
from src.version_engine.storage.publication import ClosureVerifier
from src.version_engine.write_engine.git_object_format import encode_object
from src.version_engine.write_engine.ref_transaction import RefEdit, RefState, RefTransactionService
from tests.repository_hosting.harness.postgres import literal
from tests.repository_hosting.integration.test_repository_write_admission import seed_credential
from tests.repository_hosting.integration.test_s3_publication import (
    publication as publication_fixture,
)

pytestmark = [pytest.mark.hosting_s3, pytest.mark.asyncio]
publication = publication_fixture


@pytest.mark.parametrize('publication', ['sha1', 'sha256'], indirect=True)
@pytest.mark.parametrize('fault', ['credential', 'lease'])
async def test_publication_rechecks_current_facts_after_upload_and_replays_old_ack(publication, fault):
    pg, auth, s3, db, backend, original, git, oid, prepare = publication
    user = pg.value(f"SELECT created_by FROM public.projects WHERE id={literal(auth.project)}")
    org = pg.value(f"SELECT org_id FROM public.projects WHERE id={literal(auth.project)}")
    _surface, credential, _actor = seed_credential(SimpleNamespace(pg=pg, project=auth.project, org=org, user=user))
    target = ProjectRootTarget(auth.project)
    grant = RuntimeGrant(RuntimePrincipal(credential, 'git_http_token'), target,
                         ResolvedRepositoryView(target, '', (), 'rw'), RuntimeMode.READ_WRITE)
    control = AdmittedRefAuthorityRepository(db.client, lease_provider=active_project_write_lease)
    service = RefTransactionService(control, backend, project_id=auth.project, object_format=original.object_format)
    key = str(uuid.uuid4())
    first_args = dict(request_key=key, generation=1,
                      edits=[RefEdit(b'refs/heads/main', RefState(), RefState(oid=oid))],
                      roots={oid: 'commit'}, prepare=prepare)
    async with ProjectWriteLease(auth.project, 'native-test', repository=ProjectWriteLeaseRepository(db.client)):
        first = await asyncio.to_thread(service.submit, grant, **first_args)
        assert first['status'] == 'committed'
        before = control.snapshot(auth.project)
        new_oid = git.commit({'new.txt': b'new proposal\n'})
        objects = git.objects()
        lease = active_project_write_lease(auth.project)
        def prepare_then_invalidate():
            with backend.stage_object_writes() as batch:
                for object_id, (kind, body) in objects.items():
                    backend.put(object_id, encode_object(kind, body, object_format=service.object_format)[1])
                batch.flush()
            if fault == 'credential':
                pg.sql(f"UPDATE public.access_surface_credentials SET grant_mode='r' WHERE id={literal(credential)}")
            else:
                pg.sql(f"UPDATE public.project_write_leases SET expires_at=clock_timestamp()-interval '1 second' WHERE id={literal(lease.lease_id)}")
        with pytest.raises(Exception, match=r'repository_action_denied|repository_write_lease_unavailable'):
            await asyncio.to_thread(service.submit, grant, request_key=str(uuid.uuid4()), generation=1,
                                    edits=[RefEdit(b'refs/heads/main', RefState(oid=oid), RefState(oid=new_oid))],
                                    roots={new_oid: 'commit'}, prepare=prepare_then_invalidate)
        after = control.snapshot(auth.project)
        assert (after['refs'], after['ref_sequence']) == (before['refs'], before['ref_sequence'])
        fresh = S3StorageBackend(s3, auth.project, supabase=db)
        assert ClosureVerifier(fresh, object_format=service.object_format).verify({oid: 'commit'})
    # No active lease, and potentially a read-only credential. No new upload or
    # write may occur; the original request digest/result remains recoverable.
    assert active_project_write_lease(auth.project) is None
    first_args['prepare'] = lambda: pytest.fail('committed replay uploaded again')
    read_grant = RuntimeGrant(grant.principal, target, grant.repository_view, RuntimeMode.READ)
    assert await asyncio.to_thread(service.submit, read_grant, **first_args) == first
    with pytest.raises(PermissionError, match='read-only'):
        await asyncio.to_thread(service.submit, read_grant, **(first_args | {'request_key': str(uuid.uuid4())}))
    with pytest.raises(Exception, match='request_key_reused'):
        await asyncio.to_thread(service.submit, read_grant, **(first_args | {'message': 'changed request'}))
    assert auth.count('version_ref_transactions') == 1
