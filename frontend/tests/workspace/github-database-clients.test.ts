import { describe, expect, it, vi } from 'vitest';
import { createSynchronizeGithubApi, synchronizeGithubWebhookUrl, type SynchronizeGithubBinding } from '../../packages/cloud-core/src/endpoints/synchronizeGithub';
import { createImportDatabaseApi, type ImportDatabaseSource } from '../../packages/cloud-core/src/endpoints/importDatabase';

function transport() { return { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn() }; }
const binding: SynchronizeGithubBinding = {
  id: 'github-binding-1', project_id: 'project/1', oauth_connection_id: 1,
  github_repo_owner: 'owner', github_repo_name: 'repo', default_branch: 'main',
  auto_pull: true, has_webhook_secret: true, last_pulled_sha: null, last_pulled_at: null,
  last_pushed_sha: null, last_pushed_at: null, created_at: 'now', updated_at: 'now',
};
const source: ImportDatabaseSource = { id: 'source/1', project_id: 'project/1', name: 'Database', provider: 'supabase', is_active: true, last_used_at: null, created_at: 'now' };
const result = { synchronize_github_binding_id: binding.id, direction: 'inbound' as const, status: 'success' as const, git_sha: 'sha', version_commit_id: 'commit', files_changed: 1 };

describe('GitHub Synchronize client', () => {
  it('uses canonical lifecycle/discovery/run/log URLs and separate OAuth identity', async () => {
    const t = transport(); const api = createSynchronizeGithubApi(t);
    const base = '/api/v1/projects/project%2F1/synchronize/github';
    t.get.mockResolvedValue(binding); t.post.mockResolvedValue(binding); t.patch.mockResolvedValue(binding);
    const create = { oauth_connection_id: 1, github_repo_owner: 'owner', github_repo_name: 'repo', auto_pull: true, webhook_secret: 'one-time-test-input' };
    expect(await api.createSynchronizeGithubBinding('project/1', create)).toEqual(binding);
    expect(t.post).toHaveBeenLastCalledWith(base + '/binding', create);
    await api.getSynchronizeGithubBinding('project/1');
    expect(t.get).toHaveBeenLastCalledWith(base + '/binding');
    await api.updateSynchronizeGithubBinding('project/1', { auto_pull: false });
    expect(t.patch).toHaveBeenLastCalledWith(base + '/binding', { auto_pull: false });
    t.get.mockResolvedValue({ repos: [] });
    await api.listSynchronizeGithubRepos('project/1', 7);
    expect(t.get).toHaveBeenLastCalledWith(base + '/repos?oauth_connection_id=7');
    t.get.mockResolvedValue({ repo_owner: 'a b', repo_name: 'r&x', branches: [] });
    await api.listSynchronizeGithubBranches('project/1', 7, 'a b', 'r&x');
    expect(t.get).toHaveBeenLastCalledWith(base + '/branches?oauth_connection_id=7&repo_owner=a+b&repo_name=r%26x');
    t.post.mockResolvedValue(result); await api.pullSynchronizeGithubBinding('project/1');
    expect(t.post).toHaveBeenLastCalledWith(base + '/pull', {});
    t.post.mockResolvedValue({ ...result, direction: 'outbound' }); await api.pushSynchronizeGithubBinding('project/1', { message: 'user connection_id text' });
    expect(t.post).toHaveBeenLastCalledWith(base + '/push', { message: 'user connection_id text' });
    t.get.mockResolvedValue({ synchronize_github_binding_id: binding.id, entries: [{ ...result, id: 'log-1', created_at: 'now' }], total: 1 });
    await api.listSynchronizeGithubLogs('project/1', { limit: 1, offset: 0 });
    expect(t.get).toHaveBeenLastCalledWith(base + '/logs?limit=1&offset=0');
    await api.deleteSynchronizeGithubBinding('project/1');
    expect(t.del).toHaveBeenLastCalledWith(base + '/binding');
    expect(synchronizeGithubWebhookUrl('https://api.example.test/')).toBe('https://api.example.test/api/v1/synchronize/github/webhook');
  });

  it('does not normalize aliases, foreign metadata, execution or log identities', async () => {
    const t = transport(); const api = createSynchronizeGithubApi(t);
    for (const row of [{ ...binding, project_id: 'foreign' }, { ...binding, auto_pull: undefined, auto_import: true }, { ...binding, webhook_secret: 'unexpected' }]) {
      t.get.mockResolvedValue(row);
      await expect(api.getSynchronizeGithubBinding('project/1')).rejects.toThrow('invalid binding');
    }
    t.post.mockResolvedValue({ ...result, direction: 'import' });
    await expect(api.pullSynchronizeGithubBinding('project/1')).rejects.toThrow('execution');
    t.get.mockResolvedValue({ synchronize_github_binding_id: 'foreign', entries: [{ ...result, id: 'log-1' }], total: 1 });
    await expect(api.listSynchronizeGithubLogs('project/1')).rejects.toThrow('inconsistent');
    t.get.mockResolvedValue({ synchronize_github_binding_id: binding.id, entries: [{ ...result, id: 'log-1' }, { ...result, id: 'log-1' }], total: 2 });
    await expect(api.listSynchronizeGithubLogs('project/1')).rejects.toThrow('duplicate');
  });

  it('rejects ambiguous selectors and never retries an old backend', async () => {
    const t = transport(); const api = createSynchronizeGithubApi(t);
    await expect(api.getSynchronizeGithubBinding(' ')).rejects.toThrow('explicit');
    await expect(api.listSynchronizeGithubLogs('project/1', { connection_id: 'legacy' } as never)).rejects.toThrow('Unknown');
    await expect(api.listSynchronizeGithubLogs('project/1', { limit: 0 })).rejects.toThrow('Invalid');
    expect(t.get).not.toHaveBeenCalled();
    t.get.mockRejectedValue(new Error('404 backend upgrade required'));
    await expect(api.getSynchronizeGithubBinding('project/1')).rejects.toThrow('404');
    expect(t.get).toHaveBeenCalledTimes(1);
  });
});

describe('Database Import source client', () => {
  it('keeps one-time source and save identities and preserves user table data', async () => {
    const t = transport(); const api = createImportDatabaseApi(t);
    const base = '/api/v1/imports/database/sources';
    const body = { name: 'Database', project_url: 'https://database.example.test', api_key: 'one-time-test-input' };
    t.post.mockResolvedValue({ source, database_info: {} });
    await api.createImportDatabaseSource('project/1', body);
    expect(t.post).toHaveBeenLastCalledWith(base + '?project_id=project%2F1', body);
    t.get.mockResolvedValue([source]);
    await api.listImportDatabaseSources('project/1');
    expect(t.get).toHaveBeenLastCalledWith(base + '?project_id=project%2F1');
    t.get.mockResolvedValue(source); await api.getImportDatabaseSource(source.id);
    expect(t.get).toHaveBeenLastCalledWith(base + '/source%2F1');
    t.get.mockResolvedValue([]); await api.listImportDatabaseTables(source.id);
    expect(t.get).toHaveBeenLastCalledWith(base + '/source%2F1/tables');
    const table = { columns: ['connection_id'], rows: [{ connection_id: 'user-value' }], row_count: 1, execution_time_ms: 1 };
    t.get.mockResolvedValue(table);
    expect(await api.previewImportDatabaseTable(source.id, 'a b&c', 7)).toEqual(table);
    expect(t.get).toHaveBeenLastCalledWith(base + '/source%2F1/tables/a%20b%26c/preview?limit=7');
    t.post.mockResolvedValue({ import_database_source_id: source.id, content_path: 'file.json', row_count: 1 });
    await api.saveImportDatabaseTable(source.id, 'project/1', { table: 'items', name: 'file' });
    expect(t.post).toHaveBeenLastCalledWith(base + '/source%2F1/save?project_id=project%2F1', { table: 'items', name: 'file' });
    await api.deleteImportDatabaseSource(source.id);
    expect(t.del).toHaveBeenLastCalledWith(base + '/source%2F1');
  });

  it('rejects wrong Project/source, duplicate rows, credentials and old envelopes', async () => {
    const t = transport(); const api = createImportDatabaseApi(t);
    for (const rows of [[{ ...source, project_id: 'foreign' }], [{ ...source, api_key: 'unexpected' }], [source, source]]) {
      t.get.mockResolvedValue(rows);
      await expect(api.listImportDatabaseSources('project/1')).rejects.toThrow();
    }
    t.get.mockResolvedValue({ ...source, id: 'other' });
    await expect(api.getImportDatabaseSource(source.id)).rejects.toThrow('identity');
    t.post.mockResolvedValue({ connection: source, database_info: {} });
    await expect(api.createImportDatabaseSource('project/1', { name: 'x', project_url: 'u', api_key: 'k' })).rejects.toThrow();
    t.post.mockResolvedValue({ import_database_source_id: 'another', content_path: 'file.json', row_count: 0 });
    await expect(api.saveImportDatabaseTable(source.id, 'project/1', { name: 'x', table: 'items' })).rejects.toThrow('another source');
  });

  it('never drops empty identity or falls back on errors', async () => {
    const t = transport(); const api = createImportDatabaseApi(t);
    await expect(api.listImportDatabaseSources('')).rejects.toThrow('explicit');
    await expect(api.deleteImportDatabaseSource('')).rejects.toThrow('explicit');
    expect(t.get).not.toHaveBeenCalled(); expect(t.del).not.toHaveBeenCalled();
    t.get.mockRejectedValue(new Error('403 forbidden'));
    await expect(api.getImportDatabaseSource(source.id)).rejects.toThrow('403');
    expect(t.get).toHaveBeenCalledTimes(1);
  });
});
