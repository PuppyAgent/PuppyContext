"""Physical mixed-authority reconciliation, with owned PG/S3 and explicit grants."""
import asyncio
import uuid
from types import SimpleNamespace

import pytest

from src.platform.project.write_lease import ProjectWriteLease, ProjectWriteLeaseRepository
from src.version_engine.infrastructure.supabase.billing_repository import RepositoryBilling
from src.version_engine.infrastructure.supabase.repo_manager import VersionRepoManager
from src.version_engine.storage.backends.s3 import S3StorageBackend
from src.version_engine.write_engine.git_object_format import encode_object
from src.version_engine.write_engine.ref_transaction import RefEdit, RefState, RefTransactionService
from tests.repository_hosting.harness.postgres import literal
from tests.repository_hosting.integration.test_native_s3_capacity import enroll
from tests.repository_hosting.integration.test_repository_logical_billing import (
    enroll_billing,
    value,
)
from tests.repository_hosting.integration.test_s3_publication import (
    publication as publication_fixture,
)

pytestmark = [pytest.mark.hosting_s3, pytest.mark.asyncio]
publication = publication_fixture


@pytest.mark.parametrize('publication', ['sha1', 'sha256'], indirect=True)
async def test_native_s3_reconciliation_cold_measurement_and_lost_ack(publication, monkeypatch):
    pg, auth, s3, db, backend, original, _git, oid, prepare = publication
    service, grant, org = enroll(publication)
    fixture = enroll_billing(SimpleNamespace(pg=pg, project=auth.project, org=org))
    pg.sql(f"UPDATE public.organization_entitlements SET entitlements='{{\"limits\":{{\"storage.max_bytes\":1000}}}}' WHERE org_id={literal(org)}")
    service = RefTransactionService(service.control, backend, project_id=auth.project,
                                    object_format=original.object_format, capacity=service.capacity,
                                    billing=RepositoryBilling(service.control))
    async with ProjectWriteLease(auth.project, 'usage-reconciliation', repository=ProjectWriteLeaseRepository(db.client)):
        result = await asyncio.to_thread(service.submit, grant, request_key=str(uuid.uuid4()), generation=1,
                                         edits=[RefEdit(b'refs/heads/main', RefState(), RefState(oid=oid))],
                                         roots={oid: 'commit'}, prepare=prepare)
    assert result['status'] == 'committed' and value(fixture) == 265
    # Legacy sibling uses its own SHA-1 tree, regardless of native object format.
    sibling = 'usage-legacy-'+uuid.uuid4().hex
    creator = pg.value(f'SELECT created_by FROM public.projects WHERE id={literal(auth.project)}')
    pg.sql(f"""BEGIN;
        INSERT INTO public.projects(id,name,org_id,created_by,lifecycle_status)
        VALUES({literal(sibling)},'usage-legacy',{literal(org)},{literal(creator)},'ready');
        INSERT INTO public.project_members(id,org_id,project_id,user_id,role,granted_by)
        VALUES({literal('pm-'+sibling)},{literal(org)},{literal(sibling)},{literal(creator)},'admin',{literal(creator)});
        COMMIT;
    """)
    legacy = S3StorageBackend(s3, sibling, supabase=db)
    blob, loose = encode_object('blob', b'legacy')
    await asyncio.to_thread(legacy.put, blob, loose)
    tree, loose = encode_object('tree', b'100644 a\0'+bytes.fromhex(blob)+b'100644 b\0'+bytes.fromhex(blob))
    await asyncio.to_thread(legacy.put, tree, loose)
    pg.sql(f"UPDATE public.projects SET version_root_hash={literal(tree)} WHERE id={literal(sibling)};"
           f"UPDATE public.organization_usage_counters SET value=99 WHERE org_id={literal(org)}")
    # Exercise the same backend-only factory selected by the scheduler. A job
    # actor must NOT bypass the admitted end-user reader's credential checks.
    reconciler = VersionRepoManager(s3, db).create_usage_reconciler()
    request = str(uuid.uuid4())
    real_call = reconciler.control.call

    def lose_ack(function, **parameters):
        result = real_call(function, **parameters)
        if function == 'finish_version_storage_reconciliation':
            raise TimeoutError('known completed reconciliation ACK lost')
        return result

    monkeypatch.setattr(reconciler.control, 'call', lose_ack)
    with pytest.raises(TimeoutError, match='ACK lost'):
        await asyncio.to_thread(reconciler.reconcile, org, request_key=request)
    assert value(fixture) == 277
    monkeypatch.setattr(reconciler.control, 'call', real_call)
    monkeypatch.setattr(reconciler, 'measure', lambda *args: pytest.fail('replay remeasured objects'))
    pg.sql(f"UPDATE public.organization_entitlements SET effective_until=clock_timestamp() WHERE org_id={literal(org)}")
    replay = await asyncio.to_thread(reconciler.reconcile, org, request_key=request)
    assert replay['value'] == 277 and value(fixture) == 277
