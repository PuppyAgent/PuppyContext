'use client';

import { useSessionValue } from '@/features/workspace/session';

/**
 * useAccessData — single hook owning every piece of state the access
 * page reads from the network.
 *
 * Consolidates what used to be ~120 lines of inline logic at the top
 * of the original `AccessPointsPage`:
 *
 *   - Two SWR queries  (scopes, connectors) with revalidate config
 *   - Bucketing        (connectors → Map<scopeId, Connector[]>)
 *   - Filtering+sort   (only scopes with ≥1 connector, root-first)
 *   - Selection state  (selectedTargetKey + auto-select-first effect)
 *   - Pause/resume     (pendingConnectorIds Set + handlePauseResume)
 *
 * Returning everything as a single object lets `page.tsx` destructure
 * the bits it needs without paying the cost of re-running SWR on the
 * same key. The `loading`/`noScopes` flags are derived here so the
 * page never re-implements the "data still loading?" check.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import useSWR from 'swr';
import {
  deleteAccessSurface,
  isAccessSurface,
  listAccessSurfaces,
  listScopes,
  pauseAccessSurface,
  projectRootRepositoryView,
  repositoryScopeView,
  repositoryTargetKey,
  repositoryViewKey,
  resumeAccessSurface,
  updateAccessSurface,
  type AccessSurface,
  type RepositoryView,
} from '@/lib/repoApi';
import {
  getAccessProviderSortRank,
  isAgentProvider,
} from '@/lib/accessProviderRegistry';
import { AI_AGENT_ENABLED } from '@/lib/featureFlags';

/**
 * Patch shape accepted by `handleUpdate`. Mirrors `repoApi.updateConnector`
 * but typed to the small set of fields the access-page UI is allowed to
 * touch — name, direction (third-party only), trigger, and provider config.
 * `oauth_connection_id` and `status` are deliberately omitted: status flips
 * go through the dedicated `handlePauseResume` path so we keep one source
 * of truth for pending-state UI and the dedicated /pause /resume endpoints
 * remain authoritative; OAuth swap is a flow we haven't designed yet.
 */
export type ConnectorEditPatch = Partial<{
  name: string;
  direction: 'inbound' | 'outbound' | 'bidirectional';
  trigger: { type: 'manual' | 'scheduled' | 'on_change'; config?: Record<string, unknown> };
  config: Record<string, unknown>;
}>;

export interface UseAccessDataResult {
  loading: boolean;
  loadError: Error | undefined;
  noScopes: boolean;
  allScopes: RepositoryView[];
  sortedScopes: RepositoryView[];
  connectorsByTarget: Map<string, AccessSurface[]>;
  selectedScope: RepositoryView | undefined;
  selectedConnectors: AccessSurface[];
  representativeConnector: AccessSurface | undefined;
  pendingConnectorIds: ReadonlySet<string>;
  setSelectedTargetKey: (key: string) => void;
  handlePauseResume: (connectorId: string) => Promise<void>;
  /** PATCH a connector with the given partial; revalidates SWR on success. */
  handleUpdate: (connectorId: string, patch: ConnectorEditPatch) => Promise<void>;
  /** DELETE a connector. Server rejects built-ins (cli/git_remote/agent); UI should hide the action for those. */
  handleDelete: (connectorId: string) => Promise<void>;
  /** Refresh both scopes + connectors. Used as the `onMutated` callback for the
   *  inline scope settings block (saving / rotating / etc.). Returning the
   *  resolved value keeps the caller's awaitable contract. */
  refresh: () => Promise<unknown>;
  /** Clear the active scope selection — the next render picks a new
   *  first-scope automatically. Used after the user deletes the active
   *  scope from the inline settings block. */
  clearScopeSelection: () => void;
}

export function useAccessData(projectId: string): UseAccessDataResult {
  const { data: scopes, error: scopesError, mutate: mutateScopes } = useSWR(
    projectId ? ['repo-scopes', projectId] : null,
    () => listScopes(projectId),
    { refreshInterval: 30000, revalidateOnFocus: false, dedupingInterval: 60000 },
  );
  const { data: connectors, error: connectorsError, mutate: mutateConnectors } = useSWR(
    projectId ? ['repo-connectors', projectId] : null,
    () => listAccessSurfaces(projectId),
    { refreshInterval: 30000, revalidateOnFocus: false, dedupingInterval: 60000 },
  );

  const [selectedTargetKey, setSelectedTargetKey] = useSessionValue('accessTarget');
  const [pendingConnectorIds, setPendingConnectorIds] = useState<ReadonlySet<string>>(() => new Set());

  // Management rows and targets come only from persisted Access resources.
  // Adapter inventories must not fabricate target ownership by matching paths.
  const accessConnectors = useMemo(
    () => (connectors ?? []).filter(isAccessSurface),
    [connectors],
  );

  // Bucket connectors by explicit repository target; inside each bucket sort built-ins
  // (cli, agent) first, then by created_at.
  //
  // When the AI Agent feature flag is off (see
  // `frontend/lib/featureFlags.ts`) we drop `agent` connectors at
  // this single chokepoint instead of asking every downstream
  // component (ScopeDetailPanel, ConnectorCard, ScopeSidebar, header
  // counts, etc.) to filter individually. The agent records still
  // exist server-side and the rest of the page (selection, pause,
  // delete) keeps the same shape — there are simply no agent rows
  // to surface while the feature is hidden.
  const connectorsByTarget = useMemo(() => {
    const m = new Map<string, AccessSurface[]>();
    accessConnectors.forEach((c) => {
      if (!AI_AGENT_ENABLED && isAgentProvider(c.kind)) return;
      const key = repositoryTargetKey(c.target);
      if (!m.has(key)) m.set(key, []);
      m.get(key)!.push(c);
    });
    for (const list of m.values()) {
      list.sort((a, b) => {
        const order = (c: AccessSurface) => getAccessProviderSortRank(c.kind);
        return order(a) - order(b) || (a.created_at ?? '').localeCompare(b.created_at ?? '');
      });
    }
    return m;
  }, [accessConnectors]);

  const repositoryViews = useMemo(
    () => [
      projectRootRepositoryView(projectId),
      ...(scopes ?? []).map(repositoryScopeView),
    ],
    [projectId, scopes],
  );

  // Target lifecycle is independent from Access Surface lifecycle. Keep the
  // Project root and every real Scope visible even before access is enabled.
  const sortedScopes = useMemo(() => {
    if (!scopes) return [];
    return repositoryViews
      .sort((a, b) => {
        if (a.target.kind === 'project_root' && b.target.kind !== 'project_root') return -1;
        if (a.target.kind !== 'project_root' && b.target.kind === 'project_root') return 1;
        return a.created_at.localeCompare(b.created_at);
      });
  }, [scopes, repositoryViews]);

  // Auto-select the first scope on first load / when the current
  // selection disappears.
  useEffect(() => {
    if (
      selectedTargetKey
      && sortedScopes.some((view) => repositoryViewKey(view) === selectedTargetKey)
    ) return;
    const first = sortedScopes[0];
    if (first) setSelectedTargetKey(repositoryViewKey(first));
  }, [sortedScopes, selectedTargetKey, setSelectedTargetKey]);

  const selectedScope = useMemo(
    () => sortedScopes.find((view) => repositoryViewKey(view) === selectedTargetKey) ?? sortedScopes[0],
    [sortedScopes, selectedTargetKey],
  );
  const selectedConnectors = useMemo(
    () => (selectedScope ? connectorsByTarget.get(repositoryViewKey(selectedScope)) ?? [] : []),
    [connectorsByTarget, selectedScope],
  );
  const representativeConnector = selectedConnectors[0];

  // Tiny helper — every async action below follows the same
  // "mark pending → run → revalidate → unmark pending" rhythm. Inlining
  // this three times read worse than naming the rhythm once.
  const withPending = useCallback(
    async (connectorId: string, fn: () => Promise<void>) => {
      setPendingConnectorIds((prev) => {
        if (prev.has(connectorId)) return prev;
        const next = new Set(prev);
        next.add(connectorId);
        return next;
      });
      try {
        await fn();
      } finally {
        setPendingConnectorIds((prev) => {
          if (!prev.has(connectorId)) return prev;
          const next = new Set(prev);
          next.delete(connectorId);
          return next;
        });
      }
    },
    [],
  );

  const handlePauseResume = useCallback(async (connectorId: string) => {
    await withPending(connectorId, async () => {
      try {
        const target = accessConnectors.find((c) => c.id === connectorId);
        if (!target) return;
        const isActive = target.status === 'active' || target.status === 'syncing';
        if (isActive) {
          await pauseAccessSurface(projectId, connectorId);
        } else {
          await resumeAccessSurface(projectId, connectorId);
        }
        await mutateConnectors();
      } catch (err) {
        console.error('Failed to toggle Access surface status:', err);
      }
    });
  }, [accessConnectors, projectId, mutateConnectors, withPending]);

  const handleUpdate = useCallback(
    async (connectorId: string, patch: ConnectorEditPatch) => {
      await withPending(connectorId, async () => {
        try {
          await updateAccessSurface(projectId, connectorId, patch);
          await mutateConnectors();
        } catch (err) {
          console.error('Failed to update connector:', err);
          // Re-throw so the caller (inline edit input) can surface a
          // local error state and revert the optimistic display.
          throw err;
        }
      });
    },
    [projectId, mutateConnectors, withPending],
  );

  const handleDelete = useCallback(
    async (connectorId: string) => {
      await withPending(connectorId, async () => {
        try {
          await deleteAccessSurface(projectId, connectorId);
          await mutateConnectors();
        } catch (err) {
          console.error('Failed to delete connector:', err);
          throw err;
        }
      });
    },
    [projectId, mutateConnectors, withPending],
  );

  const loadError = asError(scopesError) ?? asError(connectorsError);
  const loading = !loadError && (scopes === undefined || connectors === undefined);
  const noScopes = !loading && sortedScopes.length === 0;
  const allScopes = repositoryViews;

  // Joint refresh — scope edits (rename / mode / exclude) only touch
  // `repo-scopes`, but a delete cascades to connectors so we always
  // refresh both. Single function keeps the call-site contract small.
  const refresh = useCallback(async () => {
    await Promise.all([mutateScopes(), mutateConnectors()]);
  }, [mutateScopes, mutateConnectors]);

  // After the active scope is deleted from the inline settings block,
  // null the selection — the auto-select-first effect picks up an
  // adjacent scope on the next render so the user lands on something
  // meaningful instead of a dead detail pane.
  const clearScopeSelection = useCallback(() => {
    setSelectedTargetKey(null);
  }, [setSelectedTargetKey]);

  return {
    loading,
    loadError,
    noScopes,
    allScopes,
    sortedScopes,
    connectorsByTarget,
    selectedScope,
    selectedConnectors,
    representativeConnector,
    pendingConnectorIds,
    setSelectedTargetKey,
    handlePauseResume,
    handleUpdate,
    handleDelete,
    refresh,
    clearScopeSelection,
  };
}

function asError(value: unknown): Error | undefined {
  if (value instanceof Error) return value;
  if (value == null) return undefined;
  return new Error('Could not load access data.');
}
