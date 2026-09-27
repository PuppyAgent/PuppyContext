import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import crypto from 'node:crypto';

const api = process.env.INSTALL_API;
const auth = process.env.INSTALL_AUTH;
const web = process.env.INSTALL_WEB;
const mail = process.env.INSTALL_MAIL;
const key = process.env.INSTALL_ANON_KEY;
const statePath = process.env.INSTALL_STATE;
for (const target of [api, auth, web, mail]) {
  if (!target || new URL(target).hostname !== '127.0.0.1') throw new Error('Installation tests only target an isolated loopback stack');
}

async function success(response) {
  expect(response.status(), new URL(response.url()).pathname).toBeLessThan(300);
  expect(response.status()).toBeGreaterThanOrEqual(200);
  return response.json();
}

async function signup(request, email, password) {
  const registered = await success(await request.post(`${auth}/auth/v1/signup`, {
    headers: { apikey: key }, data: { email, password },
  }));
  // CI enables confirmation. Receiving mail is part of the real Auth path.
  expect(registered.access_token).toBeUndefined();
  let messageId;
  await expect.poll(async () => {
    const messages = await success(await request.get(`${mail}/api/v1/messages`));
    messageId = messages.messages.find(m => m.To.some(t => t.Address === email))?.ID;
    return Boolean(messageId);
  }, { timeout: 30_000 }).toBe(true);
  const message = await success(await request.get(`${mail}/api/v1/message/${messageId}`));
  const confirmation = message.HTML.match(/href="([^\"]*\/verify[^\"]*)"/)?.[1]?.replaceAll('&amp;', '&');
  expect(confirmation).toBeTruthy();
  expect(new URL(confirmation).origin).toBe(auth);
  const confirmed = await request.get(confirmation, { maxRedirects: 0 });
  expect([302, 303]).toContain(confirmed.status());
  const session = await success(await request.post(`${auth}/auth/v1/token?grant_type=password`, {
    headers: { apikey: key }, data: { email, password },
  }));
  expect(session.access_token).toBeTruthy();
  const claims = JSON.parse(Buffer.from(session.access_token.split('.')[1], 'base64url').toString());
  expect(claims).toMatchObject({
    iss: `${auth}/auth/v1`, aud: 'authenticated', role: 'authenticated',
  });
  return session;
}

test('fresh installation and restart preserve real authenticated file operations', async ({ request, page }) => {
  let state;
  if (process.env.INSTALL_PHASE === 'seed') {
    const email = `install-${crypto.randomUUID()}@example.com`;
    const password = `Install-${crypto.randomUUID()}!`;
    const session = await signup(request, email, password);
    const headers = { Authorization: `Bearer ${session.access_token}` };
    const initialized = await success(await request.post(`${api}/api/v1/auth/initialize`, { headers }));
    const project = await success(await request.post(`${api}/api/v1/projects/`, {
      headers: { ...headers, 'Idempotency-Key': crypto.randomUUID() },
      data: { name: 'Fresh installation test', org_id: initialized.data.org_id },
    }));
    const projectId = project.data.id;
    const content = `Persistent file ${crypto.randomUUID()}\n中文内容\n`;
    const written = await success(await request.post(`${api}/api/v1/content/${projectId}/write`, {
      headers, data: { path: 'install-check.md', content, node_type: 'markdown' },
    }));
    expect(written.data.commit_id).toBeTruthy();
    const outsider = await signup(request, `outsider-${crypto.randomUUID()}@example.com`, password);
    const forbidden = await request.get(`${api}/api/v1/content/${projectId}/cat?path=install-check.md`, {
      headers: { Authorization: `Bearer ${outsider.access_token}` },
    });
    expect([403, 404]).toContain(forbidden.status());
    state = { email, password, projectId, content, session, outsider };
    fs.writeFileSync(statePath, JSON.stringify(state), { mode: 0o600 });
  } else {
    state = JSON.parse(fs.readFileSync(statePath, 'utf8'));
    const refreshed = await success(await request.post(`${api}/api/v1/auth/refresh`, {
      data: { refresh_token: state.session.refresh_token },
    }));
    expect(refreshed.data.access_token).toBeTruthy();
    state.session = refreshed.data;
  }
  const headers = { Authorization: `Bearer ${state.session.access_token}` };
  const projects = await success(await request.get(`${api}/api/v1/projects/`, { headers }));
  expect(projects.data.some(project => project.id === state.projectId)).toBe(true);
  const read = await success(await request.get(`${api}/api/v1/content/${state.projectId}/cat?path=install-check.md`, { headers }));
  expect(read.data.content_text).toBe(state.content);
  const raw = await request.get(`${api}/api/v1/content/${state.projectId}/raw?path=install-check.md`, { headers });
  expect(raw.status()).toBe(200);
  expect(await raw.text()).toBe(state.content);
  const anonymous = await request.get(`${api}/api/v1/content/${state.projectId}/cat?path=install-check.md`);
  expect([401, 403]).toContain(anonymous.status());
  const forbidden = await request.get(`${api}/api/v1/content/${state.projectId}/cat?path=install-check.md`, {
    headers: { Authorization: `Bearer ${state.outsider.access_token}` },
  });
  expect([403, 404]).toContain(forbidden.status());

  const destination = `/projects/${state.projectId}/data`;
  if (process.env.INSTALL_PHASE === 'verify') {
    // A previously signed-in user must retain their session and workspace.
    await page.context().addCookies(state.browserCookies);
    await page.goto(`${web}${destination}`);
    await expect(page).toHaveURL(`${web}${destination}`);
    await expect(page.getByText('install-check.md', { exact: true }).first()).toBeVisible({ timeout: 30_000 });
    await page.context().clearCookies();
  }
  // Also exercise fresh sign-in against the upgraded frontend.
  await page.goto(`${web}/login?next=${encodeURIComponent(destination)}`);
  await page.getByPlaceholder('Your email address').fill(state.email);
  const checkedEmail = page.waitForResponse(response =>
    new URL(response.url()).pathname.endsWith('/auth/check-email') && response.request().method() === 'POST');
  await page.getByRole('button', { name: 'Continue', exact: true }).click();
  await success(await checkedEmail);
  await page.getByPlaceholder('Enter your password').fill(state.password);
  await page.getByRole('button', { name: 'Sign In', exact: true }).click();
  await expect(page).toHaveURL(`${web}${destination}`, { timeout: 60_000 });
  await expect(page.getByText('install-check.md', { exact: true }).first()).toBeVisible({ timeout: 30_000 });
  await page.reload();
  await expect(page).toHaveURL(`${web}${destination}`);
  await expect(page.getByText('install-check.md', { exact: true }).first()).toBeVisible({ timeout: 30_000 });
  if (process.env.INSTALL_PHASE === 'seed') {
    state.browserCookies = await page.context().cookies();
    fs.writeFileSync(statePath, JSON.stringify(state), { mode: 0o600 });
  }
  if (process.env.INSTALL_PHASE === 'verify') {
    const changed = `${state.content}Updated after upgrade\n`;
    await success(await request.post(`${api}/api/v1/content/${state.projectId}/write`, {
      headers, data: { path: 'install-check.md', content: changed, node_type: 'markdown' },
    }));
    const reread = await request.get(`${api}/api/v1/content/${state.projectId}/raw?path=install-check.md`, { headers });
    expect(reread.status()).toBe(200);
    expect(await reread.text()).toBe(changed);
    await success(await request.post(`${api}/api/v1/content/${state.projectId}/rm`, {
      headers, data: { path: 'install-check.md' },
    }));
    expect((await request.get(`${api}/api/v1/content/${state.projectId}/cat?path=install-check.md`, { headers })).status()).toBe(404);
    await success(await request.delete(`${api}/api/v1/projects/${state.projectId}`, { headers }));
    await success(await request.post(`${api}/api/v1/auth/logout`, {
      headers, data: { refresh_token: state.session.refresh_token },
    }));
  }
});

test('upgraded Supabase REST supports old and new storage clients', async ({ request }) => {
  test.skip(process.env.INSTALL_UPGRADED !== 'true' || process.env.INSTALL_PHASE !== 'verify');
  const serviceKey = process.env.INSTALL_SERVICE_ROLE_KEY;
  const headers = { apikey: serviceKey, Authorization: `Bearer ${serviceKey}`, Prefer: 'return=representation' };
  const row = async (table, id) => (await success(await request.get(`${auth}/rest/v1/${table}?id=eq.${id}`, { headers })))[0];
  const task = await row('search_index_tasks', 'issue049-tool-1');
  expect(task.config.extension).toBe('preserved');
  expect(task.result.extension).toBe('preserved');
  expect(task.created_by).toBe('00000000-0000-4000-8000-000000000049');
  await success(await request.patch(`${auth}/rest/v1/uploads?id=eq.issue049-tool-1`, { headers, data: { progress: 41 } }));
  expect((await row('search_index_tasks', 'issue049-tool-1')).progress).toBe(41);
  await success(await request.patch(`${auth}/rest/v1/search_index_tasks?id=eq.issue049-tool-1`, { headers, data: { progress: 53 } }));
  expect((await row('uploads', 'issue049-tool-1')).progress).toBe(53);
  expect((await row('uploads', 'issue049-upload-1')).config.sentinel).toBe('keep');
  await success(await request.patch(`${auth}/rest/v1/github_integrations?id=eq.issue049-binding`, { headers, data: { last_imported_sha: 'c'.repeat(40) } }));
  expect((await row('github_sync_bindings', 'issue049-binding')).last_imported_sha).toBe('c'.repeat(40));
  for (const column of ['access_point_id', 'access_surface_id']) {
    const binding = await success(await request.post(`${auth}/rest/v1/access_tools?on_conflict=${column},tool_id`, {
      headers: { ...headers, Prefer: 'resolution=merge-duplicates,return=representation' },
      data: { [column]: 'issue049-surface', tool_id: 'issue049-tool-1', enabled: true },
    }));
    expect(binding[0].access_surface_id).toBe('issue049-surface');
    expect(binding[0].access_point_id).toBe('issue049-surface');
  }
  for (const table of ['access_tools', 'github_integrations', 'github_sync_bindings', 'search_index_tasks']) {
    const rejected = await request.get(`${auth}/rest/v1/${table}`, { headers: { apikey: key, Authorization: `Bearer ${key}` } });
    expect([401, 403]).toContain(rejected.status());
  }
});
