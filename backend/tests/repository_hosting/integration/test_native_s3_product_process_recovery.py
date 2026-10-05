"""Real worker death and late remote PUT; not lost-input or paired-restore proof."""
from __future__ import annotations

import asyncio
import json
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from src.version_engine.storage.publication import ClosureVerifier
from src.version_engine.write_engine.git_object_format import decode_object, encode_object
from tests.repository_hosting.harness.git import Git
from tests.repository_hosting.harness.late_put import LatePut
from tests.repository_hosting.harness.postgres import literal
from tests.repository_hosting.integration.test_native_s3_product_attempts import (
    pending,
    prepared,
    read_bytes,
    setup,
)
from tests.repository_hosting.integration.test_repository_logical_billing import events, value
from tests.repository_hosting.integration.test_s3_publication import (
    publication as publication_fixture,
)

pytestmark = [pytest.mark.hosting_s3, pytest.mark.asyncio]
publication = publication_fixture
BACKEND = Path(__file__).resolve().parents[3]


def spawn(request):
    worker = subprocess.Popen([sys.executable, '-m', 'tests.repository_hosting.harness.product_worker'],
        cwd=BACKEND, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    worker.stdin.write(json.dumps(request))
    worker.stdin.close()
    worker.stdin = None
    return worker


def result(worker):
    stdout, stderr = worker.communicate(timeout=30)
    assert worker.returncode == 0, stderr
    records = [line.removeprefix('HOSTING_RESULT=') for line in stdout.splitlines() if line.startswith('HOSTING_RESULT=')]
    assert len(records) == 1, 'worker did not return exactly one result'
    record = json.loads(records[0])
    assert record['pid'] == worker.pid
    return record['result']


def footprint(pg, project):
    tables = ('version_ref_transactions', 'version_product_operations', 'version_product_publication_attempts',
              'version_object_pins', 'project_write_leases', 'version_repository_capacity_inflight',
              'version_repository_object_capacity')
    return {table: pg.value(f"SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY to_jsonb(r)::text),'[]') "
                           f"FROM public.{table} r WHERE project_id={literal(project)}") for table in tables}


def cold_objects(service, result, path):
    oid = result['product']['commit_oid']
    manifest = ClosureVerifier(service.backend, object_format=service.object_format).verify({oid: 'commit'})
    cold = Git.init(path, bare=True, format=service.object_format)
    for expected in manifest.objects:
        kind, body = decode_object(service.backend.get_durable(expected))
        assert cold.run('hash-object', '-w', '-t', kind, '--stdin', input=body).stdout.strip().decode() == expected
    cold.run('update-ref', 'refs/heads/main', oid)
    cold.run('fsck', '--full', '--strict')
    assert cold.run('show', 'main:file').stdout == b'data'


@pytest.mark.parametrize('publication', ['sha1', 'sha256'], indirect=True)
async def test_killed_product_process_late_put_preserves_new_ack(publication, tmp_path):
    pg, auth, s3, _db, grant, billing, manager, service, original_request = setup(publication)
    blob = encode_object('blob', b'data', object_format=service.object_format)[0]
    workers = []
    with LatePut(s3.endpoint_url, s3.bucket_name, auth.project, service.backend._key_for(blob)) as gate:
        # Supervisor retains original input. No claim of recovering a lost body,
        # end-user token authentication, or a production producer's handoff.
        request = {key: val for key, val in original_request.items() if key != 'splice'}
        request.update(project_id=auth.project, credential_id=grant.principal.principal_id, proxy=gate.url)
        old = spawn(request)
        workers.append(old)
        try:
            assert await asyncio.to_thread(gate.started.wait, 15), 'worker did not reach owned PUT gate'
            claims = pending(pg, auth.project)
            original = prepared(pg, auth.project)
            assert len(claims) == 1 and value(billing) == 0
            old_pin = claims[0]['pin_id']
            lease = pg.value(f"SELECT lease_id FROM public.version_publication_admissions WHERE pin_id={literal(old_pin)}")
            old.kill()
            await asyncio.to_thread(old.communicate, timeout=5)
            assert old.returncode == -signal.SIGKILL
            assert not gate.finished.is_set() and pending(pg, auth.project) == claims
            # Real wall-clock expiry, not owner SQL expiry or process death as
            # lease/I/O settlement. Physical child lease/claims are not removed.
            deadline = time.monotonic() + 35
            while pg.value(f"SELECT expires_at<=clock_timestamp() FROM public.project_write_leases WHERE id={literal(lease)}") != 't':
                assert time.monotonic() < deadline, 'killed worker lease did not expire'
                await asyncio.sleep(0.1)
            fresh = spawn(request)
            workers.append(fresh)
            acknowledged = await asyncio.to_thread(result, fresh)
            acknowledged_at = time.monotonic()
            assert fresh.pid != old.pid
            assert acknowledged['status'] == 'committed' and acknowledged['receipt_id'] != old_pin
            assert acknowledged['product']['commit_oid'] == original['proposal']['product_result']['commit_oid']
            assert pending(pg, auth.project) == claims and prepared(pg, auth.project) == original
            assert await asyncio.to_thread(read_bytes, manager.get_native_service(auth.project), grant) == b'data'
            assert not gate.finished.is_set()
            gate.release.set()
            assert await asyncio.to_thread(gate.finished.wait, 15), 'owned late PUT did not finish'
            assert gate.status == 200 and gate.forward_started >= acknowledged_at and gate.errors == []
            assert pending(pg, auth.project) == claims  # Remote completion is still not automatic settlement.
            assert value(billing) == 4 and events(billing) == 1
            await asyncio.to_thread(cold_objects, manager.get_native_service(auth.project), acknowledged, tmp_path / 'cold.git')
            pg.sql(f"UPDATE public.access_surface_credentials SET grant_mode='r' WHERE id={literal(grant.principal.principal_id)}")
            before = footprint(pg, auth.project)
            replay = spawn(request)
            workers.append(replay)
            assert await asyncio.to_thread(result, replay) == acknowledged
            assert footprint(pg, auth.project) == before
            pg.sql(f"UPDATE public.access_surface_credentials SET status='revoked' WHERE id={literal(grant.principal.principal_id)}")
            revoked = spawn(request)
            workers.append(revoked)
            _, error = await asyncio.to_thread(revoked.communicate, timeout=30)
            assert revoked.returncode and 'repository_action_denied' in error
            assert footprint(pg, auth.project) == before
        finally:
            gate.release.set()
            for worker in workers:
                if worker.poll() is None:
                    worker.kill()
                await asyncio.to_thread(worker.communicate, timeout=5)
