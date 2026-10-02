import { describe, expect, it, vi } from 'vitest';
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { createAccessSurfacesApi, type AccessSurface, type CloudTransport } from '../../packages/cloud-core/src';

const target = { kind: 'project_root' as const, project_id: 'p/1' };
function surface(overrides: Partial<AccessSurface> = {}): AccessSurface {
  return { id: 'a/1', project_id: 'p/1', target, kind: 'mcp', name: 'Access', direction: 'bidirectional',
    config: {}, policy: {}, oauth_connection_id: null, trigger: { type: 'manual' }, status: 'active',
    last_activity_at: null, error_message: null, created_by: null, created_at: null, updated_at: null, ...overrides };
}
function transport() { return { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), del: vi.fn() }; }

describe('canonical Access surface client', () => {
  it('uses encoded canonical URLs and preserves nested user metadata through the lifecycle', async () => {
    const t = transport();
    const row = surface();
    const api = createAccessSurfacesApi(t as CloudTransport);
    const body = { target, kind: row.kind, direction: 'bidirectional' as const,
      config: { metadata: { connection_id: 'user-owned', provider: 'a user label' } } };
    t.post.mockResolvedValue(row); t.patch.mockResolvedValue(row); t.get.mockResolvedValue([row]);
    expect(await api.createAccessSurface('p/1', body)).toEqual(row);
    expect(t.post).toHaveBeenCalledWith('/api/v1/projects/p%2F1/access/surfaces', body);
    expect(await api.listAccessSurfaces('p/1', { kind: 'mcp' })).toEqual([row]);
    expect(t.get).toHaveBeenCalledWith('/api/v1/projects/p%2F1/access/surfaces?kind=mcp');
    await api.updateAccessSurface('p/1', row.id, { name: 'Edited', config: body.config });
    expect(t.patch).toHaveBeenCalledWith('/api/v1/projects/p%2F1/access/surfaces/a%2F1', { name: 'Edited', config: body.config });
    await api.pauseAccessSurface('p/1', row.id); await api.resumeAccessSurface('p/1', row.id);
    await api.deleteAccessSurface('p/1', row.id);
    expect(t.post.mock.calls.slice(1).map(([url]) => url)).toEqual([
      '/api/v1/projects/p%2F1/access/surfaces/a%2F1/pause', '/api/v1/projects/p%2F1/access/surfaces/a%2F1/resume',
    ]);
    expect(t.del).toHaveBeenCalledWith('/api/v1/projects/p%2F1/access/surfaces/a%2F1');
    expect(Object.keys(api).some(name => /run|sync/i.test(name))).toBe(false);
  });

  it('uses the same identity for global metadata, configuration and one-time credential issuance', async () => {
    const t = transport(); const api = createAccessSurfacesApi(t as CloudTransport); const row = surface();
    t.post.mockResolvedValueOnce(row).mockResolvedValueOnce({ access_surface_id: row.id, credential: 'once', target });
    t.get.mockResolvedValueOnce(row).mockResolvedValueOnce([row]); t.patch.mockResolvedValue(row);
    await api.configureAccessSurface({ project_id: row.project_id, kind: 'mcp', path: 'folder' });
    expect(t.post).toHaveBeenCalledWith('/api/v1/access/surfaces', { project_id: 'p/1', kind: 'mcp', path: 'folder' });
    expect(await api.getAccessSurface(row.id)).toEqual(row);
    expect(await api.listAccessibleSurfaces({ project_id: 'p/1' })).toEqual([row]);
    await api.renameAccessSurface(row.id, 'Changed');
    await api.updateAccessSurfaceMetadata(row.id, { status: 'paused' });
    expect(await api.regenerateAccessSurfaceKey(row.id)).toEqual({ access_surface_id: row.id, credential: 'once', target });
    await api.deleteConfiguredAccessSurface(row.id);
    expect(t.post).toHaveBeenLastCalledWith('/api/v1/access/surfaces/a%2F1/regenerate-key', {});
    expect(t.del).toHaveBeenCalledWith('/api/v1/access/surfaces/a%2F1');
  });

  it('enables only the requested target and activates an existing Agent surface', async () => {
    const t = transport(); const api = createAccessSurfacesApi(t as CloudTransport);
    const agent = surface({ kind: 'agent' });
    t.post.mockResolvedValueOnce([agent]).mockResolvedValueOnce(agent);
    expect(await api.enableTargetAccess('p/1', target)).toEqual([agent]);
    await api.activateAccessSurfaceAgent('p/1', agent.id);
    expect(t.post.mock.calls).toEqual([
      ['/api/v1/projects/p%2F1/access/surfaces/enable-target', { target }],
      ['/api/v1/projects/p%2F1/access/surfaces/a%2F1/activate-agent', {}],
    ]);
  });

  it.each([
    surface({ project_id: 'foreign' }), surface({ id: '' }),
    surface({ kind: undefined, provider: 'mcp' } as never),
    surface({ target: { kind: 'project_root', project_id: 'foreign' } }),
    surface({ target: { kind: 'scope', project_id: 'p/1', scope_id: '' } }),
    surface({ target: { ...target, scope_id: 'ambiguous' } } as never),
  ])('rejects malformed, legacy or foreign inventory identities without downgrading', async row => {
    const t = transport(); t.get.mockResolvedValue([row]);
    await expect(createAccessSurfacesApi(t as CloudTransport).listAccessSurfaces('p/1')).rejects.toThrow();
    expect(t.get).toHaveBeenCalledTimes(1);
  });

  it('rejects duplicate identities, wrong target echoes and wrong credential owners', async () => {
    const t = transport(); const api = createAccessSurfacesApi(t as CloudTransport);
    t.get.mockResolvedValue([surface(), surface()]);
    await expect(api.listAccessSurfaces('p/1')).rejects.toThrow('duplicate');
    const foreignTarget = surface({ target: { kind: 'scope', project_id: 'p/1', scope_id: 'other' } });
    t.post.mockResolvedValueOnce(foreignTarget).mockResolvedValueOnce([foreignTarget])
      .mockResolvedValueOnce({ access_surface_id: 'another', credential: 'secret' });
    await expect(api.createAccessSurface('p/1', { target, kind: 'mcp', direction: 'inbound' })).rejects.toThrow('another repository target');
    await expect(api.enableTargetAccess('p/1', target)).rejects.toThrow('another repository target');
    await expect(api.regenerateAccessSurfaceKey('a/1')).rejects.toThrow('inconsistent surface');
  });

  it('never silently broadens invalid selectors or retries a legacy URL', async () => {
    const t = transport(); const api = createAccessSurfacesApi(t as CloudTransport);
    await expect(api.listAccessSurfaces('p/1', { provider: 'mcp' } as never)).rejects.toThrow('Unknown');
    await expect(api.listAccessibleSurfaces({ project_id: ' ' })).rejects.toThrow('empty');
    await expect(api.listAccessSurfaces('')).rejects.toThrow('explicit');
    await expect(api.createAccessSurface('foreign', { target, kind: 'mcp', direction: 'inbound' })).rejects.toThrow('foreign');
    expect(t.get).not.toHaveBeenCalled(); expect(t.post).not.toHaveBeenCalled();
    t.get.mockRejectedValue(new Error('404 canonical backend is required'));
    await expect(api.listAccessSurfaces('p/1')).rejects.toThrow('404');
    expect(t.get).toHaveBeenCalledExactlyOnceWith('/api/v1/projects/p%2F1/access/surfaces');
  });

  it('retires legacy resource DTOs, runs and generic transport URLs from consumers', () => {
    expect(existsSync('packages/cloud-core/src/endpoints/connectors.ts')).toBe(false);
    function scan(dir: string) {
      for (const entry of readdirSync(dir, { withFileTypes: true })) {
        const file = join(dir, entry.name);
        if (entry.isDirectory()) scan(file);
        else if (/\.(ts|tsx)$/.test(file)) {
          const source = readFileSync(file, 'utf8');
          expect(source, file).not.toMatch(/\/connectors(?:[/'"`?])/);
          expect(source, file).not.toMatch(/\/api\/v1\/access\/(?!surfaces(?:[/'"`?]|$))/);
          expect(source, file).not.toMatch(/\bConnectorRun\b|\bSyncEndpointInfo\b/);
        }
      }
    }
    for (const dir of ['app', 'components', 'contexts', 'features', 'lib', 'packages/cloud-core/src']) scan(dir);
    expect(readFileSync('features/access/hooks/useAccessData.ts', 'utf8')).not.toMatch(/mcpEndpointToConnector|scopeByPath|listMcpEndpoints/);
  });
});
