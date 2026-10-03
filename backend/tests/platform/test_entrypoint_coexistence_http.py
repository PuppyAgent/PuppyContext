"""ISSUE-059 coexistence matrix: actual clients, HTTP, services and policy.

Only persistence, authentication and external execution are isolated facts. This
is not Electron-window, deployed, live Provider or PostgreSQL acceptance.
"""

import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from src.platform.synchronize.public_router import router as synchronize_router
from tests.platform.test_access_public_api import (
    environment as access_environment,  # noqa: F401 -- fixture registration
)
from tests.platform.test_synchronize_clients_http import http_server as http_server
from tests.platform.test_synchronize_identity_http import (
    environment as synchronize_environment,  # noqa: F401 -- fixture registration
)

ROOT = Path(__file__).resolve().parents[3]
NODE = shutil.which("node")


@pytest.fixture(params=["access-only", "binding-only", "coexist-distinct", "coexist-collision"])
def canonical(request, monkeypatch):
    access_app, access_store, *_ = request.getfixturevalue("access_environment")
    app, bindings, runs, queue = request.getfixturevalue("synchronize_environment")
    app.include_router(synchronize_router, prefix="/api/v1")
    app.include_router(access_app.router)
    app.dependency_overrides.update(access_app.dependency_overrides)
    if request.param == "coexist-collision":
        insert = access_store.insert

        def colliding(*args, **kwargs):
            row = insert(*args, **kwargs)
            del access_store.items[row.id]
            row = replace(row, id="binding-1")
            access_store.items[row.id] = row
            return row

        monkeypatch.setattr(access_store, "insert", colliding)

    def unexpected_network(*args, **kwargs):
        raise AssertionError("Only Node may contact the localhost contract server")

    monkeypatch.setattr(httpx.Client, "request", unexpected_network)
    monkeypatch.setattr(httpx.AsyncClient, "request", unexpected_network)
    return app, request.param, access_store, bindings, runs, queue


@pytest.mark.integration
@pytest.mark.parametrize("client", ["web-sdk", "desktop"])
def test_actual_clients_keep_coexisting_resources_independent(http_server, canonical, client):
    if not NODE:
        pytest.skip("Node with native TypeScript stripping is required")
    version = tuple(
        map(int, subprocess.check_output([NODE, "--version"], text=True).lstrip("v").split(".")[:2])
    )
    if not (version >= (23, 6) or (version[0] == 22 and version[1] >= 18)):
        pytest.skip("Native TypeScript stripping requires Node 22.18+ LTS or 23.6+")
    if client == "desktop":
        checkout = os.environ.get("PUPPYONE_DESKTOP_SOURCE")
        if not checkout:
            pytest.skip("Set PUPPYONE_DESKTOP_SOURCE to verify Desktop")
        source = Path(checkout) / "src/lib/cloud"
    else:
        source = ROOT / "frontend/packages/cloud-core/src/endpoints"
    access = source / ("accessSurfacesApi.ts" if client == "desktop" else "accessSurfaces.ts")
    sync = source / ("synchronizeApi.ts" if client == "desktop" else "synchronize.ts")
    result = subprocess.run(
        [
            NODE,
            "--input-type=module",
            "-",
            access.as_uri(),
            sync.as_uri(),
            http_server,
            client,
            canonical[1],
        ],
        input=DRIVER,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "coexistence matrix passed" in result.stdout
    # Refresh of an already queued run can deduplicate, but never dispatches an
    # additional job because an Access surface was paused/resumed/deleted.
    assert canonical[-1].enqueue_sync_run.await_count == (0 if canonical[1] == "access-only" else 1)


DRIVER = r"""
import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { registerHooks } from 'node:module';
import { fileURLToPath } from 'node:url';
registerHooks({ resolve(specifier, context, nextResolve) {
  if (specifier.startsWith('.') && context.parentURL) {
    const candidate = new URL(specifier + '.ts', context.parentURL);
    if (existsSync(fileURLToPath(candidate))) return nextResolve(candidate.href, context);
  }
  return nextResolve(specifier, context);
} });
const [accessSource, syncSource, origin, client, mode] = process.argv.slice(2);
const { createAccessSurfacesApi } = await import(accessSource);
const { createSynchronizeApi } = await import(syncSource);
const requests = [];
const request = async (path, init = {}) => {
  requests.push(path);
  const response = await fetch(origin + path, { ...init, headers: { 'Content-Type': 'application/json', 'X-PuppyOne-Repository-Contract': '2' } });
  if (!response.ok) throw new Error(`HTTP ${response.status}: ${await response.text()}`);
  const value = await response.json();
  if (value.code !== 0) throw new Error(value.message);
  return value.data;
};
const send = method => (path, body) => request(path, { method, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
const t = client === 'desktop' ? (path, _session, _changed, init) => request('/api/v1' + path, init)
  : { get: send('GET'), post: send('POST'), patch: send('PATCH'), put: send('PUT'), del: send('DELETE') };
function bind(factory) {
  const api = factory(t);
  return client !== 'desktop' ? api : Object.fromEntries(Object.entries(api).map(([name, method]) => [name, (...args) => method({ user_id: 'user-1' }, ...args)]));
}
let access = bind(createAccessSurfacesApi), sync = bind(createSynchronizeApi);
const project = 'project-1';
const makeAccess = () => access.createAccessSurface(project, { kind: 'mcp', name: 'Same name', direction: 'outbound', target: { kind: 'scope', project_id: project, scope_id: 'scope-1' } });
const created = mode === 'access-only' ? null : await sync.createSynchronizeBinding({ project_id: project, provider: 'url', target_path: 'docs', sync_mode: 'manual', config: { source: { resource_url: 'https://example.test', resource_name: 'Same name' }, options: {} } });
let surface = mode === 'binding-only' ? null : await makeAccess();
const binding = created?.binding;
// New client instances must rediscover server resources, not retain create echoes.
access = bind(createAccessSurfacesApi); sync = bind(createSynchronizeApi);
assert.equal((await access.listAccessSurfaces(project)).length, surface ? 1 : 0);
assert.equal((await sync.listSynchronizeBindings(project)).length, binding ? 1 : 0);
if (surface && !binding) {
  await assert.rejects(() => sync.pauseSynchronizeBinding(surface.id), /HTTP 404/);
  await assert.rejects(() => sync.refreshSynchronizeBinding(surface.id), /HTTP 404/);
  await assert.rejects(() => sync.deleteSynchronizeBinding(surface.id), /HTTP 404/);
  assert.equal((await access.getAccessSurface(surface.id)).status, 'active');
} else if (binding && !surface) {
  await assert.rejects(() => access.getAccessSurface(binding.id), /HTTP 404/);
  await assert.rejects(() => access.pauseAccessSurface(project, binding.id), /HTTP 404/);
  await assert.rejects(() => access.deleteAccessSurface(project, binding.id), /HTTP 404/);
  assert.equal((await sync.listSynchronizeBindings(project))[0].status, 'active');
} else {
  assert.equal(surface.id === binding.id, mode === 'coexist-collision');
  await access.pauseAccessSurface(project, surface.id);
  assert.equal((await sync.listSynchronizeBindings(project))[0].status, 'active');
  await sync.pauseSynchronizeBinding(binding.id);
  await access.resumeAccessSurface(project, surface.id);
  assert.equal((await sync.listSynchronizeBindings(project))[0].status, 'paused');
  const deduped = await sync.refreshSynchronizeBinding(binding.id);
  assert.equal(deduped.results[0].synchronize_binding_id, binding.id);
  assert.equal(deduped.results[0].deduped, true);
  assert.equal((await access.getAccessSurface(surface.id)).status, 'active');
  await sync.resumeSynchronizeBinding(binding.id);
  assert.equal((await sync.listSynchronizeRuns(binding.id))[0].synchronize_binding_id, binding.id);
  await access.deleteAccessSurface(project, surface.id);
  assert.equal((await sync.listSynchronizeBindings(project))[0].id, binding.id);
  surface = await makeAccess();
  await sync.deleteSynchronizeBinding(binding.id);
  assert.equal((await access.getAccessSurface(surface.id)).id, surface.id);
  assert.deepEqual(await sync.listSynchronizeBindings(project), []);
}
await assert.rejects(() => access.getAccessSurface('missing'), /HTTP 404/);
await assert.rejects(() => sync.refreshSynchronizeBinding('missing'), /HTTP 404/);
if (surface) await assert.rejects(() => access.pauseAccessSurface('foreign', surface.id), /HTTP 404/);
assert.ok(requests.every(path => path.startsWith('/api/v1/synchronize/') || path.startsWith('/api/v1/access/surfaces/') || /\/api\/v1\/projects\/[^/]+\/access\/surfaces/.test(path)));
console.log(`${client} ${mode} coexistence matrix passed (${requests.length} real requests)`);
"""
