"""Real Desktop/shared read clients over HTTP, with isolated storage/identity."""

import os
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from tests.platform.test_activity_public_api import (
    environment as activity_environment,  # noqa: F401 -- pytest fixture registration
)
from tests.platform.test_resource_dashboard import (
    environment as dashboard_environment,  # noqa: F401 -- pytest fixture registration
)
from tests.platform.test_synchronize_clients_http import http_server as http_server

ROOT = Path(__file__).resolve().parents[3]
NODE = shutil.which("node")


@pytest.fixture(params=["dashboard-sdk", "dashboard-desktop", "activity-sdk"])
def canonical(request, monkeypatch):
    value = request.getfixturevalue(request.param.split("-")[0] + "_environment")

    def unexpected_network(*args, **kwargs):
        raise AssertionError("Only Node may contact the localhost contract server")

    monkeypatch.setattr(httpx.Client, "request", unexpected_network)
    monkeypatch.setattr(httpx.AsyncClient, "request", unexpected_network)
    return (*value, request.param)


@pytest.mark.integration
def test_real_aggregate_clients_http(http_server, canonical):
    if not NODE:
        pytest.skip("Node with native TypeScript stripping is required")
    version = tuple(
        map(int, subprocess.check_output([NODE, "--version"], text=True).lstrip("v").split(".")[:2])
    )
    if not (version >= (23, 6) or (version[0] == 22 and version[1] >= 18)):
        pytest.skip("Native TypeScript stripping requires Node 22.18+ LTS or 23.6+")
    kind = canonical[-1]
    if kind == "dashboard-desktop":
        checkout = os.environ.get("PUPPYONE_DESKTOP_SOURCE")
        if not checkout:
            pytest.skip("Set PUPPYONE_DESKTOP_SOURCE to verify Desktop")
        source = Path(checkout) / "src/lib/cloud/resourceDashboardApi.ts"
    else:
        source = (
            ROOT
            / "frontend/packages/cloud-core/src/endpoints"
            / ("activity.ts" if kind == "activity-sdk" else "resourceDashboard.ts")
        )
    result = subprocess.run(
        [NODE, "--input-type=module", "-", source.as_uri(), http_server, kind],
        input=DRIVER,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "canonical aggregate HTTP passed" in result.stdout


DRIVER = r"""
import assert from 'node:assert/strict';
const [source, origin, kind] = process.argv.slice(2);
const module = await import(source);
const requests = [];
async function request(path, init = {}) {
  requests.push(path);
  const response = await fetch(origin + path, init);
  if (!response.ok) throw new Error(`HTTP ${response.status}: ${await response.text()}`);
  const value = await response.json();
  if (value.code !== 0) throw new Error(value.message);
  return value.data;
}
const t = { get: request };
if (kind === 'activity-sdk') {
  const api = module.createActivityApi(t);
  const result = await api.getProjectActivity('project-1');
  assert.deepEqual(result.items.map(row => row.kind), ['upload', 'import', 'synchronize_run']);
  assert.ok(result.items.every(row => row.id === 'same-id' && row.message === 'historical sync_run connection_id text'));
  await assert.rejects(() => api.getProjectActivity('foreign'), /HTTP 404/);
  assert.ok(requests.every(path => path.startsWith('/api/v1/activity/items?')));
} else {
  const api = kind === 'dashboard-desktop'
    ? module.createResourceDashboardApi((path, _session, _changed, init) => request('/api/v1' + path, init))
    : module.createResourceDashboardApi(t);
  const read = project => api.getResourceDashboard(...(kind === 'dashboard-desktop' ? [{ user_id: 'user-1' }, project] : [project]));
  const data = await read('project-1');
  assert.ok(!('connections' in data));
  assert.deepEqual(data.resources.map(row => [row.resource_kind, row.resource_id]), [['synchronize', 'same'], ['access', 'same'], ['access', 'mcp']]);
  assert.equal(data.resources[0].usage_buckets.at(-1), 1);
  assert.equal(data.resources[1].usage_buckets.at(-1), 2);
  assert.equal(data.resources[1].target.kind, 'project_root');
  assert.equal(data.resources[2].target.scope_id, 'scope-1');
  assert.ok(!JSON.stringify(data).includes('must-not-leak'));
  assert.deepEqual((await read('project-1')).resources, data.resources);
  await assert.rejects(() => read('foreign'), /HTTP 404/);
  assert.ok(requests.every(path => path.endsWith('/dashboard/resources')));
}
console.log(`${kind} canonical aggregate HTTP passed (${requests.length} real requests)`);
"""
