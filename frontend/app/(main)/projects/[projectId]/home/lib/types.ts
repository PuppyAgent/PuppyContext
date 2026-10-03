import type { TreeEntry } from '@/lib/contentTreeApi';
import type { DashboardResource } from '@puppyone/cloud-core';

export type { ResourceDashboard as ProjectDashboard } from '@puppyone/cloud-core';
export { dashboardResourceKey } from '@puppyone/cloud-core';
export type ApDirection = 'inbound' | 'outbound' | 'bidirectional';

/** Display enrichment only. Resource kind/ID/target remain authoritative. */
export type DashboardEntrypoint = DashboardResource & { displayProvider: string };
export function dashboardEntrypoint(resource: DashboardResource): DashboardEntrypoint {
  return { ...resource, displayProvider: resource.resource_kind === 'access' ? resource.kind : resource.provider };
}

export interface TreeNode {
  entry: TreeEntry;
  children: TreeNode[];
}
