"""Real Node client -> localhost HTTP -> FastAPI/service/policy contract.

Persistence, authentication and job transport are the same isolated facts used
by the HTTP lifecycle suite. No production service or provider is contacted.
Set PUPPYONE_DESKTOP_SOURCE to a Desktop checkout to run its independent client.
"""
import os
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time

import pytest
import uvicorn

from tests.platform.test_synchronize_identity_http import environment as environment
from tests.platform.test_synchronize_public_api import canonical as canonical

ROOT = Path(__file__).resolve().parents[3]
NODE = shutil.which("node")


@pytest.fixture
def http_server(canonical):
    app, *_ = canonical
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started, "local contract server did not start"
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive(), "local contract server did not stop"


@pytest.mark.integration
@pytest.mark.parametrize("client", ["web-sdk", "desktop"])
def test_real_resource_client_http_round_trip(http_server, client):
    if not NODE:
        pytest.skip("Node with native TypeScript stripping is required")
    version = tuple(map(int, subprocess.check_output([NODE, "--version"], text=True).lstrip("v").split(".")[:2]))
    if not (version >= (23, 6) or version[0] == 22 and version[1] >= 18):
        pytest.skip("Native TypeScript stripping requires Node 22.18+ LTS or 23.6+")
    if client == "desktop":
        checkout = os.environ.get("PUPPYONE_DESKTOP_SOURCE")
        if not checkout:
            pytest.skip("Set PUPPYONE_DESKTOP_SOURCE for cross-repository client verification")
        source = Path(checkout) / "src/lib/cloud/synchronizeApi.ts"
    else:
        source = ROOT / "frontend/packages/cloud-core/src/endpoints/synchronize.ts"
    assert source.is_file()
    result = subprocess.run([NODE, "--input-type=module", "-", source.as_uri(), http_server, client],
        input=DRIVER, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "canonical HTTP round-trip passed" in result.stdout


DRIVER = r"""
import assert from 'node:assert/strict';
const [source, origin, kind] = process.argv.slice(2);
const { createSynchronizeApi } = await import(source);
const requests = [];
async function request(path, init = {}) {
  requests.push(path);
  const response = await fetch(origin + path, { ...init, headers: { 'Content-Type': 'application/json' } });
  if (!response.ok) throw new Error(`HTTP ${response.status}: ${await response.text()}`);
  const envelope = await response.json();
  if (envelope.code !== 0) throw new Error(envelope.message);
  return envelope.data;
}
const send = (method) => (path, body) => request(path, { method, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
const session = { user_id: 'user-1' };
const api = kind === 'desktop'
  ? createSynchronizeApi((path, _session, _changed, init) => request('/api/v1' + path, init))
  : createSynchronizeApi({ get: send('GET'), post: send('POST'), patch: send('PATCH'), put: send('PUT'), del: send('DELETE') });
const call = (method, ...args) => api[method](...(kind === 'desktop' ? [session, ...args] : args));
const created = await call('createSynchronizeBinding', {
  project_id: 'project-1', provider: 'url', target_path: 'contracts', sync_mode: 'manual',
  config: { source: { resource_url: 'https://example.test', metadata: { connection_id: 'keep-user-data' } }, options: {} },
});
assert.equal(created.binding.project_id, 'project-1');
assert.equal(created.execution_result.synchronize_binding_id, created.binding.id);
assert.ok(created.execution_result.synchronize_run_id);
const id = created.binding.id;
const rows = await call('listSynchronizeBindings', 'project-1');
assert.equal(rows[0].id, id);
assert.equal(rows[0].config.source.metadata.connection_id, 'keep-user-data');
assert.ok('last_synchronize_commit_id' in rows[0]);
assert.ok(!('last_sync_commit_id' in rows[0]));
await call('updateSynchronizeBinding', id, { target_path: 'updated' });
await call('updateSynchronizeTrigger', id, { sync_mode: 'manual', trigger: { type: 'manual' } });
const runs = await call('listSynchronizeRuns', id);
assert.equal(runs[0].synchronize_binding_id, id);
const run = await call('getSynchronizeRun', created.execution_result.synchronize_run_id);
assert.equal(run.synchronize_binding_id, id);
await call('refreshSynchronizeBinding', id);
await call('pauseSynchronizeBinding', id);
assert.equal((await call('listSynchronizeBindings', 'project-1'))[0].status, 'paused');
await call('resumeSynchronizeBinding', id);
await call('deleteSynchronizeBinding', id);
assert.deepEqual(await call('listSynchronizeBindings', 'project-1'), []);
await assert.rejects(() => call('refreshSynchronizeBinding', 'access-only'), /HTTP 404/);
assert.ok(requests.every(path => path.startsWith('/api/v1/synchronize/')));
console.log(`${kind}: canonical HTTP round-trip passed (${requests.length} real HTTP requests)`);
"""
