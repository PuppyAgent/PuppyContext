'use client';

import { getSynchronizeStatus, type SynchronizeStatus } from '@/lib/synchronizeApi';
import { useProjectTools, useTreeDir } from '@/lib/hooks/useData';
import { listMcpEndpoints } from '@/lib/mcpEndpointsApi';
import { getRepoIdentity, listAccessSurfaces, listScopes } from '@/lib/repoApi';
import { listSandboxEndpoints } from '@/lib/sandboxEndpointsApi';
import useSWR from 'swr';

const decorationConfig = { revalidateOnFocus: false, dedupingInterval: 60000, keepPreviousData: false };

/** Foreground: only this project's root. Background: file badges / connection
 * metadata after that snapshot exists. No arbitrary delay and no all-project
 * tree traversal. Expanded directories share useTreeDir's per-path cache. */
export function useFileWorkspaceQueries(projectId: string) {
  const root = useTreeDir(projectId, '');
  const enabled = Boolean(projectId) && root.hasLoaded;
  const tools = useProjectTools(enabled ? projectId : undefined);
  const sync = useSWR<SynchronizeStatus>(enabled ? ['synchronize-status', projectId] : null,
    () => getSynchronizeStatus(projectId), decorationConfig);
  const mcp = useSWR(enabled ? ['mcp-endpoints', projectId] : null,
    () => listMcpEndpoints(projectId), decorationConfig);
  const sandbox = useSWR(enabled ? ['sandbox-endpoints', projectId] : null,
    () => listSandboxEndpoints(projectId), decorationConfig);
  const scopes = useSWR(enabled ? ['repo-scopes', projectId] : null,
    () => listScopes(projectId), decorationConfig);
  const connectors = useSWR(enabled ? ['repo-connectors', projectId] : null,
    () => listAccessSurfaces(projectId), decorationConfig);
  const identity = useSWR(enabled ? ['repo-identity', projectId] : null,
    () => getRepoIdentity(projectId), decorationConfig);
  return { root, tools, sync, mcp, sandbox, scopes, connectors, identity };
}
