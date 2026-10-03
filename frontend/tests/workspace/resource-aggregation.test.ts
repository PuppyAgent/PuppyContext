import { describe, expect, it, vi } from 'vitest';
import { createResourceDashboardApi, validateResourceDashboard, dashboardResourceKey, type ResourceDashboard } from '../../packages/cloud-core/src/endpoints/resourceDashboard';
import { createActivityApi } from '../../packages/cloud-core/src/endpoints/activity';
import { createProjectSession } from '@/features/workspace/session';

function transport() { return { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn() }; }
const summary = { resource_id: 'same', project_id: 'project/1', name: 'Same', path: '', status: 'active', direction: null, trigger: null, last_activity_at: null, created_at: null, error_message: null, usage_buckets: [] };
const data: ResourceDashboard = {
  project: { id: 'project/1', name: 'Project', description: null }, nodes: { total: 0, files: 0, folders: 0 }, tools: [], uploads: [],
  resources: [
    { ...summary, resource_kind: 'synchronize', provider: 'url' },
    { ...summary, resource_kind: 'access', kind: 'mcp', target: { kind: 'project_root', project_id: 'project/1' }, scope_mode: null },
  ],
};

describe('domain-qualified aggregate resource contracts', () => {
  it('accepts ID collisions across domains, but never legacy or duplicate inventory', async () => {
    const t = transport(); t.get.mockResolvedValue(data);
    const api = createResourceDashboardApi(t);
    expect((await api.getResourceDashboard('project/1')).resources.map(dashboardResourceKey)).toEqual(['synchronize:same', 'access:same']);
    expect(t.get).toHaveBeenCalledWith('/api/v1/projects/project%2F1/dashboard/resources');
    expect(() => validateResourceDashboard({ ...data, resources: [data.resources[0], data.resources[0]] }, 'project/1')).toThrow('duplicate');
    t.get.mockResolvedValue({ project: data.project, connections: [] });
    await expect(api.getResourceDashboard('project/1')).rejects.toThrow('inventory');
    expect(t.get).toHaveBeenCalledTimes(2);
  });

  it('rejects foreign, ambiguous and credential-bearing resource projections', () => {
    for (const row of [
      { ...data.resources[0], project_id: 'foreign' },
      { ...data.resources[0], resource_kind: 'connection' },
      { ...data.resources[0], access_key: 'metadata-must-not-authorize' },
      { ...data.resources[1], target: { kind: 'project_root', project_id: 'project/1', scope_id: 'ambiguous' } },
      { ...data.resources[1], target: { kind: 'scope', project_id: 'foreign', scope_id: 'scope-1' } },
    ]) expect(() => validateResourceDashboard({ ...data, resources: [row] } as ResourceDashboard, 'project/1')).toThrow();
  });

  it('keeps Activity own IDs and kind, preserves history, and rejects selectors/old kinds', async () => {
    const t = transport(); const api = createActivityApi(t);
    t.get.mockResolvedValue({ items: ['upload', 'import', 'synchronize_run'].map(kind => ({ id: 'same', kind, project_id: 'project/1', message: 'historical sync_run text' })), total: 3 });
    const result = await api.getProjectActivity('project/1', { activeOnly: false, limit: 7 });
    expect(result.items).toHaveLength(3);
    expect(result.items[2].message).toBe('historical sync_run text');
    expect(t.get).toHaveBeenCalledWith('/api/v1/activity/items?project_id=project%2F1&active_only=false&limit=7');
    await expect(api.getProjectActivity('project/1', { kind: 'sync_run' } as never)).rejects.toThrow('kind');
    await expect(api.getProjectActivity('project/1', { connection_id: 'old' } as never)).rejects.toThrow('selector');
    expect(t.get).toHaveBeenCalledTimes(1);
    t.get.mockResolvedValue({ items: [{ id: 'same', kind: 'sync_run', project_id: 'project/1' }], total: 1 });
    await expect(api.getProjectActivity('project/1')).rejects.toThrow('identity');
    t.get.mockRejectedValue(new Error('backend upgrade required'));
    await expect(api.getProjectActivity('project/1')).rejects.toThrow('upgrade');
    expect(t.get).toHaveBeenCalledTimes(3);
  });

  it('switches between bindings on the same path rather than toggling the previous binding closed', () => {
    const session = createProjectSession();
    session.getState().openPanel({ type: 'sync_config', nodeId: '', synchronizeBindingId: 'binding-1' });
    session.getState().togglePanel({ type: 'sync_config', nodeId: '', synchronizeBindingId: 'binding-2' });
    expect(session.getState().panel.synchronizeBindingId).toBe('binding-2');
    session.getState().togglePanel({ type: 'sync_config', nodeId: '', synchronizeBindingId: 'binding-2' });
    expect(session.getState().panel.type).toBe('none');
  });
});
