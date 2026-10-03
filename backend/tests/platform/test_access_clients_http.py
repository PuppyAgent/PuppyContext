"""Actual SDK -> localhost HTTP -> Access routers/service/policy.

Identity, persistence and credential issuance use isolated facts, not live
JWT/Provider/database services. This does not establish deployed acceptance.
"""

import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from tests.platform.test_access_public_api import environment as environment
from tests.platform.test_synchronize_clients_http import http_server as http_server

ROOT = Path(__file__).resolve().parents[3]
NODE = shutil.which("node")


@pytest.fixture
def canonical(environment, monkeypatch):
    # Reuse only the localhost server lifecycle, not Synchronize persistence.
    _, memory, *_ = environment

    def issue_mcp_key(surface_id):
        assert memory.get(surface_id).kind == "mcp"
        memory.issuances.append(surface_id)
        return {"id": surface_id, "api_key": "one-time-mcp-test-key"}

    monkeypatch.setattr(
        "src.platform.access.adapters.mcp_endpoint.repository.McpEndpointRepository",
        lambda: SimpleNamespace(regenerate_api_key=issue_mcp_key),
    )

    def unexpected_network(*args, **kwargs):
        raise AssertionError("The isolated contract server must not contact external services")

    # Only Node may make HTTP requests, and its origin is this localhost server.
    monkeypatch.setattr(httpx.Client, "request", unexpected_network)
    monkeypatch.setattr(httpx.AsyncClient, "request", unexpected_network)
    return environment


@pytest.mark.integration
@pytest.mark.parametrize("client", ["web-sdk", "desktop"])
def test_actual_access_sdk_http_round_trip(http_server, client):
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
            pytest.skip("Set PUPPYONE_DESKTOP_SOURCE for cross-repository client verification")
        source = Path(checkout) / "src/lib/cloud/accessSurfacesApi.ts"
    else:
        source = ROOT / "frontend/packages/cloud-core/src/endpoints/accessSurfaces.ts"
    assert source.is_file()
    result = subprocess.run(
        [NODE, "--input-type=module", "-", source.as_uri(), http_server, client],
        input=DRIVER,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "canonical Access HTTP round-trip passed" in result.stdout


DRIVER = r"""
import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { registerHooks } from 'node:module';
import { fileURLToPath } from 'node:url';
// Match the Web bundler's relative TS resolution without transforming client logic.
registerHooks({ resolve(specifier, context, nextResolve) {
  if (specifier.startsWith('.') && context.parentURL) {
    const candidate = new URL(specifier + '.ts', context.parentURL);
    if (existsSync(fileURLToPath(candidate))) return nextResolve(candidate.href, context);
  }
  return nextResolve(specifier, context);
} });
const [source, origin, client] = process.argv.slice(2);
const { createAccessSurfacesApi } = await import(source);
const requests = [];
const request = async (path, init = {}) => {
  requests.push([init.method ?? 'GET', path]);
  const response = await fetch(origin + path, {
    ...init, headers: { 'Content-Type': 'application/json', 'X-PuppyOne-Repository-Contract': '2' },
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}: ${await response.text()}`);
  const result = await response.json();
  if (result.code !== 0) throw new Error(result.message);
  return result.data;
};
const send = method => (path, body) => request(path, { method, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
const resourceApi = client === 'desktop'
  ? createAccessSurfacesApi((path, _session, _changed, init) => request('/api/v1' + path, init))
  : createAccessSurfacesApi({ get: send('GET'), post: send('POST'), patch: send('PATCH'), put: send('PUT'), del: send('DELETE') });
const api = client === 'desktop' ? new Proxy(resourceApi, {
  get(target, method) { return (...args) => target[method]({ user_id: 'user-1' }, ...args); },
}) : resourceApi;
const projectId = 'project-1';
const target = { kind: 'scope', project_id: projectId, scope_id: 'scope-1' };
const surface = await api.createAccessSurface(projectId, {
  target, kind: 'mcp', direction: 'outbound', name: 'Managed',
  config: { metadata: { connection_id: 'user-data' } },
});
assert.equal(surface.kind, 'mcp');
assert.deepEqual(surface.target, target);
assert.ok(!('provider' in surface));
assert.ok(!('last_run_id' in surface));
assert.ok('last_activity_at' in surface);
const id = surface.id;
assert.equal((await api.listAccessSurfaces(projectId))[0].id, id);
assert.deepEqual((await api.getAccessSurface(id)).config.metadata, { connection_id: 'user-data' });
await api.updateAccessSurface(projectId, id, { name: 'Edited', trigger: { type: 'manual' } });
assert.equal((await api.listAccessSurfaces(projectId))[0].name, 'Edited');
await api.pauseAccessSurface(projectId, id);
assert.equal((await api.listAccessSurfaces(projectId))[0].status, 'paused');
await api.resumeAccessSurface(projectId, id);
await api.renameAccessSurface(id, 'Renamed');
await api.updateAccessSurfaceMetadata(id, { config: { metadata: { connection_id: 'still-user-data' } } });
assert.equal((await api.getAccessSurface(id)).name, 'Renamed');
const issued = await api.regenerateAccessSurfaceKey(id);
assert.equal(issued.access_surface_id, id);
assert.equal(typeof issued.credential, 'string');
assert.ok(!(JSON.stringify(await api.getAccessSurface(id))).includes(issued.credential));
await api.deleteAccessSurface(projectId, id);
assert.deepEqual(await api.listAccessSurfaces(projectId), []);
await assert.rejects(() => api.getAccessSurface(id), /HTTP 404/);
await assert.rejects(() => api.pauseAccessSurface(projectId, 'not-an-access-surface'), /HTTP 404/);
const configured = await api.configureAccessSurface({ project_id: projectId, kind: 'mcp', name: 'Configured' });
assert.ok(configured.mcp_api_key);
assert.equal((await api.listAccessibleSurfaces({ project_id: projectId }))[0].id, configured.id);
assert.ok(!JSON.stringify(await api.getAccessSurface(configured.id)).includes(configured.mcp_api_key));
await api.deleteConfiguredAccessSurface(configured.id);
const enabled = await api.enableTargetAccess(projectId, { kind: 'project_root', project_id: projectId });
assert.deepEqual(enabled.map(row => row.kind).sort(), ['cli', 'git_remote']);
assert.ok(requests.every(([, path]) => path.startsWith('/api/v1/access/surfaces') || path.startsWith(`/api/v1/projects/${projectId}/access/surfaces`)));
assert.ok(requests.every(([, path]) => !path.includes('/run')));
console.log(`canonical Access HTTP round-trip passed (${requests.length} real requests)`);
"""
