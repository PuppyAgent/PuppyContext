import type { CloudTransport } from '../transport';
import type { AccessKind } from './accessSurfaces';
import type { RepositoryTarget } from '../repositoryTargets';

interface DashboardResourceSummary {
  resource_id: string;
  project_id: string;
  name: string | null;
  path: string | null;
  direction: string | null;
  status: string;
  trigger: Record<string, unknown> | null;
  last_activity_at: string | null;
  error_message: string | null;
  created_at: string | null;
  usage_buckets: number[];
}
export type DashboardResource = DashboardResourceSummary & (
  | { resource_kind: 'synchronize'; provider: string; path: string }
  | { resource_kind: 'access'; kind: AccessKind; target: RepositoryTarget; scope_mode: 'r' | 'rw' | null }
);
export interface ResourceDashboard {
  project: { id: string; name: string; description: string | null };
  nodes: { total: number; folders: number; files: number };
  resources: DashboardResource[];
  tools: { id: string; name: string; type: string | null; index_status: string | null }[];
  uploads: { id: string; status: string; type: string; progress: number; message: string | null }[];
}
export const dashboardResourceKey = (resource: DashboardResource): string => `${resource.resource_kind}:${resource.resource_id}`;

export function validateResourceDashboard(data: ResourceDashboard, projectId: string): ResourceDashboard {
  if (!data || data.project?.id !== projectId || !Array.isArray(data.resources)) throw new Error('Dashboard returned an invalid Project or inventory');
  const seen = new Set<string>();
  for (const row of data.resources) {
    if (!row || typeof row.resource_id !== 'string' || !row.resource_id.trim() || row.project_id !== projectId
      || !['synchronize', 'access'].includes(row.resource_kind)) throw new Error('Dashboard returned an invalid resource identity');
    if (row.resource_kind === 'synchronize') {
      if (typeof row.provider !== 'string' || !row.provider.trim() || typeof row.path !== 'string') throw new Error('Dashboard returned an invalid Synchronize binding');
    } else {
      const target = row.target;
      if (!['git_remote', 'cli', 'agent', 'mcp', 'mcp_endpoint', 'sandbox', 'sandbox_endpoint'].includes(row.kind)
        || !target || target.project_id !== projectId || !['project_root', 'scope'].includes(target.kind)
        || (target.kind === 'scope' && (typeof target.scope_id !== 'string' || !target.scope_id.trim()))) throw new Error('Dashboard returned an invalid Access target');
      const allowed = target.kind === 'scope' ? ['kind', 'project_id', 'scope_id'] : ['kind', 'project_id'];
      if (Object.keys(target).some(key => !allowed.includes(key))) throw new Error('Dashboard Access target contains ambiguous fields');
    }
    if ('access_key' in row || 'api_key' in row) throw new Error('Dashboard metadata must not contain credentials');
    const key = dashboardResourceKey(row);
    if (seen.has(key)) throw new Error('Dashboard returned duplicate resource identities');
    seen.add(key);
  }
  return data;
}
export function createResourceDashboardApi(t: CloudTransport) {
  return {
    async getResourceDashboard(projectId: string): Promise<ResourceDashboard> {
      if (!projectId.trim()) throw new Error('An explicit Dashboard Project ID is required');
      return validateResourceDashboard(await t.get<ResourceDashboard>(`/api/v1/projects/${encodeURIComponent(projectId)}/dashboard/resources`), projectId);
    },
  };
}
