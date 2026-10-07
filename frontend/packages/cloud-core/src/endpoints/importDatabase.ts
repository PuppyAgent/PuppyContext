import type { CloudTransport } from '../transport';

export type ImportDatabaseKeyType = 'anon' | 'service_role';
export interface ImportDatabaseSource {
  id: string; name: string; provider: string; project_id: string;
  is_active: boolean; last_used_at: string | null; created_at: string;
}
export interface ImportDatabaseSourceCreate { name: string; provider?: string; project_url: string; api_key: string; key_type?: ImportDatabaseKeyType }
export interface ImportDatabaseSourceCreated { source: ImportDatabaseSource; database_info: Record<string, unknown> }
export interface ImportDatabaseTable { name: string; type: string; columns: { name: string; type: string }[] }
export interface ImportDatabasePreview { columns: string[]; rows: Record<string, unknown>[]; row_count: number; execution_time_ms: number }
export interface ImportDatabaseSave { name: string; table: string; limit?: number }
export interface ImportDatabaseSaved { import_database_source_id: string; content_path: string; row_count: number }
export interface ImportDatabaseErrorDetail { error_code: string | null; message: string; suggested_actions: string[] }

const BASE = '/api/v1/imports/database/sources';
function explicit(value: string): string {
  if (typeof value !== 'string' || !value.trim()) throw new Error('An explicit Database Import source identity or selector is required');
  return value;
}
function path(id: string) { return `${BASE}/${encodeURIComponent(explicit(id))}`; }
function project(projectId: string) { return new URLSearchParams({ project_id: explicit(projectId) }).toString(); }
function source(value: ImportDatabaseSource, projectId?: string, id?: string) {
  if (!value || typeof value.id !== 'string' || !value.id.trim() || typeof value.project_id !== 'string' || !value.project_id.trim()
      || (projectId !== undefined && value.project_id !== projectId) || (id !== undefined && value.id !== id)
      || typeof value.is_active !== 'boolean' || 'config' in value || 'api_key' in value) throw new Error('Database Import returned an invalid source identity or metadata');
  return value;
}

export function createImportDatabaseApi(t: CloudTransport) {
  return {
    async createImportDatabaseSource(projectId: string, body: ImportDatabaseSourceCreate) {
      const value = await t.post<ImportDatabaseSourceCreated>(`${BASE}?${project(projectId)}`, body);
      source(value.source, projectId);
      return value;
    },
    async listImportDatabaseSources(projectId: string) {
      const rows = await t.get<ImportDatabaseSource[]>(`${BASE}?${project(projectId)}`);
      if (!Array.isArray(rows)) throw new Error('Database Import inventory is not an array');
      const ids = new Set<string>();
      for (const row of rows) {
        source(row, projectId);
        if (ids.has(row.id)) throw new Error('Database Import inventory contains duplicate source identities');
        ids.add(row.id);
      }
      return rows;
    },
    async getImportDatabaseSource(id: string) { return source(await t.get<ImportDatabaseSource>(path(id)), undefined, id); },
    async deleteImportDatabaseSource(id: string): Promise<void> { await t.del(path(id)); },
    listImportDatabaseTables(id: string) { return t.get<ImportDatabaseTable[]>(`${path(id)}/tables`); },
    previewImportDatabaseTable(id: string, table: string, limit = 50) {
      if (!Number.isSafeInteger(limit) || limit < 1 || limit > 200) throw new Error('Invalid Database Import preview limit');
      return t.get<ImportDatabasePreview>(`${path(id)}/tables/${encodeURIComponent(explicit(table))}/preview?limit=${limit}`);
    },
    async saveImportDatabaseTable(id: string, projectId: string, body: ImportDatabaseSave) {
      const value = await t.post<ImportDatabaseSaved>(`${path(id)}/save?${project(projectId)}`, body);
      if (value.import_database_source_id !== id) throw new Error('Database Import save returned another source identity');
      return value;
    },
  };
}
