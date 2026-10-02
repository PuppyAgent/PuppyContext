import { describe, expect, it, vi } from 'vitest';
import { createSynchronizeApi, type CloudTransport } from '../../packages/cloud-core/src';
import { readFileSync, existsSync, readdirSync } from 'node:fs';
import { join } from 'node:path';

function transport() {
  return { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), del: vi.fn() };
}

describe('canonical Synchronize client', () => {
  it('uses one canonical binding identity for create/list/edit/run/pause/delete', async () => {
    const t = transport();
    const binding = { id: 'binding/1', project_id: 'project/1' };
    t.post.mockResolvedValue({ binding, execution_result: { synchronize_binding_id: binding.id, synchronize_run_id: 'run' } });
    t.get.mockResolvedValue([binding]);
    const api = createSynchronizeApi(t as CloudTransport);
    const body = { project_id: 'project/1', provider: 'url', config: { source: { metadata: { connection_id: 'unchanged' } } } };
    await api.createSynchronizeBinding(body);
    expect(t.post).toHaveBeenCalledWith('/api/v1/synchronize/bindings', body);
    expect(await api.listSynchronizeBindings('project/1')).toEqual([binding]);
    expect(t.get).toHaveBeenCalledWith('/api/v1/synchronize/bindings?project_id=project%2F1');
    await api.updateSynchronizeBinding(binding.id, { target_path: 'destination' });
    await api.updateSynchronizeTrigger(binding.id, { sync_mode: 'manual' });
    await api.pauseSynchronizeBinding(binding.id);
    await api.resumeSynchronizeBinding(binding.id);
    await api.refreshSynchronizeBinding(binding.id);
    await api.deleteSynchronizeBinding(binding.id);
    expect(t.patch.mock.calls.map(([path]) => path)).toEqual([
      '/api/v1/synchronize/bindings/binding%2F1', '/api/v1/synchronize/bindings/binding%2F1/trigger',
    ]);
    expect(t.post.mock.calls.slice(1).map(([path]) => path)).toEqual([
      '/api/v1/synchronize/bindings/binding%2F1/pause', '/api/v1/synchronize/bindings/binding%2F1/resume',
      '/api/v1/synchronize/bindings/binding%2F1/refresh',
    ]);
    expect(t.del).toHaveBeenCalledWith('/api/v1/synchronize/bindings/binding%2F1');
  });

  it('rejects old, foreign or missing identities without legacy fallback', async () => {
    const t = transport();
    const api = createSynchronizeApi(t as CloudTransport);
    t.get.mockResolvedValueOnce([{ id: 'binding', project_id: 'foreign' }])
      .mockResolvedValueOnce([{ id: 'run', synchronize_binding_id: 'other-binding' }])
      .mockResolvedValueOnce({ id: 'run', access_point_id: 'legacy-id' });
    await expect(api.listSynchronizeBindings('project')).rejects.toThrow('another project');
    await expect(api.listSynchronizeRuns('binding')).rejects.toThrow('another binding');
    await expect(api.getSynchronizeRun('run')).rejects.toThrow('missing its binding ID');
    expect(() => api.deleteSynchronizeBinding('')).toThrow('binding ID');
    expect(t.get).toHaveBeenCalledTimes(3);
    expect(t.del).not.toHaveBeenCalled();
  });

  it('uses canonical provider, status, bootstrap, pull and push contracts', async () => {
    const t = transport();
    const api = createSynchronizeApi(t as CloudTransport);
    await api.listSynchronizeProviders();
    await api.listSynchronizeProviderResources('google_docs', { q: 'a b', cursor: 'c/2' });
    await api.getSynchronizeStatus('p/1');
    await api.listFailedSynchronizeRuns('p/1', 999);
    await api.bootstrapSynchronizeBindings({ project_id: 'p/1', provider: 'url', config: {} });
    await api.pullSynchronizeBindings({ synchronize_binding_id: 'b/1' });
    await api.pushSynchronizePath('p/1', 'folder/a b.md');
    expect(t.get.mock.calls.map(([path]) => path)).toEqual([
      '/api/v1/synchronize/providers',
      '/api/v1/synchronize/providers/google_docs/resources?q=a+b&cursor=c%2F2',
      '/api/v1/synchronize/status?project_id=p%2F1',
      '/api/v1/synchronize/failed-runs?project_id=p%2F1&limit=200',
    ]);
    expect(t.post.mock.calls.map(([path]) => path)).toEqual([
      '/api/v1/synchronize/bootstrap', '/api/v1/synchronize/pull?synchronize_binding_id=b%2F1',
      '/api/v1/synchronize/push/folder/a%20b.md?project_id=p%2F1',
    ]);
  });

  it('removes the duplicate legacy leaf clients and rejects reintroduced generic URLs', () => {
    expect(existsSync('lib/syncApi.ts')).toBe(false);
    expect(existsSync('lib/workflowApi.ts')).toBe(false);
    function scan(dir: string) {
      for (const entry of readdirSync(dir, { withFileTypes: true })) {
        const path = join(dir, entry.name);
        if (entry.isDirectory()) scan(path);
        else if (/\.(ts|tsx)$/.test(path)) {
          // GitHub's independently tracked webhook cutover is not the generic API.
          const source = readFileSync(path, 'utf8').replaceAll('/api/v1/integrations/github/webhook', 'GITHUB_WEBHOOK_PENDING_058');
          expect(source, path).not.toMatch(/\/api\/v1\/integrations(?:[/'"`?])/);
        }
      }
    }
    for (const dir of ['app', 'components', 'contexts', 'features', 'lib', 'packages/cloud-core/src']) scan(dir);
  });
});
