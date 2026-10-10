import type { EndpointEntry, EndpointNameMap } from '@/features/files/components/access-points/types';
import type { EntrypointBadge } from '@/features/files/components/explorer';
import type { PanelState } from '@/features/files/usePanelStore';
import { entrypointBadgeKey } from '@/features/files/entrypointBadges';
import { repositoryTargetKey } from '@puppyone/cloud-core';
import { isMcpProvider, isSandboxProvider } from '@/lib/accessProviderRegistry';

export function endpointToPanelState(ep: EntrypointBadge, nodeId: string): PanelState {
  if (ep.resourceKind === 'synchronize') return { type: 'sync_config', nodeId, synchronizeBindingId: ep.id };
  if (ep.resourceKind !== 'access') return { type: 'none' };
  if (ep.provider.startsWith('agent:')) return { type: 'workspace_chat', nodeId, agentId: ep.id };
  if (isMcpProvider(ep.provider)) return { type: 'mcp_config', nodeId, mcpEndpointId: ep.id };
  if (isSandboxProvider(ep.provider)) return { type: 'sandbox_config', nodeId, sandboxEndpointId: ep.id };
  return {
    type: 'access_list', nodeId,
    view: ep.repositoryTarget ? 'detail' : 'overview',
    selectedTargetKey: ep.repositoryTarget ? repositoryTargetKey(ep.repositoryTarget) : undefined,
  };
}

export function getEndpointEntries(
  nodeEndpointMap: Map<string, EntrypointBadge[]>,
  nameMap: EndpointNameMap,
): EndpointEntry[] {
  const map = new Map<string, EndpointEntry>();
  for (const [nodeId, eps] of nodeEndpointMap.entries()) {
    for (const ep of eps) {
      const key = entrypointBadgeKey(ep);
      if (map.has(key)) continue;
      const referencedName = ep.resourceKind === 'synchronize' ? nameMap.syncs[ep.id]
        : ep.provider.startsWith('agent:') ? nameMap.agents[ep.id] : undefined;
      const name = ep.name || referencedName || ep.provider;
      const nodeName = nameMap.nodes[nodeId] || (nodeId ? nodeId : 'Root');
      map.set(key, { ep, nodeId, name, nodeName });
    }
  }
  return Array.from(map.values());
}
