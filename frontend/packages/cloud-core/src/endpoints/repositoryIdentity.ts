import type { CloudTransport } from '../transport';

/** Project repository/protocol identity, not an Access surface or binding. */
export interface RepoIdentity {
  project_id: string;
  url: string;
  prompt_template: string;
  content_initialized?: boolean;
  head_commit_id?: string | null;
  scopes: { id: string; name: string; path: string; git_url: string }[];
}

export function createRepositoryIdentityApi(t: CloudTransport) {
  return {
    getRepoIdentity(projectId: string): Promise<RepoIdentity> {
      return t.get<RepoIdentity>(`/api/v1/projects/${encodeURIComponent(projectId)}/access-point`);
    },
  };
}
