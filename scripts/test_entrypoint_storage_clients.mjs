// Real shared/Desktop resource clients against the owned populated-upgrade API.
// Native Node TypeScript stripping; no HTTP, authorization or persistence doubles.
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
import path from 'node:path';

const { origin, project, binding, source, access, user, cloudRoot, desktopRoot } =
  JSON.parse(process.env.ENTRYPOINT_CLIENT_CONTEXT);
assert.equal(new URL(origin).hostname, '127.0.0.1');
const token = process.env.ENTRYPOINT_CLIENT_TOKEN;
assert.ok(token);
const load = file => import(pathToFileURL(file).href);
const shared = name => path.join(cloudRoot, 'frontend/packages/cloud-core/src/endpoints', name + '.ts');
const desktop = name => path.join(desktopRoot, 'src/lib/cloud', name + 'Api.ts');
const requests = [];
async function request(resource, init = {}) {
  assert.ok(resource.startsWith('/api/v1/'));
  requests.push(resource);
  const response = await fetch(origin + resource, { ...init, headers: {
    'Content-Type': 'application/json', 'X-PuppyOne-Repository-Contract': '2',
    ...init.headers, Authorization: `Bearer ${token}`,
  } });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const body = await response.json();
  assert.equal(body.code, 0);
  return body.data;
}
const send = method => (resource, body) => request(resource, {
  method, ...(body === undefined ? {} : { body: JSON.stringify(body) }),
});
const transport = { get: send('GET'), post: send('POST'), patch: send('PATCH'), put: send('PUT'), del: send('DELETE') };
const desktopTransport = (resource, _session, _changed, init) => request('/api/v1' + resource, init);
for (const kind of ['shared', 'desktop']) {
  const { createSynchronizeApi } = await load(kind === 'shared' ? shared('synchronize') : desktop('synchronize'));
  const { createResourceDashboardApi } = await load(kind === 'shared' ? shared('resourceDashboard') : desktop('resourceDashboard'));
  const t = kind === 'shared' ? transport : desktopTransport;
  const api = createSynchronizeApi(t);
  const session = { user_id: user };
  const call = (method, ...args) => api[method](...(kind === 'desktop' ? [session, ...args] : args));
  const rows = await call('listSynchronizeBindings', project);
  assert.deepEqual(new Set(rows.map(row => row.id)), new Set([binding, 'retained-history']));
  assert.ok(!JSON.stringify(rows).includes('opaque-private-history'));
  const history = await call('listSynchronizeRuns', binding);
  assert.ok(history.length > 0);
  assert.ok(history.every(row => row.synchronize_binding_id === binding));
  assert.equal((await call('getSynchronizeRun', history[0].id)).synchronize_binding_id, binding);
  await assert.rejects(() => call('refreshSynchronizeBinding', source), /HTTP 404/);
  await assert.rejects(() => call('resumeSynchronizeBinding', 'retained-history'), /HTTP 409/);
  await call('pauseSynchronizeBinding', binding);
  assert.equal((await call('listSynchronizeBindings', project)).find(row => row.id === binding).status, 'paused');
  await call('resumeSynchronizeBinding', binding);
  const dashboardApi = createResourceDashboardApi(t);
  const dashboard = await dashboardApi.getResourceDashboard(...(kind === 'desktop' ? [session, project] : [project]));
  assert.ok(dashboard.resources.some(row => row.resource_kind === 'access' && row.resource_id === access));
  assert.ok(!dashboard.resources.some(row => row.resource_kind === 'synchronize' && row.resource_id === source));
}
const { createImportDatabaseApi } = await load(shared('importDatabase'));
const database = createImportDatabaseApi(transport);
assert.deepEqual(new Set((await database.listImportDatabaseSources(project)).map(row => row.id)), new Set([source, 'explicit-dual-source']));
assert.equal((await database.getImportDatabaseSource(source)).project_id, project);
const { createSynchronizeGithubApi } = await load(shared('synchronizeGithub'));
const github = createSynchronizeGithubApi(transport);
assert.equal((await github.getSynchronizeGithubBinding(project)).id, 'github-history');
const logs = await github.listSynchronizeGithubLogs(project);
assert.deepEqual(new Set(logs.entries.map(row => row.direction)), new Set(['inbound', 'outbound']));
assert.ok(requests.every(resource => !/\/integrations\/|\/db-connector\//.test(resource)));
console.log(`Actual shared/Desktop client libraries passed ${requests.length} authenticated requests against final populated storage.`);
