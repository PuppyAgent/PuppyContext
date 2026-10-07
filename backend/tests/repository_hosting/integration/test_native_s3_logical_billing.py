"""Real objects + checked SQL billing; explicit grant and synthetic entitlement."""
import asyncio
import uuid
from types import SimpleNamespace

import pytest

from src.platform.authorization.models import RuntimeGrant, RuntimeMode
from src.platform.project.write_lease import ProjectWriteLease, ProjectWriteLeaseRepository
from src.version_engine.infrastructure.supabase.billing_repository import RepositoryBilling
from src.version_engine.storage.backends.s3 import S3StorageBackend
from src.version_engine.storage.publication import ClosureVerifier
from src.version_engine.write_engine.git_object_format import encode_object
from src.version_engine.write_engine.ref_transaction import RefEdit, RefState, RefTransactionService
from tests.repository_hosting.harness.postgres import literal
from tests.repository_hosting.integration.test_native_s3_capacity import enroll
from tests.repository_hosting.integration.test_repository_logical_billing import (
    enroll_billing,
    events,
    value,
)
from tests.repository_hosting.integration.test_s3_publication import (
    publication as publication_fixture,
)

pytestmark = [pytest.mark.hosting_s3, pytest.mark.asyncio]
publication = publication_fixture


@pytest.mark.parametrize('publication', ['sha1', 'sha256'], indirect=True)
async def test_native_s3_logical_billing_denial_recovery_and_read_only_replay(publication):
    pg, auth, s3, db, backend, _original, git, oid, prepare = publication
    capacity_service, grant, org = enroll(publication)
    fixture = enroll_billing(SimpleNamespace(pg=pg, project=auth.project, org=org))
    pg.sql(f"UPDATE public.organization_entitlements SET entitlements='{{\"limits\":{{\"storage.max_bytes\":500}}}}' WHERE org_id={literal(org)}")
    control = capacity_service.control
    service = RefTransactionService(control, backend, project_id=auth.project,
                                    object_format=capacity_service.object_format,
                                    capacity=capacity_service.capacity, billing=RepositoryBilling(control))
    first_args = dict(request_key=str(uuid.uuid4()), generation=1,
                      edits=[RefEdit(b'refs/heads/main', RefState(), RefState(oid=oid))],
                      roots={oid: 'commit'}, prepare=prepare)
    async with ProjectWriteLease(auth.project, 'native-billing', repository=ProjectWriteLeaseRepository(db.client)):
        first = await asyncio.to_thread(service.submit, grant, **first_args)
        assert first['status'] == 'committed' and value(fixture) == 265 and events(fixture) == 1
        original = control.snapshot(auth.project)
        tip = git.commit({'large': b'L' * 400})
        objects = git.objects()

        def upload():
            with backend.stage_object_writes() as batch:
                for object_id, (kind, body) in objects.items():
                    backend.put(object_id, encode_object(kind, body, object_format=service.object_format)[1])
                batch.flush()

        second_args = dict(request_key=str(uuid.uuid4()), generation=1,
                           edits=[RefEdit(b'refs/heads/main', RefState(oid=oid), RefState(oid=tip))],
                           roots={tip: 'commit'}, prepare=upload)
        with pytest.raises(Exception, match='storage_quota_exceeded'):
            await asyncio.to_thread(service.submit, grant, **second_args)
        unchanged = control.snapshot(auth.project)
        assert (unchanged['refs'], unchanged['ref_sequence']) == (original['refs'], original['ref_sequence'])
        assert value(fixture) == 265 and events(fixture) == 1
        assert auth.count('version_ref_transactions') == auth.count('version_ref_events') == 1
        fresh = S3StorageBackend(s3, auth.project, supabase=db)
        assert ClosureVerifier(fresh, object_format=service.object_format).verify({oid: 'commit'})
        # Retry a sealed publication, using the new acknowledged projection.
        # The failed SQL transaction left neither a result nor a billing event.
        pg.sql(f"UPDATE public.organization_entitlements SET source_revision=2,entitlements='{{\"limits\":{{\"storage.max_bytes\":1000}}}}' WHERE org_id={literal(org)}")
        second_args['prepare'] = lambda: pytest.fail('sealed retry attempted storage mutation')
        second = await asyncio.to_thread(service.submit, grant, **second_args)
        assert second['status'] == 'committed' and value(fixture) == 665 and events(fixture) == 2
        assert auth.state()['oid'] == tip
        assert ClosureVerifier(fresh, object_format=service.object_format).verify({tip: 'commit'})
    pg.sql(f"UPDATE public.version_repository_billing SET initialized=false WHERE project_id={literal(auth.project)};"
           f"UPDATE public.organization_entitlements SET effective_until=clock_timestamp() WHERE org_id={literal(org)}")
    read_grant = RuntimeGrant(grant.principal, grant.target, grant.repository_view, RuntimeMode.READ)
    first_args['prepare'] = lambda: pytest.fail('original-result replay attempted storage mutation')
    assert await asyncio.to_thread(service.submit, read_grant, **first_args) == first
    assert value(fixture) == 665 and events(fixture) == 2
