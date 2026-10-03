import type { CloudTransport } from '../transport';
import type { RepositoryTarget } from '../repositoryTargets';
import { BUILTIN_ACCESS_PROVIDER_IDS, getAccessProviderSortRank } from '../accessProviders';

export type AccessKind = 'git_remote' | 'cli' | 'agent' | 'mcp' | 'mcp_endpoint' | 'sandbox' | 'sandbox_endpoint';
export type AccessDirection = 'bidirectional' | 'inbound' | 'outbound';
export type AccessSurfaceStatus = 'active' | 'paused' | 'syncing' | 'error' | 'pending' | 'disabled';

export interface AccessSurface {
  id: string;
  project_id: string;
  target: RepositoryTarget;
  kind: AccessKind;
  name: string | null;
  direction: string | null;
  config: Record<string, unknown>;
  policy: Record<string, unknown>;
  oauth_connection_id: number | null;
  trigger: Record<string, unknown> | null;
  status: string;
  last_activity_at: string | null;
  error_message: string | null;
  created_by: string | null;
  created_at: string | null;
  updated_at: string | null;
  path?: string | null;
  node_name?: string | null;
  has_key?: boolean | null;
  key_last4?: string | null;
}

export interface AccessSurfaceCreate {
  target: RepositoryTarget;
  kind: AccessKind;
  direction: AccessDirection;
  name?: string;
  config?: Record<string, unknown>;
  policy?: Record<string, unknown>;
  oauth_connection_id?: number | null;
  trigger?: { type: 'manual' | 'scheduled' | 'on_change'; config?: Record<string, unknown> };
}
export type AccessSurfaceUpdate = Partial<Omit<AccessSurfaceCreate, 'target' | 'kind'>> & { status?: 'active' | 'paused' };
export interface AccessSurfaceConfigure {
  project_id: string;
  kind: 'agent' | 'mcp' | 'sandbox';
  name?: string;
  path?: string;
  config?: Record<string, unknown>;
  accesses?: Record<string, unknown>[];
  tools_config?: Record<string, unknown>[];
}
export interface AccessSurfaceCreated {
  id: string;
  project_id: string;
  kind: AccessKind;
  name: string | null;
  status: string;
  target?: RepositoryTarget | null;
  mcp_api_key?: string | null;
  mcp_server_url?: string | null;
}
export interface AccessCredentialIssued {
  access_surface_id: string;
  credential: string;
  target?: RepositoryTarget | null;
  credential_hint?: string | null;
}
export interface AccessSurfaceKind {
  kind: AccessKind;
  display_name: string;
  description: string;
  auth: string;
  creation_mode: string;
  category: 'access';
  icon: string;
}

export const ACCESS_SURFACE_KINDS: readonly AccessKind[] = ['git_remote', 'cli', 'agent', 'mcp', 'mcp_endpoint', 'sandbox', 'sandbox_endpoint'];
export const BUILTIN_ACCESS_KINDS = BUILTIN_ACCESS_PROVIDER_IDS;

function segment(value: string): string {
  if (typeof value !== 'string' || !value.trim()) throw new Error('An explicit Access resource ID is required');
  return encodeURIComponent(value);
}

function validateTarget(target: RepositoryTarget, projectId: string): void {
  if (!target || target.project_id !== projectId
    || (target.kind !== 'project_root' && target.kind !== 'scope')
    || (target.kind === 'scope' && (typeof target.scope_id !== 'string' || !target.scope_id.trim()))) {
    throw new Error('Access surface returned an invalid or foreign repository target');
  }
  const allowed = target.kind === 'scope' ? ['kind', 'project_id', 'scope_id'] : ['kind', 'project_id'];
  if (Object.keys(target).some(key => !allowed.includes(key))) throw new Error('Access target contains ambiguous fields');
}

function requireSameTarget(actual: RepositoryTarget, expected: RepositoryTarget): void {
  if (actual.kind !== expected.kind || actual.project_id !== expected.project_id
    || (actual.kind === 'scope' && expected.kind === 'scope' && actual.scope_id !== expected.scope_id)) {
    throw new Error('Access operation returned another repository target');
  }
}

export function validateAccessSurface(surface: AccessSurface, projectId?: string, surfaceId?: string): AccessSurface {
  if (!surface || typeof surface.id !== 'string' || !surface.id.trim()
    || typeof surface.project_id !== 'string' || !surface.project_id.trim()
    || (projectId !== undefined && surface.project_id !== projectId)
    || (surfaceId !== undefined && surface.id !== surfaceId)
    || !ACCESS_SURFACE_KINDS.includes(surface.kind)) {
    throw new Error('Access response has an invalid or inconsistent surface identity');
  }
  validateTarget(surface.target, surface.project_id);
  return surface;
}

function query(params: Record<string, string | undefined>, allowed: readonly string[]): string {
  const values = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (!allowed.includes(key)) throw new Error('Unknown Access query selector');
    if (value === undefined) continue;
    if (typeof value !== 'string' || !value.trim()) throw new Error('Access selectors must not be empty');
    values.set(key, value);
  }
  return values.size ? `?${values}` : '';
}

/** Canonical resource transport; no legacy URL, provider-field or identity fallback. */
export function createAccessSurfacesApi(t: CloudTransport) {
  const globalBase = '/api/v1/access/surfaces';
  const projectBase = (projectId: string) => `/api/v1/projects/${segment(projectId)}/access/surfaces`;
  const path = (projectId: string, id: string) => `${projectBase(projectId)}/${segment(id)}`;
  const globalPath = (id: string) => `${globalBase}/${segment(id)}`;
  const inventory = (rows: AccessSurface[], projectId?: string) => {
    if (!Array.isArray(rows)) throw new Error('Access inventory is not an array');
    const seen = new Set<string>();
    return rows.map(row => {
      validateAccessSurface(row, projectId);
      if (seen.has(row.id)) throw new Error('Access inventory contains duplicate surface identities');
      seen.add(row.id);
      return row;
    });
  };
  return {
    async listAccessSurfaces(projectId: string, filter: { kind?: AccessKind; direction?: AccessDirection } = {}) {
      return inventory(await t.get<AccessSurface[]>(projectBase(projectId) + query(filter, ['kind', 'direction'])), projectId);
    },
    async listAccessibleSurfaces(filter: { project_id?: string; kind?: AccessKind; status?: string } = {}) {
      return inventory(await t.get<AccessSurface[]>(globalBase + query(filter, ['project_id', 'kind', 'status'])), filter.project_id);
    },
    async getAccessSurface(id: string) {
      return validateAccessSurface(await t.get<AccessSurface>(globalPath(id)), undefined, id);
    },
    listAccessSurfaceTypes: () => t.get<AccessSurfaceKind[]>(`${globalBase}/types`),
    async createAccessSurface(projectId: string, body: AccessSurfaceCreate) {
      validateTarget(body.target, projectId);
      const surface = validateAccessSurface(await t.post<AccessSurface>(projectBase(projectId), body), projectId);
      if (surface.kind !== body.kind) throw new Error('Access creation returned another surface kind');
      requireSameTarget(surface.target, body.target);
      return surface;
    },
    async configureAccessSurface(body: AccessSurfaceConfigure) {
      segment(body.project_id);
      const surface = await t.post<AccessSurfaceCreated>(globalBase, body);
      if (typeof surface?.id !== 'string' || !surface.id.trim() || surface.project_id !== body.project_id || surface.kind !== body.kind) {
        throw new Error('Access creation returned inconsistent surface identities');
      }
      if (surface.target) validateTarget(surface.target, body.project_id);
      return surface;
    },
    async enableTargetAccess(projectId: string, target: RepositoryTarget) {
      validateTarget(target, projectId);
      const surfaces = inventory(await t.post<AccessSurface[]>(`${projectBase(projectId)}/enable-target`, { target }), projectId);
      for (const surface of surfaces) requireSameTarget(surface.target, target);
      return surfaces;
    },
    async updateAccessSurface(projectId: string, id: string, body: AccessSurfaceUpdate) {
      return validateAccessSurface(await t.patch<AccessSurface>(path(projectId, id), body), projectId, id);
    },
    async updateAccessSurfaceMetadata(id: string, body: Pick<AccessSurfaceUpdate, 'status' | 'trigger' | 'config'>) {
      return validateAccessSurface(await t.patch<AccessSurface>(globalPath(id), body), undefined, id);
    },
    async renameAccessSurface(id: string, name: string) {
      return validateAccessSurface(await t.patch<AccessSurface>(`${globalPath(id)}/rename`, { name }), undefined, id);
    },
    async regenerateAccessSurfaceKey(id: string) {
      const result = await t.post<AccessCredentialIssued>(`${globalPath(id)}/regenerate-key`, {});
      if (result?.access_surface_id !== id || typeof result.credential !== 'string' || !result.credential.trim()) throw new Error('Access credential returned an inconsistent surface identity');
      return result;
    },
    async deleteAccessSurface(projectId: string, id: string): Promise<void> { await t.del(path(projectId, id)); },
    async deleteConfiguredAccessSurface(id: string): Promise<void> { await t.del(globalPath(id)); },
    async activateAccessSurfaceAgent(projectId: string, id: string) {
      return validateAccessSurface(await t.post<AccessSurface>(`${path(projectId, id)}/activate-agent`, {}), projectId, id);
    },
    async pauseAccessSurface(projectId: string, id: string): Promise<void> { await t.post(`${path(projectId, id)}/pause`, {}); },
    async resumeAccessSurface(projectId: string, id: string): Promise<void> { await t.post(`${path(projectId, id)}/resume`, {}); },
  };
}

export function isAccessSurface(surface: Pick<AccessSurface, 'kind'> & Partial<Pick<AccessSurface, 'trigger'>>): boolean {
  return ACCESS_SURFACE_KINDS.includes(surface.kind) && surface.trigger?.type !== 'import_once';
}

export function filterAccessSurfaces(surfaces: readonly AccessSurface[]): AccessSurface[] {
  return surfaces.filter(isAccessSurface);
}

export function sortAccessSurfacesBuiltinFirst(surfaces: readonly AccessSurface[]): AccessSurface[] {
  return [...surfaces].sort((a, b) => getAccessProviderSortRank(a.kind) - getAccessProviderSortRank(b.kind)
    || (a.created_at ?? '').localeCompare(b.created_at ?? ''));
}
