import type { CloudTransport } from '../transport';

export type SynchronizeGithubDirection = 'inbound' | 'outbound';
export type SynchronizeGithubStatus = 'pending' | 'success' | 'failed' | 'conflict';
export interface SynchronizeGithubBindingCreate {
  oauth_connection_id: number; github_repo_owner: string; github_repo_name: string;
  default_branch?: string; auto_pull?: boolean; webhook_secret?: string | null;
}
export interface SynchronizeGithubBindingUpdate { default_branch?: string; auto_pull?: boolean; webhook_secret?: string | null }
export interface SynchronizeGithubBinding {
  id: string; project_id: string; oauth_connection_id: number | null;
  github_repo_owner: string; github_repo_name: string; default_branch: string;
  auto_pull: boolean; has_webhook_secret: boolean;
  last_pulled_sha: string | null; last_pulled_at: string | null;
  last_pushed_sha: string | null; last_pushed_at: string | null;
  created_at: string; updated_at: string;
}
export interface SynchronizeGithubRepo { owner: string; name: string; full_name: string; default_branch: string; private: boolean }
export interface SynchronizeGithubRepos { repos: SynchronizeGithubRepo[] }
export interface SynchronizeGithubBranch { name: string; sha: string; protected: boolean; is_default: boolean }
export interface SynchronizeGithubBranches { repo_owner: string; repo_name: string; branches: SynchronizeGithubBranch[] }
export interface SynchronizeGithubPull { branch?: string | null; force?: boolean }
export interface SynchronizeGithubPush { branch?: string | null; message?: string | null }
export interface SynchronizeGithubResult {
  synchronize_github_binding_id: string; status: SynchronizeGithubStatus; direction: SynchronizeGithubDirection;
  git_sha: string | null; version_commit_id: string | null; files_changed: number | null; error_message?: string | null;
}
export interface SynchronizeGithubLog extends SynchronizeGithubResult { id: string; created_at: string }
export interface SynchronizeGithubLogs { synchronize_github_binding_id: string; entries: SynchronizeGithubLog[]; total: number }

function explicit(value: string): string {
  if (typeof value !== 'string' || !value.trim()) throw new Error('An explicit GitHub Synchronize identity or selector is required');
  return value;
}
function oauthId(value: number): string {
  if (!Number.isSafeInteger(value) || value < 1) throw new Error('An explicit OAuth account ID is required');
  return String(value);
}
function base(projectId: string) { return `/api/v1/projects/${encodeURIComponent(explicit(projectId))}/synchronize/github`; }
function binding(value: SynchronizeGithubBinding, projectId: string): SynchronizeGithubBinding {
  if (!value || !value.id || value.project_id !== projectId || typeof value.auto_pull !== 'boolean' || 'webhook_secret' in value) {
    throw new Error('GitHub Synchronize returned an invalid binding identity or metadata');
  }
  explicit(value.id);
  return value;
}
function execution<T extends SynchronizeGithubResult>(value: T, direction?: SynchronizeGithubDirection): T {
  if (!value || typeof value.synchronize_github_binding_id !== 'string' || !value.synchronize_github_binding_id.trim()
      || !['inbound', 'outbound'].includes(value.direction) || (direction && value.direction !== direction)) {
    throw new Error('GitHub Synchronize returned an invalid execution identity or direction');
  }
  return value;
}

export function createSynchronizeGithubApi(t: CloudTransport) {
  return {
    async getSynchronizeGithubBinding(projectId: string) {
      const value = await t.get<SynchronizeGithubBinding | null>(`${base(projectId)}/binding`);
      return value === null ? null : binding(value, projectId);
    },
    async createSynchronizeGithubBinding(projectId: string, body: SynchronizeGithubBindingCreate) {
      oauthId(body.oauth_connection_id);
      return binding(await t.post<SynchronizeGithubBinding>(`${base(projectId)}/binding`, body), projectId);
    },
    async updateSynchronizeGithubBinding(projectId: string, body: SynchronizeGithubBindingUpdate) {
      return binding(await t.patch<SynchronizeGithubBinding>(`${base(projectId)}/binding`, body), projectId);
    },
    async deleteSynchronizeGithubBinding(projectId: string): Promise<void> { await t.del(`${base(projectId)}/binding`); },
    listSynchronizeGithubRepos(projectId: string, oauthConnectionId: number) {
      const query = new URLSearchParams({ oauth_connection_id: oauthId(oauthConnectionId) });
      return t.get<SynchronizeGithubRepos>(`${base(projectId)}/repos?${query}`);
    },
    async listSynchronizeGithubBranches(projectId: string, oauthConnectionId: number, owner: string, name: string) {
      const query = new URLSearchParams({ oauth_connection_id: oauthId(oauthConnectionId), repo_owner: explicit(owner), repo_name: explicit(name) });
      const result = await t.get<SynchronizeGithubBranches>(`${base(projectId)}/branches?${query}`);
      if (result.repo_owner !== owner || result.repo_name !== name) throw new Error('GitHub branch discovery returned another repository');
      return result;
    },
    async pullSynchronizeGithubBinding(projectId: string, body: SynchronizeGithubPull = {}) {
      return execution(await t.post<SynchronizeGithubResult>(`${base(projectId)}/pull`, body), 'inbound');
    },
    async pushSynchronizeGithubBinding(projectId: string, body: SynchronizeGithubPush = {}) {
      return execution(await t.post<SynchronizeGithubResult>(`${base(projectId)}/push`, body), 'outbound');
    },
    async listSynchronizeGithubLogs(projectId: string, options: { limit?: number; offset?: number } = {}) {
      const query = new URLSearchParams();
      for (const [key, value] of Object.entries(options)) {
        if (!['limit', 'offset'].includes(key)) throw new Error('Unknown GitHub log selector');
        if (!Number.isSafeInteger(value) || value! < (key === 'limit' ? 1 : 0) || (key === 'limit' && value! > 500)) throw new Error('Invalid GitHub log selector');
        query.set(key, String(value));
      }
      const value = await t.get<SynchronizeGithubLogs>(`${base(projectId)}/logs${query.size ? `?${query}` : ''}`);
      explicit(value.synchronize_github_binding_id);
      if (!Array.isArray(value.entries)) throw new Error('GitHub log inventory is not an array');
      const seen = new Set<string>();
      for (const row of value.entries) {
        execution(row); explicit(row.id);
        if (row.synchronize_github_binding_id !== value.synchronize_github_binding_id || seen.has(row.id)) throw new Error('GitHub logs contain inconsistent or duplicate identities');
        seen.add(row.id);
      }
      return value;
    },
  };
}

export function synchronizeGithubWebhookUrl(apiOrigin: string): string {
  return `${explicit(apiOrigin).replace(/\/$/, '')}/api/v1/synchronize/github/webhook`;
}
