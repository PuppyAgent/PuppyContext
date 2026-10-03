import type { CloudTransport as Transport } from '../transport';

export type ActivityKind = 'upload' | 'import' | 'synchronize_run';
export interface ActivityItem {
  id: string;
  kind: ActivityKind;
  project_id: string;
  created_by?: string | null;
  label?: string | null;
  status?: string | null;
  phase?: string | null;
  progress?: number | null;
  message?: string | null;
  error_message?: string | null;
  result_path?: string | null;
  result_commit_id?: string | null;
  created_at?: string | null;
  completed_at?: string | null;
}
export interface ActivityListResponse { items: ActivityItem[]; total: number }
export interface ActivitySelectors { kind?: ActivityKind; activeOnly?: boolean; limit?: number }
const KINDS = new Set(['upload', 'import', 'synchronize_run']);
const TERMINAL = new Set(['completed', 'success', 'failed', 'cancelled', 'canceled', 'skipped', 'conflict', 'error']);
export function isActivityItemActive(item: ActivityItem): boolean {
  return !item.completed_at && !TERMINAL.has((item.status || '').toLowerCase()) && !TERMINAL.has((item.phase || '').toLowerCase());
}
export function createActivityApi(t: Transport) {
  return {
    async getProjectActivity(projectId: string, options: ActivitySelectors = {}): Promise<ActivityListResponse> {
      if (!projectId.trim()) throw new Error('An explicit Activity Project ID is required');
      if (Object.keys(options).some(key => !['kind', 'activeOnly', 'limit'].includes(key))) throw new Error('Unknown Activity selector');
      if (options.kind !== undefined && !KINDS.has(options.kind)) throw new Error('Unknown Activity kind');
      if (options.limit !== undefined && (!Number.isInteger(options.limit) || options.limit < 1 || options.limit > 200)) throw new Error('Invalid Activity limit');
      const query = new URLSearchParams({ project_id: projectId });
      if (options.kind !== undefined) query.set('kind', options.kind);
      if (options.activeOnly !== undefined) query.set('active_only', String(options.activeOnly));
      if (options.limit !== undefined) query.set('limit', String(options.limit));
      const data = await t.get<ActivityListResponse>(`/api/v1/activity/items?${query}`);
      if (!Array.isArray(data?.items)) throw new Error('Activity inventory is not an array');
      const seen = new Set<string>();
      for (const item of data.items) {
        if (!item || typeof item.id !== 'string' || !item.id.trim() || item.project_id !== projectId || !KINDS.has(item.kind)
          || (options.kind !== undefined && item.kind !== options.kind)) throw new Error('Activity returned an invalid or foreign resource identity');
        const key = `${item.kind}:${item.id}`;
        if (seen.has(key)) throw new Error('Activity returned duplicate resource identities');
        seen.add(key);
      }
      return data;
    },
  };
}
