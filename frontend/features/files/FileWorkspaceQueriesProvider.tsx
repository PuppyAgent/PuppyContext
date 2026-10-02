'use client';

import { useAgent } from '@/contexts/AgentContext';
import {
  DataLayoutContext,
  type SyncEndpointInfo,
} from '@/features/files/DataLayoutContext';
import { useFileWorkspaceQueries } from '@/features/files/useFileWorkspaceQueries';
import {
  isMcpProvider,
  isSandboxProvider,
} from '@/lib/accessProviderRegistry';
import {
  isAccessSurfaceConnector,
  normalizeAccessSurfaceConnectors,
  projectRootRepositoryView,
  repositoryScopeView,
  repositoryTargetKey,
  type Connector,
} from '@/lib/repoApi';
import { useCallback, useMemo } from 'react';

interface DataLayoutProps {
  children: React.ReactNode;
  projectId: string;
}

function normalizeEndpointPath(path: string | null | undefined): string {
  if (!path || path === '/') return '';
  return path.replace(/^\/+|\/+$/g, '');
}

export function FileWorkspaceQueriesProvider({ children, projectId }: DataLayoutProps) {

  const { savedAgents } = useAgent();
  const reads = useFileWorkspaceQueries(projectId);
  const { root } = reads;
  const { tools: projectTools } = reads.tools;
  const { data: syncStatusData, mutate: mutateSyncStatus } = reads.sync;
  const { data: mcpEndpoints } = reads.mcp;
  const { data: sandboxEndpoints } = reads.sandbox;
  const { data: scopes, mutate: mutateScopes } = reads.scopes;
  const { data: connectorsList, mutate: mutateConnectors } = reads.connectors;
  const { data: repoIdentity, error: repoIdentityError, isLoading: repoIdentityLoading, mutate: mutateIdentity } = reads.identity;

  const accessConnectorsForDataView = useMemo(
    () =>
      normalizeAccessSurfaceConnectors(connectorsList || [])
        .filter((connector) => isAccessSurfaceConnector(connector)),
    [connectorsList],
  );

  const repositoryViews = useMemo(
    () => [
      projectRootRepositoryView(projectId),
      ...(scopes || []).map(repositoryScopeView),
    ],
    [projectId, scopes],
  );

  const connectorsByTarget = useMemo(() => {
    const m = new Map<string, Connector[]>();
    for (const c of accessConnectorsForDataView) {
      const key = repositoryTargetKey(c.target);
      const list = m.get(key) || [];
      list.push(c);
      m.set(key, list);
    }
    return m;
  }, [accessConnectorsForDataView]);

  const mutateRepo = useCallback(async () => {
    await Promise.all([mutateScopes(), mutateConnectors(), mutateIdentity()]);
  }, [mutateScopes, mutateConnectors, mutateIdentity]);

  const nodeEndpointMap = useMemo(() => {
    const map = new Map<string, SyncEndpointInfo[]>();
    const append = (rawNodeId: string | null | undefined, endpoint: SyncEndpointInfo) => {
      const nodeId = normalizeEndpointPath(rawNodeId);
      const list = map.get(nodeId) || [];
      if (list.some((item) => item.syncId === endpoint.syncId && item.provider === endpoint.provider)) return;
      list.push(endpoint);
      map.set(nodeId, list);
    };

    if (syncStatusData?.bindings) {
      for (const s of syncStatusData.bindings) {
        append(s.path, {
          syncId: s.id,
          provider: s.provider,
          direction: s.direction,
          status: s.status,
          name: s.name ?? undefined,
          accessKey: null,
        });
      }
    }

    // Project connectors+repository views into the per-row endpoint view so the
    // object menu and connection-list affordances use the canonical model.
    // Credentials are never read from repository metadata; a connector may
    // expose a one-time credential only in its issuance response. Agent
    // connectors are skipped because AgentContext projects them below.
    const viewByTarget = new Map(
      repositoryViews.map((view) => [repositoryTargetKey(view.target), view]),
    );
    for (const c of accessConnectorsForDataView) {
      if (c.provider === 'agent') continue;
      const view = viewByTarget.get(repositoryTargetKey(c.target));
      if (!view) continue;
      append(view.path, {
        syncId: c.id,
        provider: c.provider,
        direction: c.direction,
        status: c.status,
        name: c.name || view.name,
        accessKey: null,
        repositoryTarget: view.target,
      });
    }

    for (const agent of savedAgents) {
      if (agent.type === 'chat' && agent.resources) {
        for (const r of agent.resources) {
          append(r.path, {
            syncId: agent.id,
            provider: `agent:${agent.type}`,
            direction: 'bidirectional',
            status: 'active',
            name: agent.name,
            accessKey: agent.mcp_api_key,
          });
        }
      }
    }

    for (const endpoint of mcpEndpoints || []) {
      const info: SyncEndpointInfo = {
        syncId: endpoint.id,
        provider: 'mcp',
        direction: 'bidirectional',
        status: endpoint.status,
        name: endpoint.name,
        accessKey: endpoint.api_key,
      };
      append(endpoint.path, info);
      for (const access of endpoint.accesses || []) {
        append(access.path, info);
      }
    }

    for (const endpoint of sandboxEndpoints || []) {
      const info: SyncEndpointInfo = {
        syncId: endpoint.id,
        provider: 'sandbox',
        direction: 'bidirectional',
        status: endpoint.status,
        name: endpoint.name,
        accessKey: endpoint.access_key,
      };
      append(endpoint.path, info);
      for (const mount of endpoint.mounts || []) {
        append(mount.path, info);
      }
    }

    return map;
  }, [syncStatusData, savedAgents, mcpEndpoints, sandboxEndpoints, repositoryViews, accessConnectorsForDataView]);

  const syncEndpoints = useMemo(() => {
    const pickPriority = (provider: string): number => {
      if (provider.startsWith('agent:')) return 1;
      if (isMcpProvider(provider)) return 2;
      if (isSandboxProvider(provider)) return 3;
      return 4;
    };

    const map = new Map<string, SyncEndpointInfo>();
    for (const [nodeId, endpoints] of nodeEndpointMap.entries()) {
      const selected = [...endpoints].sort(
        (a, b) => pickPriority(a.provider) - pickPriority(b.provider),
      )[0];
      if (selected) map.set(nodeId, selected);
    }
    return map;
  }, [nodeEndpointMap]);

  const contextValue = useMemo(
    () => ({
      syncStatusData,
      mutateSyncStatus,
      projectTools,
      syncEndpoints,
      nodeEndpointMap,
      scopes: repositoryViews,
      connectorsByTarget,
      repoIdentity,
      repoIdentityError,
      repoIdentityLoading: !root.hasLoaded && !root.error || repoIdentityLoading,
      mutateRepo,
    }),
    [
      syncStatusData,
      mutateSyncStatus,
      projectTools,
      syncEndpoints,
      nodeEndpointMap,
      repositoryViews,
      connectorsByTarget,
      repoIdentity,
      repoIdentityError,
      repoIdentityLoading,
      root.hasLoaded,
      root.error,
      mutateRepo,
    ],
  );

  return (
    <DataLayoutContext.Provider value={contextValue}>
      {children}
    </DataLayoutContext.Provider>
  );
}
