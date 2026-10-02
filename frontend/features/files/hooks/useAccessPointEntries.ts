import type { SyncStatusSync } from '@/features/files/DataLayoutContext';
import type { EndpointNameMap, ProviderIconLookup } from '@/features/files/components/access-points';
import { getEndpointEntries } from '@/features/files/components/access-points/utils';
import type { SyncEndpointInfo } from '@/features/files/components/explorer';
import { useConnectorSpecs } from '@/lib/hooks/useData';
import { resolveProviderIconUrl } from '@/lib/providerIcons';
import { useMemo } from 'react';

interface AgentNameSource {
  id: string;
  name: string;
}

export function useAccessPointEntries({
  nodeEndpointMap,
  savedAgents,
  tableNameById,
  syncStatusData,
}: {
  nodeEndpointMap: Map<string, SyncEndpointInfo[]>;
  savedAgents: readonly AgentNameSource[];
  tableNameById: Record<string, string>;
  syncStatusData: { bindings: SyncStatusSync[] } | undefined;
}) {
  const { specs: connectorSpecs } = useConnectorSpecs();

  const endpointNameMap = useMemo<EndpointNameMap>(() => {
    const agents: Record<string, string> = {};
    for (const agent of savedAgents) agents[agent.id] = agent.name;

    const nodes: Record<string, string> = { ...tableNameById };
    if (syncStatusData?.bindings) {
      for (const sync of syncStatusData.bindings) {
        if (sync.path && !nodes[sync.path] && sync.name) nodes[sync.path] = sync.name;
      }
    }

    const syncs: Record<string, string> = {};
    if (syncStatusData?.bindings) {
      for (const sync of syncStatusData.bindings) {
        const providerLabels: Record<string, string> = {
          gmail: 'Gmail',
          google_calendar: 'Calendar',
          google_sheets: 'Sheets',
          google_drive: 'Drive',
          google_docs: 'Docs',
          github: 'GitHub',
          notion: 'Notion',
          linear: 'Linear',
          airtable: 'Airtable',
          mcp: 'MCP Server',
          sandbox: 'Sandbox',
        };
        syncs[sync.id] = sync.name || providerLabels[sync.provider] || sync.provider;
      }
    }

    return { agents, nodes, syncs };
  }, [savedAgents, tableNameById, syncStatusData]);

  const providerIcons = useMemo<ProviderIconLookup>(() => {
    const icons: ProviderIconLookup = {};
    for (const spec of connectorSpecs) {
      icons[spec.provider] = {
        icon: spec.icon,
        iconUrl: resolveProviderIconUrl({
          provider: spec.provider,
          icon: spec.icon,
          iconUrl: spec.icon_url,
        }),
      };
    }
    return icons;
  }, [connectorSpecs]);

  const accessPointEntries = useMemo(
    () => getEndpointEntries(nodeEndpointMap, endpointNameMap),
    [nodeEndpointMap, endpointNameMap],
  );

  return { accessPointEntries, endpointNameMap, providerIcons };
}
