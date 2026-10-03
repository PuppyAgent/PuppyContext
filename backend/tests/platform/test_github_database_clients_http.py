"""Actual shared clients over localhost HTTP; provider/storage/auth facts are isolated."""

import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from tests.platform.test_github_public_api import (
    environment as github_environment,  # noqa: F401 -- pytest fixture registration
)
from tests.platform.test_import_database_public_api import (
    environment as database_environment,  # noqa: F401 -- pytest fixture registration
)
from tests.platform.test_synchronize_clients_http import http_server as http_server

ROOT = Path(__file__).resolve().parents[3]
NODE = shutil.which("node")


@pytest.fixture(params=["github", "database"])
def canonical(request, monkeypatch):
    value = request.getfixturevalue(f"{request.param}_environment")

    def unexpected_network(*args, **kwargs):
        raise AssertionError("Only the Node client may contact the localhost contract server")

    monkeypatch.setattr(httpx.Client, "request", unexpected_network)
    monkeypatch.setattr(httpx.AsyncClient, "request", unexpected_network)
    return (*value, request.param)


@pytest.mark.integration
def test_real_github_and_database_sdk_http(http_server, canonical):
    if not NODE:
        pytest.skip("Node with native TypeScript stripping is required")
    version = tuple(
        map(int, subprocess.check_output([NODE, "--version"], text=True).lstrip("v").split(".")[:2])
    )
    if not (version >= (23, 6) or (version[0] == 22 and version[1] >= 18)):
        pytest.skip("Native TypeScript stripping requires Node 22.18+ LTS or 23.6+")
    kind = canonical[-1]
    source = (
        ROOT
        / "frontend/packages/cloud-core/src/endpoints"
        / ("synchronizeGithub.ts" if kind == "github" else "importDatabase.ts")
    )
    result = subprocess.run(
        [NODE, "--input-type=module", "-", source.as_uri(), http_server, kind],
        input=DRIVER,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "canonical HTTP round-trip passed" in result.stdout


DRIVER = r"""
import assert from 'node:assert/strict';
const [source, origin, kind] = process.argv.slice(2);
const module = await import(source);
const requests = [];
async function request(path, init = {}) {
  requests.push(path);
  const response = await fetch(origin + path, { ...init, headers: { 'Content-Type': 'application/json' } });
  if (!response.ok) throw new Error(`HTTP ${response.status}: ${await response.text()}`);
  const value = await response.json();
  if (value.code !== 0) throw new Error(value.message);
  return value.data;
}
const send = method => (path, body) => request(path, { method, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
const t = { get: send('GET'), post: send('POST'), patch: send('PATCH'), put: send('PUT'), del: send('DELETE') };
const project = 'project-1';
if (kind === 'github') {
  const api = module.createSynchronizeGithubApi(t);
  assert.equal(await api.getSynchronizeGithubBinding(project), null);
  const row = await api.createSynchronizeGithubBinding(project, {
    oauth_connection_id: 1, github_repo_owner: 'owner', github_repo_name: 'repo', auto_pull: true, webhook_secret: 'test-secret',
  });
  assert.equal(row.project_id, project);
  assert.ok(row.has_webhook_secret && !('webhook_secret' in row));
  assert.equal((await api.getSynchronizeGithubBinding(project)).id, row.id);
  assert.equal((await api.updateSynchronizeGithubBinding(project, { default_branch: 'feature', auto_pull: false })).id, row.id);
  const pulled = await api.pullSynchronizeGithubBinding(project);
  const pushed = await api.pushSynchronizeGithubBinding(project, { message: 'user connection_id content' });
  assert.equal(pulled.synchronize_github_binding_id, row.id);
  assert.equal(pulled.direction, 'inbound');
  assert.equal(pushed.synchronize_github_binding_id, row.id);
  assert.equal(pushed.direction, 'outbound');
  const logs = await api.listSynchronizeGithubLogs(project, { limit: 1, offset: 1 });
  assert.equal(logs.total, 2);
  assert.equal(logs.synchronize_github_binding_id, row.id);
  assert.equal(logs.entries[0].synchronize_github_binding_id, row.id);
  assert.equal(logs.entries[0].direction, 'outbound');
  await api.deleteSynchronizeGithubBinding(project);
  assert.equal(await api.getSynchronizeGithubBinding(project), null);
  await assert.rejects(() => api.pullSynchronizeGithubBinding(project), /HTTP 404/);
  assert.ok(requests.every(path => path.startsWith(`/api/v1/projects/${project}/synchronize/github/`)));
} else {
  const api = module.createImportDatabaseApi(t);
  const created = await api.createImportDatabaseSource(project, { name: 'Database', project_url: 'https://database.example.test', api_key: 'test-secret' });
  const row = created.source;
  assert.equal(row.project_id, project);
  assert.ok(!('config' in row));
  assert.equal((await api.listImportDatabaseSources(project))[0].id, row.id);
  assert.equal((await api.getImportDatabaseSource(row.id)).id, row.id);
  assert.equal((await api.listImportDatabaseTables(row.id))[0].name, 'items');
  assert.equal((await api.previewImportDatabaseTable(row.id, 'items', 7)).rows[0].connection_id, 'user-data');
  const saved = await api.saveImportDatabaseTable(row.id, project, { name: 'items', table: 'items' });
  assert.equal(saved.import_database_source_id, row.id);
  assert.equal(saved.content_path, 'items.json');
  await assert.rejects(() => api.saveImportDatabaseTable(row.id, 'project-2', { name: 'items', table: 'items' }), /HTTP 404/);
  await api.deleteImportDatabaseSource(row.id);
  assert.deepEqual(await api.listImportDatabaseSources(project), []);
  await assert.rejects(() => api.getImportDatabaseSource(row.id), /HTTP 404/);
  for (const id of ['access-only', 'synchronize-only']) {
    await assert.rejects(() => api.getImportDatabaseSource(id), /HTTP 404/);
  }
  assert.ok(requests.every(path => path.startsWith('/api/v1/imports/database/sources')));
}
console.log(`${kind} canonical HTTP round-trip passed (${requests.length} real requests)`);
"""
