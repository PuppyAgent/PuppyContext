'use client';

import { useAgent } from '@/contexts/AgentContext';
import {
  DataLayoutContext,
  type EntrypointBadge,
} from '@/features/files/DataLayoutContext';
import { useFileWorkspaceQueries } from '@/features/files/useFileWorkspaceQueries';
import { entrypointBadgeKey, selectSynchronizeBadges } from './entrypointBadges';
import {
  isAccessSurface,
  filterAccessSurfaces,
  projectRootRepositoryView,
  repositoryScopeView,
  repositoryTargetKey,
  type AccessSurface,
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
      filterAccessSurfaces(connectorsList || [])
        .filter((connector) => isAccessSurface(connector)),
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
    const m = new Map<string, AccessSurface[]>();
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
    const map = new Map<string, EntrypointBadge[]>();
    const append = (rawNodeId: string | null | undefined, endpoint: EntrypointBadge) => {
      const nodeId = normalizeEndpointPath(rawNodeId);
      const list = map.get(nodeId) || [];
      if (list.some((item) => entrypointBadgeKey(item) === entrypointBadgeKey(endpoint))) return;
      list.push(endpoint);
      map.set(nodeId, list);
    };

    if (syncStatusData?.bindings) {
      for (const s of syncStatusData.bindings) {
        append(s.path, {
          resourceKind: 'synchronize',
          id: s.id,
          provider: s.provider,
          direction: s.direction,
          status: s.status,
          name: s.name ?? undefined,
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
      if (c.kind === 'agent') continue;
      const view = viewByTarget.get(repositoryTargetKey(c.target));
      if (!view) continue;
      append(view.path, {
        resourceKind: 'access',
        id: c.id,
        provider: c.kind,
        direction: c.direction ?? '',
        status: c.status,
        name: c.name || view.name,
        repositoryTarget: view.target,
      });
    }

    // Adapter rows are projections of access_surfaces (the same primary ID).
    // Enrich display paths only after matching that persisted resource, never
    // infer an Access identity or target from a Provider name or path.
    const surfaceById = new Map(accessConnectorsForDataView.map(surface => [surface.id, surface]));
    for (const agent of savedAgents) {
      const surface = surfaceById.get(agent.id);
      if (surface?.kind === 'agent' && agent.type === 'chat' && agent.resources) {
        for (const r of agent.resources) {
          append(r.path, {
            resourceKind: 'access',
            id: agent.id,
            provider: `agent:${agent.type}`,
            direction: 'bidirectional',
            status: surface.status,
            name: agent.name,
          });
        }
      }
    }

    for (const endpoint of mcpEndpoints || []) {
      const surface = surfaceById.get(endpoint.id);
      if (!surface || !['mcp', 'mcp_endpoint'].includes(surface.kind)) continue;
      const info: EntrypointBadge = {
        resourceKind: 'access',
        id: endpoint.id,
        provider: 'mcp',
        direction: 'bidirectional',
        status: surface.status,
        name: endpoint.name,
      };
      append(endpoint.path, info);
      for (const access of endpoint.accesses || []) {
        append(access.path, info);
      }
    }

    for (const endpoint of sandboxEndpoints || []) {
      const surface = surfaceById.get(endpoint.id);
      if (!surface || !['sandbox', 'sandbox_endpoint'].includes(surface.kind)) continue;
      const info: EntrypointBadge = {
        resourceKind: 'access',
        id: endpoint.id,
        provider: 'sandbox',
        direction: 'bidirectional',
        status: surface.status,
        name: endpoint.name,
      };
      append(endpoint.path, info);
      for (const mount of endpoint.mounts || []) {
        append(mount.path, info);
      }
    }

    return map;
  }, [syncStatusData, savedAgents, mcpEndpoints, sandboxEndpoints, repositoryViews, accessConnectorsForDataView]);

  const syncEndpoints = useMemo(() => selectSynchronizeBadges(nodeEndpointMap), [nodeEndpointMap]);

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
