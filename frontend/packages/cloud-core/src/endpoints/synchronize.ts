import type { CloudTransport } from '../transport';

export interface SynchronizeConfigField {
  key: string;
  label: string;
  type: 'text' | 'select' | 'number' | 'url';
  required: boolean;
  default: string | number | null;
  options: { value: string; label: string }[] | null;
  placeholder: string | null;
  hint: string | null;
}

export interface SynchronizeMaterializationSchema {
  id: string;
  version: number;
  provider?: string | null;
  label: string;
  description: string;
  preview_paths: string[];
  managed: boolean;
  latest?: boolean;
  latest_version?: number;
  upgrade_available?: boolean;
}

export interface SynchronizeProviderSpec {
  provider: string;
  display_name: string;
  description: string | null;
  auth: 'none' | 'oauth' | 'optional_oauth' | 'api_key' | 'access_key';
  creation_mode: 'direct' | 'bootstrap';
  category: 'datasource' | 'agent' | 'endpoint';
  icon: string | null;
  oauth_type?: string | null;
  oauth_ui_type?: string | null;
  default_node_type?: string;
  supported_sync_modes?: string[];
  default_sync_mode?: string;
  supported_directions?: string[];
  accept_types?: string[];
  config_fields?: SynchronizeConfigField[];
  icon_url?: string | null;
  materialization_schema?: SynchronizeMaterializationSchema | null;
  materialization_schemas?: SynchronizeMaterializationSchema[];
}

export interface SynchronizeSourceResource {
  id: string;
  type: string;
  name: string;
  url?: string | null;
  subtitle?: string | null;
  icon?: string | null;
  authorized: boolean;
  metadata: Record<string, unknown>;
}

export interface SynchronizeProviderResources {
  resources: SynchronizeSourceResource[];
  next_cursor?: string | null;
}

export interface SynchronizeBindingCreate {
  project_id: string;
  provider: string;
  config: Record<string, unknown>;
  target_path?: string;
  credentials_ref?: string;
  direction?: string;
  conflict_strategy?: string;
  sync_mode?: 'manual' | 'scheduled' | 'realtime';
  trigger?: { type: string; schedule?: string; timezone?: string };
}

export interface SynchronizeBinding {
  id: string;
  project_id: string;
  path: string;
  direction: string;
  provider: string;
  config: Record<string, unknown>;
  status: string;
  last_synchronize_commit_id: string;
  error_message?: string | null;
  trigger: Record<string, unknown>;
  last_synced_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface SynchronizeBindingUpdate {
  config?: Record<string, unknown>;
  target_path?: string;
  direction?: string;
  conflict_strategy?: string;
}

export interface SynchronizeTriggerUpdate {
  sync_mode: 'manual' | 'scheduled' | 'realtime';
  trigger?: { type: string; schedule?: string; timezone?: string } | null;
}

export interface SynchronizeExecutionResult {
  synchronize_binding_id: string;
  synchronize_run_id?: string | null;
  worker_job_id?: string | null;
  path?: string | null;
  provider: string;
  status: string;
  commit_id?: string | null;
  direction?: string | null;
  summary?: string | null;
  deduped?: boolean;
}

export interface SynchronizeBindingCreated {
  binding: SynchronizeBinding;
  execution_result?: SynchronizeExecutionResult | null;
}

export interface SynchronizeRun {
  id: string;
  synchronize_binding_id: string;
  status: string;
  worker_job_id?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  duration_ms?: number | null;
  exit_code?: number | null;
  stdout?: string | null;
  error?: string | null;
  trigger_type?: string | null;
  result_summary?: string | null;
}

export interface SynchronizeFailedRun {
  id: string;
  synchronize_binding_id: string;
  synchronize_binding_name?: string | null;
  target_path?: string | null;
  provider: string;
  direction: string;
  started_at?: string | null;
  finished_at?: string | null;
  duration_ms?: number | null;
  error?: string | null;
  result_summary?: string | null;
  trigger_type?: string | null;
}

export interface SynchronizeStatusItem {
  id: string;
  path: string | null;
  node_name?: string | null;
  node_type?: string | null;
  provider: string;
  direction: string;
  status: string;
  name?: string | null;
  trigger?: { type?: string; schedule?: string; timezone?: string } | null;
  last_synced_at?: string | null;
  error_message?: string | null;
}

export interface SynchronizeStatus { bindings: SynchronizeStatusItem[] }
export interface SynchronizePullResult { synced: number; results: SynchronizeExecutionResult[] }
export interface SynchronizePushResult { pushed: number; results: SynchronizeExecutionResult[] }

/** The single generic Synchronize resource client; no legacy or Access fallback. */
export function createSynchronizeApi(t: CloudTransport) {
  const base = '/api/v1/synchronize';
  const bindingPath = (id: string) => {
    if (!id?.trim()) throw new Error('A Synchronize binding ID is required');
    return `${base}/bindings/${encodeURIComponent(id)}`;
  };
  const explicitId = (id: string) => typeof id === 'string' && Boolean(id.trim());
  const project = (id: string) => {
    if (!explicitId(id)) throw new Error('An explicit Synchronize Project ID is required');
    return encodeURIComponent(id);
  };
  const validateBinding = (binding: SynchronizeBinding, id?: string) => {
    if (!binding || !explicitId(binding.id) || !explicitId(binding.project_id) || typeof binding.path !== 'string'
      || (id !== undefined && binding.id !== id)) throw new Error('Synchronize returned an invalid binding identity or target path');
    return binding;
  };
  const validateRun = (run: SynchronizeRun) => {
    if (!run || !explicitId(run.id) || !explicitId(run.synchronize_binding_id)) throw new Error('Synchronize run is missing its binding ID or own ID');
    return run;
  };
  const boundedLimit = (limit: number, max: number) => Number.isFinite(limit) ? Math.min(max, Math.max(1, Math.trunc(limit))) : 20;
  return {
    listSynchronizeProviders: () => t.get<SynchronizeProviderSpec[]>(`${base}/providers`),
    listSynchronizeProviderResources(provider: string, params: { q?: string; cursor?: string | null; resource_type?: string | null } = {}) {
      const query = new URLSearchParams();
      for (const [key, value] of Object.entries(params)) if (value) query.set(key, value);
      return t.get<SynchronizeProviderResources>(`${base}/providers/${encodeURIComponent(provider)}/resources${query.size ? `?${query}` : ''}`);
    },
    async createSynchronizeBinding(body: SynchronizeBindingCreate) {
      project(body.project_id);
      const result = await t.post<SynchronizeBindingCreated>(`${base}/bindings`, body);
      if (!result.binding?.id || result.binding.project_id !== body.project_id
        || (result.execution_result && result.execution_result.synchronize_binding_id !== result.binding.id)) {
        throw new Error('Synchronize creation returned inconsistent resource identities');
      }
      validateBinding(result.binding);
      return result;
    },
    async listSynchronizeBindings(projectId: string): Promise<SynchronizeBinding[]> {
      const rows = await t.get<SynchronizeBinding[]>(`${base}/bindings?project_id=${project(projectId)}`);
      if (!Array.isArray(rows) || rows.some((row) => !row?.id || row.project_id !== projectId)) {
        throw new Error('Synchronize list returned an invalid binding or another project');
      }
      const seen = new Set<string>();
      for (const row of rows) {
        validateBinding(row);
        if (seen.has(row.id)) throw new Error('Synchronize list returned duplicate binding identities');
        seen.add(row.id);
      }
      return rows;
    },
    async updateSynchronizeBinding(id: string, body: SynchronizeBindingUpdate) {
      return validateBinding(await t.patch<SynchronizeBinding>(bindingPath(id), body), id);
    },
    deleteSynchronizeBinding: (id: string) => t.del<unknown>(bindingPath(id)),
    updateSynchronizeTrigger: (id: string, body: SynchronizeTriggerUpdate) => t.patch<unknown>(`${bindingPath(id)}/trigger`, body),
    pauseSynchronizeBinding: (id: string) => t.post<unknown>(`${bindingPath(id)}/pause`, {}),
    resumeSynchronizeBinding: (id: string) => t.post<unknown>(`${bindingPath(id)}/resume`, {}),
    async refreshSynchronizeBinding(id: string) {
      const result = await t.post<SynchronizePullResult>(`${bindingPath(id)}/refresh`, {});
      if (!Array.isArray(result?.results) || result.results.length !== 1 || result.results.some(row => !row || row.synchronize_binding_id !== id)) throw new Error('Synchronize refresh returned another binding');
      return result;
    },
    async listSynchronizeRuns(id: string, limit = 20, offset = 0) {
      const rows = await t.get<SynchronizeRun[]>(`${bindingPath(id)}/runs?limit=${boundedLimit(limit, 100)}&offset=${Math.max(0, Math.trunc(offset) || 0)}`);
      if (!Array.isArray(rows) || rows.some((row) => !row || row.synchronize_binding_id !== id)) throw new Error('Synchronize history returned another binding');
      const seen = new Set<string>();
      return rows.map(row => {
        validateRun(row);
        if (seen.has(row.id)) throw new Error('Synchronize history returned duplicate run identities');
        seen.add(row.id);
        return row;
      });
    },
    async getSynchronizeRun(id: string) {
      if (!explicitId(id)) throw new Error('An explicit Synchronize run ID is required');
      const run = validateRun(await t.get<SynchronizeRun>(`${base}/runs/${encodeURIComponent(id)}`));
      if (run.id !== id) throw new Error('Synchronize detail returned another run');
      return run;
    },
    listFailedSynchronizeRuns: (projectId: string, limit = 50) => t.get<SynchronizeFailedRun[]>(`${base}/failed-runs?project_id=${project(projectId)}&limit=${boundedLimit(limit, 200)}`),
    getSynchronizeStatus: (projectId: string) => t.get<SynchronizeStatus>(`${base}/status?project_id=${project(projectId)}`),
    bootstrapSynchronizeBindings: (body: SynchronizeBindingCreate) => t.post<{ bindings_created: number }>(`${base}/bootstrap`, body),
    pullSynchronizeBindings(params: { synchronize_binding_id?: string; project_id?: string; provider?: string }) {
      const query = new URLSearchParams();
      for (const [key, value] of Object.entries(params)) {
        if (!['synchronize_binding_id', 'project_id', 'provider'].includes(key)) {
          throw new Error('Unknown Synchronize pull selector');
        }
        if (value === undefined) continue;
        if (typeof value !== 'string' || !value.trim()) throw new Error('Synchronize pull selectors must not be empty');
        query.set(key, value);
      }
      return t.post<SynchronizePullResult>(`${base}/pull?${query}`, {});
    },
    pushSynchronizePath: (projectId: string, path: string) => t.post<SynchronizePushResult>(`${base}/push/${path.split('/').map(encodeURIComponent).join('/')}?project_id=${project(projectId)}`, {}),
  };
}
