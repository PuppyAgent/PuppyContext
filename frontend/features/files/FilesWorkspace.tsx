'use client';

import { useWorkspaceRouter } from '@/features/workspace/navigation';

/**
 * Data Page - File/Folder Browser & Node Editor
 *
 * URL Format:
 *   /projects/{projectId}/data                    -> Project root (folder view)
 *   /projects/{projectId}/data/{folderId}         -> Folder view
 *   /projects/{projectId}/data/{folderId}/{nodeId} -> Node editor
 */

import type { BreadcrumbSegment } from '@/components/ProjectsHeader';
import { useOrganization } from '@/contexts/OrganizationContext';
import { useAuth } from '@/contexts/SupabaseAuthProvider';
import { useDataLayout } from '@/features/files/DataLayoutContext';
import {
  refreshAllContentNodes,
  useContentNodes,
  useProject,
  useProjects,
} from '@/lib/hooks/useData';
import { useSearchParams } from 'next/navigation';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { type McpToolPermissions } from '@/lib/mcpApi';

import { type AgentResource } from '@/features/files/components/views';
import { refreshProjects } from '@/lib/hooks/useData';

import { useAgent } from '@/contexts/AgentContext';
import { useOnboarding } from '@/lib/hooks/useOnboarding';

// Extracted hooks
import { useDataGridController } from '@/features/files/hooks/useDataGridController';
import { useDataPanelController } from '@/features/files/hooks/useDataPanelController';
import { useDataRouteController } from '@/features/files/hooks/useDataRouteController';
import { useDataViewPreferences } from '@/features/files/hooks/useDataViewPreferences';
import { useEditorSaveGuards } from '@/features/files/hooks/useEditorSaveGuards';
import { useEmptyProjectOpened } from '@/features/files/hooks/useEmptyProjectOpened';
import { useFileImport } from '@/features/files/hooks/useFileImport';
import { useNodeActions } from '@/features/files/hooks/useNodeActions';
import { useStructuredNodeData } from '@/features/files/hooks/useStructuredNodeData';
import {
  useEditorSaveSession,
  type EditorSaveNodeType,
} from '@/lib/hooks/useEditorSaveSession';
import { useExternalFileDropCatcher } from '@/lib/hooks/useExternalFileDropCatcher';
import { useProjectImportJobs } from '@/lib/hooks/useImportJobs';
import { isImportJobTerminal } from '@/lib/importApi';

// Extracted components
import { ProjectPageLoadingShell, SkeletonBlock } from '@/components/loading';
import { ProjectHeaderContribution } from '@/components/project/ProjectWorkspaceShell';
import { DataAccessModalHost } from '@/features/files/components/access-points/DataAccessModalHost';
import {
  DataHeaderActions,
  type DataHeaderActionTarget,
} from '@/features/files/components/DataHeaderActions';
import { DataSyncCreateModalHost } from '@/features/files/components/DataSyncCreateModalHost';
import { DataWorkspaceSurface } from '@/features/files/components/DataWorkspaceSurface';
import { FileViewerHeaderActions } from '@/features/files/components/FileViewerHeaderActions';
import { ProjectUnavailableShell } from '@/features/files/components/ProjectUnavailableShell';
import type { EditorTarget } from '@/features/files/components/right-panel';
import { useAccessPointEntries } from '@/features/files/hooks/useAccessPointEntries';
import { useDataCreateFlow } from '@/features/files/hooks/useDataCreateFlow';
import { writeFile } from '@/lib/contentTreeApi';
import { isTextLikeCategory, resolveFormat } from '@/lib/fileFormats';
import { projectAllows } from '@/lib/projectsApi';

export function FilesWorkspace({ projectId }: { projectId: string }) {
  const router = useWorkspaceRouter();
  const searchParams = useSearchParams();
  const { session, isAuthReady } = useAuth();
  const { currentOrg } = useOrganization();

  // Data fetching
  const {
    project: routeProject,
    isLoading: routeProjectLoading,
    error: routeProjectError,
  } = useProject(session ? projectId : null);
  const { projects } = useProjects(
    currentOrg?.id ?? null
  );

  // Project-level data from layout (sync status, tools, endpoints, scopes, connectors)
  const {
    syncStatusData,
    mutateSyncStatus,
    projectTools,
    syncEndpoints,
    nodeEndpointMap,
    scopes,
    connectorsByTarget,
    repoIdentity,
    repoIdentityLoading,
    repoIdentityError,
    mutateRepo,
  } = useDataLayout();

  // Agent context (needed early for syncEndpoints merge)
  const {
    draftResources,
    currentAgentId,
    savedAgents,
    hoveredAgentId,
    openSyncSetting,
    editingAgentId,
    selectedSyncId,
    selectedSyncNodeId,
    hoveredSyncNodeId,
    selectAgent,
    refreshAgents,
  } = useAgent();

  // Auto-complete onboarding steps
  const { completeStep } = useOnboarding();
  useEffect(() => {
    if (savedAgents.length > 0) completeStep('agent');
  }, [savedAgents.length]); // eslint-disable-line react-hooks/exhaustive-deps

  const {
    viewType,
    setViewType,
    editorType,
    setEditorType,
    htmlArtifactMode,
    setHtmlArtifactMode,
    csvViewMode,
    setCsvViewMode,
  } = useDataViewPreferences();

  // Legacy welcome query param — strip it without triggering old onboarding guide
  const hasWelcomeParam = searchParams?.get('welcome') === 'true';
  const hasSetupParam = searchParams?.get('setup') === 'true';
  useEffect(() => {
    if (hasWelcomeParam) {
      router.replace(`/projects/${projectId}/data`);
    }
  }, [hasWelcomeParam, projectId, router]);

  const [editorTarget, setEditorTarget] = useState<EditorTarget | null>(null);
  const [isEditorFullScreen, setIsEditorFullScreen] = useState(false);
  const [hoverHighlightNodeId, setHoverHighlightNodeId] = useState<
    string | null
  >(null);
  const { emptyProjectOpened, openEmptyProject: handleOpenEmptyProject } =
    useEmptyProjectOpened({ projectId, hasSetupParam });

  // ───── Custom Hooks ─────

  const {
    path: routePath,
    currentFolderId,
    folderBreadcrumbs,
    isResolvingPath,
    activeNodeId,
    activeNodeType,
    activePreviewType,
    activeMimeType,
    serverTextContent,
    isLoadingText,
    readError: fileReadError,
    retryRead: retryFileRead,
    markdownViewMode,
    setMarkdownViewMode,
    navigateTo,
  } = useDataRouteController({ projectId });

  const {
    nodes: contentNodes,
    isLoading: contentNodesLoading,
    error: contentNodesError,
    refresh: refreshCurrentNodes,
  } = useContentNodes(isResolvingPath ? '' : projectId, currentFolderId);
  const {
    activeJob: activeImportJob,
    latestJob: latestImportJob,
    refresh: refreshImportJobs,
    upsertJob: upsertImportJob,
  } = useProjectImportJobs(projectId);
  const seenTerminalImportJobsRef = useRef<Set<string>>(new Set());

  const activeFormat = useMemo(() => {
    if (!activeNodeId || activeNodeType === 'github') return null;
    return resolveFormat({ name: activeNodeId, mimeType: activeMimeType });
  }, [activeNodeId, activeNodeType, activeMimeType]);

  const activeTextSaveNodeType: EditorSaveNodeType =
    activeFormat?.id === 'markdown'
      ? 'markdown'
      : activeFormat?.id === 'json'
        ? 'json'
        : 'file';

  // Manual-save hook: editor edits stay local until the user hits
  // Cmd+S / clicks Save. Replaces the older 1.5s-debounced
  // auto-save which generated 100+ commits per editing session.
  // CLI / Git / external writes still go through their own code
  // paths and are unaffected.
  const editorSession = useEditorSaveSession({
    projectId,
    filePath: activeNodeId,
    serverContent: serverTextContent,
    isContentReady: !isLoadingText && !isResolvingPath && !fileReadError,
    nodeType: activeTextSaveNodeType,
  });
  const {
    content: editorTextDraft,
    onChange: onEditorTextChange,
    status: editorSaveStatus,
    save: saveEditor,
    dirty: editorDirty,
  } = editorSession;

  useEditorSaveGuards({
    dirty: editorDirty,
    save: saveEditor,
    keyboardEnabled: editorTarget === null,
  });

  const nodeActions = useNodeActions(projectId, currentFolderId);
  const fileImport = useFileImport(projectId, session?.access_token, {
    showToast: nodeActions.showToast,
  });

  // Page-wide safety net for external file drops. Without this, a
  // file dropped on the content area / right-panel / gap between
  // zones triggers the browser-default "open file in this tab"
  // behaviour — the user's session vanishes and the file appears
  // not to upload at all.
  //
  // The catcher runs silently (no full-page overlay): the explorer
  // sidebar already has its own per-row drop highlighting, and
  // overlaying a banner across the whole page on top of that read
  // as duplicated/confusing UI. So:
  //   - inside sidebar  → sidebar's own per-folder UI takes over
  //                       (its handlers stopPropagation/preventDefault
  //                       before this fallback ever runs)
  //   - outside sidebar → no visual cue, but on drop we still route
  //                       to the current folder so the file isn't
  //                       silently lost to a browser-default tab nav
  const externalDropTarget = useMemo(
    () =>
      currentFolderId
        ? {
            path: currentFolderId,
            name: folderBreadcrumbs.at(-1)?.name ?? 'Folder',
          }
        : { path: null, name: 'Root' },
    [currentFolderId, folderBreadcrumbs]
  );
  useExternalFileDropCatcher({
    onDrop: files => {
      fileImport.openFileImportForTarget(files, externalDropTarget);
    },
  });

  // Derive active node info (single source of truth for editor context).
  const effectiveNodeId = activeNodeId;

  const {
    panelState,
    openPanel,
    closePanel,
    setAccessPanelNavigationGuard,
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
    openQuickAccessModal,
    openCreateAccessModal,
    closeAccessOverviewModal,
    closeQuickAccessModal,
    closeCreateAccessModal,
    closeSyncCreateModal,
    handleDataAccessCreated,
    openAccessFullSettings,
  } = useDataPanelController({
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
  });

  const handleSyncCreated = useCallback(
    async (nodeId: string, synchronizeBindingId?: string) => {
      await mutateSyncStatus();
      refreshCurrentNodes();
      openPanel({ type: 'sync_config', nodeId, synchronizeBindingId });
    },
    [mutateSyncStatus, refreshCurrentNodes, openPanel]
  );

  const handleSyncCreatedInModal = useCallback(async () => {
    await mutateSyncStatus();
    await mutateRepo();
    refreshCurrentNodes();
    closeSyncCreateModal();
  }, [closeSyncCreateModal, mutateRepo, mutateSyncStatus, refreshCurrentNodes]);

  const {
    tableTools,
    currentTableData,
    refreshTable,
    shouldLoadStructuredTableData,
    activeNodeDisplayName,
    accessPoints,
    setAccessPoints,
    configuredAccessPoints,
    tableNameById,
    syncToolsForPath,
    deleteAllToolsForPath,
    refreshToolsForActiveNode,
  } = useStructuredNodeData({
    projectId,
    activeNodeId,
    activeNodeType,
    activeFormatDefaultViewer: activeFormat?.defaultViewer,
    contentNodes,
  });

  // Dialog states
  const [createFolderOpen, setCreateFolderOpen] = useState(false);

  // Supabase connector
  const [supabaseConnectOpen, setSupabaseConnectOpen] = useState(false);
  const [supabaseSQLEditorOpen, setSupabaseSQLEditorOpen] = useState(false);
  const [importDatabaseSourceId, setImportDatabaseSourceId] = useState<
    string | null
  >(null);

  const {
    createTableOpen,
    defaultStartOption,
    createMenuOpen,
    createMenuOpenForId,
    createMenuOpenAction,
    createMenuPosition,
    createMenuAccessOnly,
    createMenuRef,
    createMenuActions,
    highlightNodeId,
    handleCreateClick,
    handleMillerCreateClick,
    closeCreateTable,
  } = useDataCreateFlow({
    projectId,
    currentFolderId,
    navigateTo: navigateTo,
    openSyncCreatePanel,
    openSyncSetting,
    openFilePickerForTarget: fileImport.openFilePickerForTarget,
    openFileImportDialogForTarget: fileImport.openFileImportDialogForTarget,
    showToast: nodeActions.showToast,
  });

  const agentResources: AgentResource[] = useMemo(() => {
    const toAgentResource = (r: { path: string; readonly?: boolean }) => ({
      path: r.path,
      readonly: r.readonly ?? true,
    });

    if (hoveredSyncNodeId) return [{ path: hoveredSyncNodeId, readonly: true }];
    if (hoveredAgentId) {
      const agent = savedAgents.find(a => a.id === hoveredAgentId);
      if (agent?.resources && agent.resources.length > 0)
        return agent.resources.map(toAgentResource);
    }
    if (panelState.type === 'sync_create' || editingAgentId)
      return draftResources.map(toAgentResource);
    if (currentAgentId) {
      const agent = savedAgents.find(a => a.id === currentAgentId);
      if (agent?.resources && agent.resources.length > 0)
        return agent.resources.map(toAgentResource);
    }
    if (selectedSyncId && selectedSyncNodeId) {
      return [{ path: selectedSyncNodeId, readonly: true }];
    }
    return [];
  }, [
    draftResources,
    editingAgentId,
    currentAgentId,
    savedAgents,
    hoveredAgentId,
    selectedSyncId,
    selectedSyncNodeId,
    hoveredSyncNodeId,
    panelState.type,
  ]);

  const activeProject = useMemo(
    () => projects.find(p => p.id === projectId) ?? routeProject ?? null,
    [projects, projectId, routeProject]
  );

  const scopedProjects = useMemo(() => {
    const projectsForCurrentRoute =
      routeProject?.org_id && currentOrg?.id !== routeProject.org_id
        ? []
        : projects;
    if (
      !routeProject ||
      projectsForCurrentRoute.some(p => p.id === routeProject.id)
    ) {
      return projectsForCurrentRoute;
    }
    return [routeProject, ...projectsForCurrentRoute];
  }, [currentOrg?.id, projects, routeProject]);

  // ───── Effects ─────

  useEffect(() => {
    if (panelState.type === 'agent_chat' && panelState.agentId) {
      if (currentAgentId !== panelState.agentId) {
        selectAgent(panelState.agentId);
      }
    }
  }, [panelState.type, panelState.agentId, currentAgentId, selectAgent]);

  // Refresh on external events (SaaS sync, ETL, etc.)
  useEffect(() => {
    const handler = () => {
      refreshAllContentNodes(projectId);
      refreshProjects(currentOrg?.id ?? null);
    };
    window.addEventListener('saas-task-completed', handler);
    window.addEventListener('etl-task-completed', handler);
    return () => {
      window.removeEventListener('saas-task-completed', handler);
      window.removeEventListener('etl-task-completed', handler);
    };
  }, [currentOrg?.id, projectId]);

  const { accessPointEntries, providerIcons } = useAccessPointEntries({
    nodeEndpointMap,
    savedAgents,
    tableNameById,
    syncStatusData,
  });

  const {
    items,
    gridSelection,
    bulkDeleteOpen,
    setBulkDeleteOpen,
    bulkDeletePaths,
    bulkDeleteSubmitting,
    openBulkDeleteDialog,
    handleBulkDeleteConfirm,
    platformDeleteHint,
    handleMillerNavigate,
    handleRefresh,
  } = useDataGridController({
    contentNodes,
    currentFolderId,
    navigateTo: navigateTo,
    handleBulkDelete: nodeActions.handleBulkDelete,
    refresh: refreshCurrentNodes,
  });

  // ───── Breadcrumbs ─────
  // Text-only segments. Per design: the page header is just the
  // address line — no project box, no folder/markdown/file glyphs,
  // no per-segment color tinting. Type-specific iconography stays in
  // the file tree (where it's functional for scanning), the header
  // stays quiet so the user's eye doesn't compete with the workspace
  // chip on the sidebar.

  const pathSegments = useMemo<BreadcrumbSegment[]>(() => {
    const segments: BreadcrumbSegment[] = [];
    const projectName = activeProject?.name ?? (
      <SkeletonBlock width={120} height={10} radius={3} />
    );
    const hasSubContent =
      routePath.length > 0 || currentFolderId || activeNodeId;
    segments.push({
      label: projectName,
      href: hasSubContent ? `/projects/${projectId}/data` : undefined,
    });

    if (
      isResolvingPath &&
      routePath.length > 0 &&
      folderBreadcrumbs.length === 0
    ) {
      routePath.forEach(() => {
        segments.push({
          label: <SkeletonBlock width={72} height={10} radius={3} />,
        });
      });
    } else {
      folderBreadcrumbs.forEach((folder, index) => {
        const isLast = index === folderBreadcrumbs.length - 1;
        // folder.id is the full path up to this folder segment
        const folderUrlPath = folder.id
          .split('/')
          .filter(Boolean)
          .map(s => encodeURIComponent(s))
          .join('/');
        segments.push({
          label: folder.name,
          href:
            !isLast || activeNodeId
              ? `/projects/${projectId}/data/${folderUrlPath}`
              : undefined,

        });
      });
      if (activeNodeId) {
        segments.push({
          label: currentTableData?.name ?? activeNodeDisplayName,
        });
      }
    }
    return segments;
  }, [
    activeProject,
    projectId,
    folderBreadcrumbs,
    currentFolderId,
    activeNodeId,
    activeNodeDisplayName,
    currentTableData?.name,
    isResolvingPath,
    routePath,
    navigateTo,
  ]);

  const activeNodeListing = useMemo(
    () => contentNodes.find(node => node.path === activeNodeId),
    [activeNodeId, contentNodes]
  );

  const headerActionTarget = useMemo<DataHeaderActionTarget | null>(() => {
    if (!activeNodeId) return null;

    return {
      id: activeNodeId,
      name:
        currentTableData?.name ??
        activeNodeListing?.name ??
        activeNodeDisplayName,
      type: activeNodeType || activeNodeListing?.type || 'file',
      isFolder: false,
      isRoot: false,
      isSynced: activeNodeListing?.is_synced,
    };
  }, [
    activeNodeDisplayName,
    activeNodeId,
    activeNodeListing?.is_synced,
    activeNodeListing?.name,
    activeNodeListing?.type,
    activeNodeType,
    currentTableData?.name,
  ]);

  const activeUsesEditorSaveSession = Boolean(
    activeFormat?.editable && isTextLikeCategory(activeFormat)
  );

  const headerCommandMenu = headerActionTarget ? (
    <DataHeaderActions
      target={headerActionTarget}
      onRename={nodeActions.handleRename}
      onDelete={nodeActions.handleDelete}
      onDownload={nodeActions.handleDownload}
    />
  ) : undefined;

  const headerActionSlot = activeFormat ? (
    <FileViewerHeaderActions
      projectId={activeProject?.id ?? projectId}
      filePath={activeNodeId}
      viewerId={activeFormat.defaultViewer}
      editable={activeFormat.editable}
      markdownViewMode={markdownViewMode}
      onMarkdownViewModeChange={setMarkdownViewMode}
      saveStatus={activeUsesEditorSaveSession ? editorSaveStatus : 'clean'}
      onSave={saveEditor}
      editorType={editorType}
      onEditorTypeChange={setEditorType}
      htmlMode={htmlArtifactMode}
      onHtmlModeChange={setHtmlArtifactMode}
      csvViewMode={csvViewMode}
      onCsvViewModeChange={setCsvViewMode}
      actionsSlot={headerCommandMenu}
    />
  ) : (
    headerCommandMenu
  );

  // View logic flags
  const isEditorView = !!activeNodeId;
  const isFolderView = !activeNodeId;
  const isLoading = isResolvingPath || contentNodesLoading;
  const isProjectIdentityLoading =
    !isAuthReady || (!activeProject && routeProjectLoading);
  const isProjectIdentityReady = Boolean(activeProject?.name);
  const projectIdentityError =
    !routeProjectLoading && !activeProject && routeProjectError
      ? routeProjectError
      : null;
  const shouldBlockForProjectIdentity =
    !projectIdentityError &&
    !isProjectIdentityReady &&
    isProjectIdentityLoading;
  const isRootFolderView = isFolderView && !currentFolderId;
  const hasRootItems = items.length > 0;
  const noFileSelectedMode =
    items.length === 0
      ? isRootFolderView
        ? 'empty-workspace'
        : 'empty-folder'
      : 'has-content';
  const currentFolderName = folderBreadcrumbs.at(-1)?.name;
  const projectHasContentCommit = repoIdentity?.content_initialized === true;
  const latestFailedImportJob =
    latestImportJob?.status === 'failed' ? latestImportJob : null;
  const latestEmptyImportJob = activeImportJob || latestFailedImportJob;
  const shouldSurfaceEmptyImportJob =
    Boolean(latestEmptyImportJob) &&
    items.length === 0 &&
    !projectHasContentCommit;
  const isRootEmptyDecisionLoading =
    isRootFolderView &&
    (isProjectIdentityLoading ||
      isLoading ||
      (!hasRootItems && repoIdentityLoading));
  const showEmptyWorkspace =
    isRootFolderView &&
    !fileReadError && !contentNodesError && !repoIdentityError &&
    !isRootEmptyDecisionLoading &&
    (shouldSurfaceEmptyImportJob ||
      (items.length === 0 && !projectHasContentCommit && !emptyProjectOpened));
  const suppressExplorerSidebar =
    showEmptyWorkspace;

  useEffect(() => {
    const job = latestImportJob;
    if (!job || !isImportJobTerminal(job.status)) return;
    if (seenTerminalImportJobsRef.current.has(job.id)) return;
    seenTerminalImportJobsRef.current.add(job.id);
    if (job.status === 'completed') {
      void mutateSyncStatus();
      void mutateRepo();
      refreshCurrentNodes();
      window.dispatchEvent(
        new CustomEvent('import-job-completed', {
          detail: { jobId: job.id, projectId },
        })
      );
    }
  }, [
    latestImportJob,
    mutateRepo,
    mutateSyncStatus,
    projectId,
    refreshCurrentNodes,
  ]);

  // ───── Render ─────

  if (shouldBlockForProjectIdentity) {
    return <ProjectPageLoadingShell />;
  }

  if (projectIdentityError) {
    return <ProjectUnavailableShell onBackHome={() => router.push('/home')} />;
  }

  const dialogsProps = {
    projectId,
    currentFolderId,
    projects: scopedProjects,
    activeProject,
    renameDialogOpen: nodeActions.renameDialogOpen,
    renameTargetName: nodeActions.renameTarget?.name ?? '',
    renameError: nodeActions.renameError,
    onCloseRename: nodeActions.closeRenameDialog,
    onRenameConfirm: nodeActions.handleRenameConfirm,
    moveDialogTarget: nodeActions.moveDialogTarget,
    moveConfirmTarget: nodeActions.moveConfirmTarget,
    onMoveConfirm: async (nodeId: string, targetFolderId: string | null) => {
      nodeActions.setMoveDialogTarget(null);
      await nodeActions.handleMoveNode(nodeId, targetFolderId);
    },
    onMoveFinalConfirm: nodeActions.handleMoveConfirm,
    onCloseMove: () => nodeActions.setMoveDialogTarget(null),
    onCloseMoveConfirm: nodeActions.closeMoveConfirm,
    deleteDialogTarget: nodeActions.deleteDialogTarget,
    onDeleteConfirm: nodeActions.handleDeleteConfirm,
    onCloseDelete: nodeActions.closeDeleteDialog,
    createTableOpen,
    onCloseCreateTable: closeCreateTable,
    defaultStartOption,
    createFolderOpen,
    onCloseFolderDialog: () => setCreateFolderOpen(false),
    onFolderSuccess: () => refreshAllContentNodes(projectId),
    supabaseConnectOpen,
    onCloseSupabaseConnect: () => setSupabaseConnectOpen(false),
    onSupabaseConnected: (sourceId: string) => {
      setSupabaseConnectOpen(false);
      setImportDatabaseSourceId(sourceId);
      setSupabaseSQLEditorOpen(true);
    },
    supabaseSQLEditorOpen,
    importDatabaseSourceId,
    onCloseSupabaseSQLEditor: () => {
      setSupabaseSQLEditorOpen(false);
      setImportDatabaseSourceId(null);
    },
    onSupabaseSaved: () => refreshAllContentNodes(projectId),
    fileImportDialogOpen: fileImport.fileImportDialogOpen,
    onCloseFileImport: fileImport.closeFileImportDialog,
    onFileImportConfirm: fileImport.handleFileImportConfirm,
    droppedFiles: fileImport.droppedFiles,
    fileImportTargetLabel: fileImport.fileImportTarget.name,
    filePickerInputRef: fileImport.filePickerInputRef,
    folderPickerInputRef: fileImport.folderPickerInputRef,
    onFilePickerChange: fileImport.handleFilePickerChange,
    onFolderPickerChange: fileImport.handleFolderPickerChange,
  };

  const editorAreaProps = activeProject
    ? {
        activeNodeId,
        activeNodeType,
        activeMimeType,
        activeProject,
        currentTableData,
        textContent: editorTextDraft,
        isLoadingText,
        markdownViewMode,
        onTextChange: onEditorTextChange,
        setMarkdownViewMode,
        editorType,
        htmlArtifactMode,
        csvViewMode,
        configuredAccessPoints,
        onActiveTableChange: (nodePath: string) => {
          navigateTo(nodePath.split('/').filter(Boolean));
        },
        onAccessPointChange: (
          apPath: string,
          permissions: McpToolPermissions
        ) => {
          const hasAnyPermission = Object.values(permissions).some(Boolean);
          setAccessPoints(prev => {
            const existing = prev.find(ap => ap.path === apPath);
            if (existing) {
              if (!hasAnyPermission)
                return prev.filter(ap => ap.path !== apPath);
              return prev.map(ap =>
                ap.path === apPath ? { ...ap, permissions } : ap
              );
            }
            if (hasAnyPermission) {
              return [
                ...prev,
                { id: `ap-${Date.now()}`, path: apPath, permissions },
              ];
            }
            return prev;
          });
          if (activeNodeId) {
            syncToolsForPath({
              versionPath: activeNodeId,
              path: apPath,
              permissions,
              existingTools: tableTools as any,
            }).then(() => {
              refreshToolsForActiveNode();
            });
          }
        },
        onAccessPointRemove: (apPath: string) => {
          setAccessPoints(prev => prev.filter(ap => ap.path !== apPath));
          if (activeNodeId) {
            deleteAllToolsForPath({
              versionPath: activeNodeId,
              path: apPath,
              existingTools: tableTools as any,
            }).then(() => {
              refreshToolsForActiveNode();
            });
          }
        },
        onOpenDocument: (docPath: string, value: string) => {
          setEditorTarget({ path: docPath, value });
          setIsEditorFullScreen(false);
          closePanel();
        },
        onCreateTool: (path: string) => {
          if (!activeNodeId) return;
          nodeActions.handleCreateTool(
            activeNodeId,
            `${currentTableData?.name || activeNodeDisplayName || 'File'}`,
            'json',
            path
          );
        },
      }
    : null;

  return (
    <>
      <ProjectHeaderContribution
        canManageSettings={projectAllows(activeProject, 'project.settings.manage')}
        pathSegments={pathSegments}
        actions={headerActionSlot}
      />
      <DataWorkspaceSurface
      dialogsProps={dialogsProps}
      overlaysProps={{
        toast: nodeActions.toast,
        createMenuOpen,
        createMenuPosition,
        createMenuAccessOnly,
        createMenuRef,
        createMenuActions,
      }}
      bulkDeleteProps={{
        open: bulkDeleteOpen,
        paths: bulkDeletePaths,
        onClose: () => {
          if (!bulkDeleteSubmitting) setBulkDeleteOpen(false);
        },
        onConfirm: handleBulkDeleteConfirm,
      }}
      selectionProps={{
        count: gridSelection.selectedCount,
        onClear: gridSelection.clear,
        onDelete: openBulkDeleteDialog,
        busy: bulkDeleteSubmitting,
        shortcutHint: platformDeleteHint,
      }}
      explorer={{
        hidden: suppressExplorerSidebar,
        props: {
          projectId,
          folderBreadcrumbs,
          activeNodeId: activeNodeId || undefined,
          onNavigate: handleMillerNavigate,
          onCreate: handleMillerCreateClick,
          onCreateSync: (_event, nodeId) => openShareWithAI(nodeId),
          onOpenAccess: (_endpoints, nodeId) => openShareWithAI(nodeId),
          endpointByNodeId: nodeEndpointMap,
          onRename: nodeActions.handleRename,
          onDelete: nodeActions.handleDelete,
          onDownload: nodeActions.handleDownload,
          onFilesDrop: fileImport.openFileImportForTarget,
          onMoveNode: nodeActions.handleMoveNode,
          activeSyncNodeId:
            panelState.type === 'sync_config' ||
            panelState.type === 'agent_chat' ||
            panelState.type === 'workspace_chat' ||
            panelState.type === 'mcp_config' ||
            panelState.type === 'sandbox_config'
              ? (panelState.nodeId ?? null)
              : null,
          highlightNodeId,
          hoverHighlightNodeId,
          createMenuOpenForId,
          createMenuOpenAction,
        },
      }}
      content={{
        isResolvingPath,
        fileReadError,
        retryFileRead,
        isEditorView,
        isProjectIdentityLoading,
        editorAreaProps,
        isFolderView,
        isRootEmptyDecisionLoading,
        isLoading,
        readError: contentNodesError || (isRootFolderView && !hasRootItems && repoIdentityError),
        onRetryRead: () => { void Promise.all([refreshCurrentNodes(), mutateRepo()]).catch(() => {}); },
        showEmptyWorkspace,
        suppressExplorerSidebar,
        emptyWorkspaceProps: {
          project: activeProject,
          gitRemoteUrl: rootGitRemoteUrl,
          onOpenGitSetup: openRootGitRemotePanel,
          onImportFiles: createMenuActions.onImportFromFiles,
          onFilesDrop: (files: File[]) => {
            fileImport.openFileImportForTarget(files, {
              path: null,
              name: 'Root',
            });
          },
          importJob: latestEmptyImportJob,
          onImportJobCreated: async job => {
            await upsertImportJob(job);
            await refreshImportJobs();
          },
          onOpenEmptyProject: handleOpenEmptyProject,
        },
        noFileSelectedProps: {
          mode: noFileSelectedMode,
          folderName: currentFolderName,
          onCreateMarkdown: createMenuActions.onCreateBlankMarkdown,
          onUploadClick: createMenuActions.onImportFromFiles,
        },
        gridViewProps: {
          items,
          parentFolderId: currentFolderId,
          onCreateClick: handleCreateClick,
          onRename: nodeActions.handleRename,
          onDelete: nodeActions.handleDelete,
          onRefresh: handleRefresh,
          onMove: nodeActions.handleMoveRequest,
          onMoveNode: nodeActions.handleMoveNode,
          onCreateTool: nodeActions.handleCreateTool,
          onShareWithAI: openShareWithAI,
          agentResources,
          highlightNodeId: hoverHighlightNodeId || highlightNodeId,
          selectedIds: gridSelection.selectedIds,
          onToggleSelected: gridSelection.toggle,
          onRangeSelectTo: gridSelection.selectRangeTo,
          onSelectOnly: gridSelection.selectOnly,
          onClearSelection: gridSelection.clear,
        },
      }}
      rightPanelProps={{
        editorTarget,
        isEditorFullScreen,
        panelState,
        projectId,
        activeNodeId,
        activeSyncId,
        currentTableData,
        syncStatusData,
        projectTools,
        savedAgents,
        accessPointEntries,
        providerIcons,
        scopes,
        connectorsByTarget,
        currentScopePath: currentFolderId || '',
        repoIdentity,
        onClose: closeRightPanel,
        onEditorClose: () => {
          setEditorTarget(null);
          setIsEditorFullScreen(false);
        },
        onEditorSave: async newValue => {
          const target = editorTarget;
          if (!target?.path) return;
          // Persist through the same explicit-save contract as the main editor.
          const lower = target.path.toLowerCase();
          const nodeType =
            lower.endsWith('.md') ||
            lower.endsWith('.markdown') ||
            lower.endsWith('.mdx')
              ? 'markdown'
              : lower.endsWith('.json') ||
                  lower.endsWith('.json5') ||
                  lower.endsWith('.jsonc')
                ? 'json'
                : 'file';
          try {
            await writeFile(projectId, target.path, newValue, nodeType);
            // Reflect the saved value so the editor's dirty state resets and the
            // panel stays open in preview (do NOT close on every save).
            setEditorTarget({ path: target.path, value: newValue });
            refreshCurrentNodes();
          } catch (e) {
            nodeActions.showToast?.(
              `Save failed: ${e instanceof Error ? e.message : String(e)}`,
              'error'
            );
            throw e;
          }
        },
        onToggleEditorFullScreen: () =>
          setIsEditorFullScreen(!isEditorFullScreen),
        onRollbackComplete: () => {
          if (shouldLoadStructuredTableData) refreshTable();
          refreshCurrentNodes();
        },
        onSyncCreated: handleSyncCreated,
        onAccessPointHover: setHoverHighlightNodeId,
        onScopeMutated: refreshRepoAndAgents,
        onOpenPanel: openPanel,
        onCreateAccessPoint: openCreateAccessModal,
        onCreateIntegration: openSyncCreatePanel,
        onOpenSyncSetting: openSyncSetting,
        onDataUpdate: async () => {
          if (shouldLoadStructuredTableData) await refreshTable();
        },
        onAccessPanelNavigationGuardChange: setAccessPanelNavigationGuard,
      }}
      accessModalSlot={
        <>
          <DataAccessModalHost
            projectId={projectId}
            accessOverviewOpen={accessOverviewOpen}
            quickAccessScope={quickAccessScope}
            quickAccessConnectors={quickAccessConnectors}
            createAccessInitialPath={createAccessInitialPath}
            existingScopes={scopes}
            connectorsByTarget={connectorsByTarget}
            providerIcons={providerIcons}
            onCloseAccessOverview={closeAccessOverviewModal}
            onOpenExistingAccess={openQuickAccessModal}
            onCloseQuickAccess={closeQuickAccessModal}
            onCreateAccess={openCreateAccessModal}
            onOpenFullSettings={openAccessFullSettings}
            onCloseCreateAccess={closeCreateAccessModal}
            onCreated={handleDataAccessCreated}
          />
          <DataSyncCreateModalHost
            projectId={projectId}
            initialPath={syncCreateInitialPath}
            onClose={closeSyncCreateModal}
            onSyncCreated={handleSyncCreatedInModal}
          />
        </>
      }
      />
    </>
  );
}
