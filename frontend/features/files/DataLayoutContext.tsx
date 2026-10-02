'use client';

import type { Tool } from '@/lib/mcpApi';
import type { Connector, RepoIdentity, RepositoryTarget, RepositoryView } from '@/lib/repoApi';
import { createContext, useContext } from 'react';

export type { SynchronizeStatusItem as SyncStatusSync } from '@/lib/synchronizeApi';
import type { SynchronizeStatus } from '@/lib/synchronizeApi';

export interface SyncEndpointInfo {
  syncId: string;
  provider: string;
  direction: string;
  status: string;
  name?: string;
  accessKey?: string | null;
  repositoryTarget?: RepositoryTarget;
}

export interface DataLayoutContextValue {
  syncStatusData: SynchronizeStatus | undefined;
  mutateSyncStatus: () => Promise<any>;
  projectTools: Tool[];
  syncEndpoints: Map<string, SyncEndpointInfo>;
  nodeEndpointMap: Map<string, SyncEndpointInfo[]>;

  /** Project-root repository plus true scoped repository views. */
  scopes: RepositoryView[];
  /** Index of connectors by the explicit repository target discriminant. */
  connectorsByTarget: Map<string, Connector[]>;
  /** Repo identity (URL + prompt_template + per-scope keys) — fetched once per project. */
  repoIdentity: RepoIdentity | undefined;
  repoIdentityLoading: boolean;
  repoIdentityError: unknown;
  mutateRepo: () => Promise<unknown>;
}

const DataLayoutContext = createContext<DataLayoutContextValue | null>(null);

export function useDataLayout() {
  const ctx = useContext(DataLayoutContext);
  if (!ctx) throw new Error('useDataLayout must be used within DataLayout');
  return ctx;
}

export { DataLayoutContext };
