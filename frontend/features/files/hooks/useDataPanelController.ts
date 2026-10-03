'use client';


import {
  useCallback,
  useMemo,
  useState,
  type Dispatch,
  type SetStateAction,
} from 'react';

import { getApiBase } from '@/features/files/components/access-points/labels';
import type { AccessPanelNavigationGuard, EditorTarget } from '@/features/files/components/right-panel';
import { selectFilePanel, useProjectSession, useProjectSessionApi } from '@/features/workspace/session';
import { canonicalProjectGitUrl } from '@/lib/gitRemote';
import {
  matchRepositoryViewForPath,
  repositoryViewKey,
  type AccessSurface,
  type RepositoryView,
} from '@/lib/repoApi';

function normalizeAccessPath(path: string | null | undefined): string {
  return (path ?? '').trim().replace(/^\/+|\/+$/g, '').replace(/\/+/g, '/');
}

type SyncEndpointLike = {
  resourceKind: 'synchronize';
  id: string;
};

export function useDataPanelController({
  projectId,
  currentFolderId,
  effectiveNodeId,
  syncEndpoints,
  scopes,
  connectorsByTarget,
  mutateRepo,
  refreshAgents,
  setEditorTarget,
  setIsEditorFullScreen,
  setHoverHighlightNodeId,
}: {
  projectId: string;
  currentFolderId: string | null;
  effectiveNodeId: string;
  syncEndpoints: ReadonlyMap<string, SyncEndpointLike>;
  scopes: RepositoryView[];
  connectorsByTarget: Map<string, AccessSurface[]>;
  mutateRepo: () => Promise<unknown>;
  refreshAgents: () => Promise<unknown> | unknown;
  setEditorTarget: Dispatch<SetStateAction<EditorTarget | null>>;
  setIsEditorFullScreen: Dispatch<SetStateAction<boolean>>;
  setHoverHighlightNodeId: Dispatch<SetStateAction<string | null>>;
}) {
  const session = useProjectSessionApi();
  const panelState = useProjectSession(selectFilePanel);
  const openPanel = useProjectSession(state => state.openPanel);
  const closePanel = useProjectSession(state => state.closePanel);
  const [accessPanelNavigationGuard, setAccessPanelNavigationGuard] =
    useState<AccessPanelNavigationGuard | null>(null);
  const [accessOverviewOpen, setAccessOverviewOpen] = useState(false);
  const [quickAccessTargetKey, setQuickAccessTargetKey] = useState<string | null>(null);
  const [quickAccessScopeFallback, setQuickAccessScopeFallback] = useState<RepositoryView | null>(null);
  const [createAccessInitialPath, setCreateAccessInitialPath] = useState<string | null>(null);
  const [syncCreateInitialPath, setSyncCreateInitialPath] = useState<string | null>(null);

  const activeSyncNodeId = panelState.type === 'sync_config' ? panelState.nodeId ?? null : null;
  const selectedBinding = activeSyncNodeId !== null ? syncEndpoints.get(activeSyncNodeId) : undefined;
  const activeSyncId = panelState.type === 'sync_config'
    ? panelState.synchronizeBindingId ?? (selectedBinding?.resourceKind === 'synchronize' ? selectedBinding.id : null)
    : null;

  const rootScope = useMemo(() => matchRepositoryViewForPath('', scopes), [scopes]);
  const rootGitRemoteUrl = useMemo(() => {
    if (!rootScope) return null;
    return canonicalProjectGitUrl(getApiBase(), projectId);
  }, [projectId, rootScope]);

  const quickAccessScope = useMemo(() => {
    if (!quickAccessTargetKey) return null;
    return scopes.find((scope) => repositoryViewKey(scope) === quickAccessTargetKey)
      ?? quickAccessScopeFallback;
  }, [quickAccessScopeFallback, quickAccessTargetKey, scopes]);
  const quickAccessConnectors = useMemo(
    () => quickAccessScope ? connectorsByTarget.get(repositoryViewKey(quickAccessScope)) ?? [] : [],
    [connectorsByTarget, quickAccessScope],
  );

  const refreshRepoAndAgents = useCallback(async () => {
    await mutateRepo();
    await refreshAgents();
  }, [mutateRepo, refreshAgents]);

  const closeRightPanel = useCallback(() => {
    if (accessPanelNavigationGuard && !accessPanelNavigationGuard.canLeave()) return;
    setEditorTarget(null);
    setIsEditorFullScreen(false);
    closePanel();
  }, [accessPanelNavigationGuard, closePanel, setEditorTarget, setIsEditorFullScreen]);

  const openVersionHistoryPanel = useCallback(() => {
    if (!effectiveNodeId) return;
    setEditorTarget(null);
    setIsEditorFullScreen(false);
    openPanel({ type: 'version_history', nodeId: effectiveNodeId });
  }, [effectiveNodeId, openPanel, setEditorTarget, setIsEditorFullScreen]);

  const openSyncCreatePanel = useCallback((targetScopePath?: string | null) => {
    setEditorTarget(null);
    setIsEditorFullScreen(false);
    setSyncCreateInitialPath(normalizeAccessPath(targetScopePath ?? currentFolderId ?? ''));
  }, [currentFolderId, setEditorTarget, setIsEditorFullScreen]);

  const openCreateAccessModal = useCallback((folderPath: string | null | undefined) => {
    setEditorTarget(null);
    setIsEditorFullScreen(false);
    setAccessOverviewOpen(false);
    setQuickAccessTargetKey(null);
    setQuickAccessScopeFallback(null);
    setCreateAccessInitialPath(normalizeAccessPath(folderPath));
  }, [setEditorTarget, setIsEditorFullScreen]);

  const openQuickAccessModal = useCallback((scope: RepositoryView) => {
    setEditorTarget(null);
    setIsEditorFullScreen(false);
    setAccessOverviewOpen(false);
    setCreateAccessInitialPath(null);
    setQuickAccessScopeFallback(scope);
    setQuickAccessTargetKey(repositoryViewKey(scope));
  }, [setEditorTarget, setIsEditorFullScreen]);

  const openAccessOverviewModal = useCallback(() => {
    setEditorTarget(null);
    setIsEditorFullScreen(false);
    setQuickAccessTargetKey(null);
    setQuickAccessScopeFallback(null);
    setCreateAccessInitialPath(null);
    setAccessOverviewOpen(true);
    if (session.getState().panel.type === 'access_list') closePanel();
  }, [closePanel, session, setEditorTarget, setIsEditorFullScreen]);

  const openRootGitRemotePanel = useCallback(() => {
    setEditorTarget(null);
    setIsEditorFullScreen(false);
    setHoverHighlightNodeId(null);

    if (rootScope) {
      openQuickAccessModal(rootScope);
    } else {
      openCreateAccessModal(null);
    }
  }, [
    openCreateAccessModal,
    openQuickAccessModal,
    rootScope,
    setEditorTarget,
    setHoverHighlightNodeId,
    setIsEditorFullScreen,
  ]);

  const openShareWithAI = useCallback((folderPath: string | null | undefined) => {
    const normalizedPath = normalizeAccessPath(folderPath);
    const existingScope = matchRepositoryViewForPath(normalizedPath, scopes);
    if (existingScope) {
      setEditorTarget(null);
      setIsEditorFullScreen(false);
      openPanel({
        type: 'access_list',
        view: 'detail',
        selectedTargetKey: repositoryViewKey(existingScope),
      });
      return;
    }
    openCreateAccessModal(normalizedPath);
  }, [openCreateAccessModal, openPanel, scopes, setEditorTarget, setIsEditorFullScreen]);

  const closeAccessOverviewModal = useCallback(() => {
    setAccessOverviewOpen(false);
  }, []);

  const closeQuickAccessModal = useCallback(() => {
    setQuickAccessTargetKey(null);
    setQuickAccessScopeFallback(null);
  }, []);

  const closeCreateAccessModal = useCallback(() => {
    setCreateAccessInitialPath(null);
  }, []);

  const closeSyncCreateModal = useCallback(() => {
    setSyncCreateInitialPath(null);
  }, []);

  const handleDataAccessCreated = useCallback(async (scope: RepositoryView) => {
    try {
      await refreshRepoAndAgents();
    } finally {
      setQuickAccessScopeFallback(scope);
      setQuickAccessTargetKey(repositoryViewKey(scope));
    }
  }, [refreshRepoAndAgents]);

  const openAccessFullSettings = useCallback((targetKey: string) => {
    closeQuickAccessModal();
    openPanel({ type: 'access_list', view: 'settings', selectedTargetKey: targetKey });
  }, [closeQuickAccessModal, openPanel]);

  return {
    panelState,
    openPanel,
    closePanel,
    setAccessPanelNavigationGuard,
    activeSyncNodeId,
    activeSyncId,
    rootGitRemoteUrl,
    accessOverviewOpen,
    quickAccessScope,
    quickAccessConnectors,
    createAccessInitialPath,
    syncCreateInitialPath,
    refreshRepoAndAgents,
    closeRightPanel,
    openVersionHistoryPanel,
    openSyncCreatePanel,
    openRootGitRemotePanel,
    openShareWithAI,
    openAccessOverviewModal,
    openQuickAccessModal,
    openCreateAccessModal,
    closeAccessOverviewModal,
    closeQuickAccessModal,
    closeCreateAccessModal,
    closeSyncCreateModal,
    handleDataAccessCreated,
    openAccessFullSettings,
  };
}
