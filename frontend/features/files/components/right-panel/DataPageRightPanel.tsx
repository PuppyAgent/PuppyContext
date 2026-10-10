'use client';

import { usePublishActiveFile } from '@/features/workspace/activeFile';

import type { SavedAgent } from '@/components/AgentRail';
import { DocumentEditor } from '@/components/RightAuxiliaryPanel/DocumentEditor';
import type { AccessOption } from '@/components/chat/ChatInputArea';
import { PageLoading } from '@/components/loading';
import { WorkspaceInspectorRegion } from '@/components/sidebar/WorkspaceInspectorRegion';
import type { SyncStatusSync } from '@/features/files/DataLayoutContext';
import { PanelShell } from '@/features/files/components/PanelShell';
import type { EndpointEntry, ProviderIconLookup } from '@/features/files/components/access-points';
import { useEditorSaveGuards } from '@/features/files/hooks/useEditorSaveGuards';
import type { PanelState } from '@/features/files/usePanelStore';
import { AI_AGENT_ENABLED } from '@/lib/featureFlags';
import {
  useEditorSaveSession,
  type EditorSaveNodeType,
} from '@/lib/hooks/useEditorSaveSession';
import type { Tool } from '@/lib/mcpApi';
import { getMcpEndpoint, type McpEndpoint } from '@/lib/mcpEndpointsApi';
import type { TableData } from '@/lib/projectsApi';
import {
  matchRepositoryViewForPath,
  repositoryViewKey,
  type AccessSurface,
  type RepoIdentity,
  type RepositoryView,
} from '@/lib/repoApi';
import {
  getSandboxEndpoint,
  type SandboxEndpoint,
} from '@/lib/sandboxEndpointsApi';
import dynamic from 'next/dynamic';
import { useCallback, useMemo } from 'react';
import useSWR from 'swr';

const PanelLoading = () => <PageLoading variant='fill' />;

const VersionHistoryPanel = dynamic(
  () =>
    import('@/components/editors/VersionHistoryPanel').then(m => ({
      default: m.VersionHistoryPanel,
    })),
  { ssr: false, loading: PanelLoading }
);
const SyncConfigPanel = dynamic(
  () =>
    import('@/features/files/components/SyncConfigPanel').then(m => ({ default: m.SyncConfigPanel })),
  { ssr: false, loading: PanelLoading }
);
const McpConfigPanel = dynamic(
  () => import('@/features/files/components/McpConfigPanel').then(m => ({ default: m.McpConfigPanel })),
  { ssr: false, loading: PanelLoading }
);
const SandboxConfigPanel = dynamic(
  () =>
    import('@/features/files/components/SandboxConfigPanel').then(m => ({
      default: m.SandboxConfigPanel,
    })),
  { ssr: false, loading: PanelLoading }
);
const ChatRuntimeView = dynamic(
  () =>
    import('@/features/agent/components/ChatRuntimeView').then(m => ({
      default: m.ChatRuntimeView,
    })),
  { ssr: false, loading: PanelLoading }
);

export interface EditorTarget {
  path: string;
  value: string;
}

export interface AccessPanelNavigationGuard {
  readonly canLeave: () => boolean;
}

interface DataPageRightPanelProps {
  readonly editorTarget: EditorTarget | null;
  readonly isEditorFullScreen: boolean;
  readonly panelState: PanelState;
  readonly projectId: string;
  readonly activeNodeId?: string;
  readonly activeSyncId: string | null;
  readonly currentTableData?: TableData;
  readonly syncStatusData: { bindings: SyncStatusSync[] } | undefined;
  readonly projectTools: Tool[];
  readonly savedAgents: SavedAgent[];
  readonly accessPointEntries: EndpointEntry[];
  readonly providerIcons: ProviderIconLookup;
  /** Redesign 2026-05-02: scope list for matching the current URL path. */
  readonly scopes: RepositoryView[];
  /** Redesign 2026-05-02: connectors indexed by scope_id. */
  readonly connectorsByTarget: Map<string, AccessSurface[]>;
  /** Redesign 2026-05-02: current canonical URL path (empty string for root). */
  readonly currentScopePath: string;
  /** Redesign 2026-05-02: project identity payload (URL + prompt_template + scope keys). */
  readonly repoIdentity: RepoIdentity | undefined;
  readonly onClose: () => void;
  onEditorClose: () => void;
  onEditorSave: (newValue: string) => Promise<void>;
  onToggleEditorFullScreen: () => void;
  onRollbackComplete: () => void;
  onSyncCreated: (nodeId: string, synchronizeBindingId?: string) => void | Promise<void>;
  onAccessPointHover: (nodeId: string | null) => void;
  /** Refresh scopes / connectors / repo identity after a scope CRUD
   *  mutation. Wired at page level to useDataLayout().mutateRepo, which
   *  is typed as `Promise<unknown>` because it forwards SWR's mutate()
   *  return value (we don't care about the resolved value, just the
   *  completion). */
  onScopeMutated: () => Promise<unknown>;
  onOpenPanel: (panel: PanelState) => void;
  onCreateAccessPoint: (folderPath: string | null | undefined) => void;
  onCreateIntegration: (scopePath?: string | null) => void;
  onOpenSyncSetting: (
    syncId: string,
    resource: {
      path: string;
      nodeName: string;
      nodeType: 'folder';
      readonly: boolean;
    }
  ) => void;
  onDataUpdate: () => Promise<void>;
  onAccessPanelNavigationGuardChange?: (
    guard: AccessPanelNavigationGuard | null
  ) => void;
}

export function DataPageRightPanel({
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
  accessPointEntries: _accessPointEntries,
  scopes,
  currentScopePath,
  repoIdentity,
  onClose,
  onEditorClose,
  onEditorSave,
  onToggleEditorFullScreen,
  onRollbackComplete,
  onSyncCreated,
  onOpenPanel,
  onCreateIntegration,
  onOpenSyncSetting,
  onDataUpdate,
}: DataPageRightPanelProps) {
  usePublishActiveFile({ tableId: activeNodeId, tableData: currentTableData?.data, onDataUpdate });
  const documentFilePath = editorTarget?.path ?? '';
  const documentNodeType = useMemo<EditorSaveNodeType>(() => {
    const lower = documentFilePath.toLowerCase();
    if (
      lower.endsWith('.md') ||
      lower.endsWith('.markdown') ||
      lower.endsWith('.mdx')
    )
      return 'markdown';
    if (
      lower.endsWith('.json') ||
      lower.endsWith('.json5') ||
      lower.endsWith('.jsonc')
    )
      return 'json';
    return 'file';
  }, [documentFilePath]);
  const saveDocumentContent = useCallback(
    async (content: string) => {
      await onEditorSave(content);
    },
    [onEditorSave]
  );
  const documentSession = useEditorSaveSession({
    projectId,
    filePath: documentFilePath,
    serverContent: editorTarget?.value ?? '',
    nodeType: documentNodeType,
    saveContent: saveDocumentContent,
    skipDraftRestore: editorTarget === null,
  });
  useEditorSaveGuards({
    dirty: documentSession.dirty,
    save: documentSession.save,
    keyboardEnabled: editorTarget !== null,
  });
  const closeDocumentEditor = useCallback(() => {
    if (
      documentSession.dirty &&
      typeof window !== 'undefined' &&
      !window.confirm(
        'You have unsaved changes. Close this editor and discard the local draft?'
      )
    ) {
      return;
    }
    onEditorClose();
  }, [documentSession.dirty, onEditorClose]);

  // For access_list, the panel always tracks the *current file-tree
  // folder* (one-way: file tree → panel) so the user's reading context
  // stays in sync with whatever scope they're navigating into.
  //
  // For all other panel types, fall back to the previous "snapshot
  // nodeId at open time" behaviour so version history / sync config /
  // agent chat keep their sticky context.
  const panelScopePath =
    panelState.type === 'access_list'
      ? currentScopePath
      : panelState.type !== 'version_history' && panelState.nodeId !== undefined
        ? panelState.nodeId
        : currentScopePath;

  // Scope context remains available for the older integration/config panels.
  // Project Access itself is owned by the persistent project auxiliary rail.
  const drilledScope =
    panelState.selectedTargetKey
      ? (scopes.find(
          s => repositoryViewKey(s) === panelState.selectedTargetKey
        ) ?? null)
      : null;
  const folderScope = matchRepositoryViewForPath(panelScopePath, scopes);
  const currentScope = drilledScope ?? folderScope;
  const syncConfigId =
    panelState.type === 'sync_config'
      ? (panelState.accessEndpointId ?? activeSyncId)
      : activeSyncId;
  const panelMcpId =
    panelState.type === 'mcp_config' ? panelState.mcpEndpointId : undefined;
  const { data: mcpEndpointDetail } = useSWR<McpEndpoint>(
    panelMcpId ? ['mcp-endpoint-detail', panelMcpId] : null,
    () => getMcpEndpoint(panelMcpId!),
    { revalidateOnFocus: false }
  );

  const panelSandboxId =
    panelState.type === 'sandbox_config'
      ? panelState.sandboxEndpointId
      : undefined;
  const { data: sandboxEndpointDetail } = useSWR<SandboxEndpoint>(
    panelSandboxId ? ['sandbox-endpoint-detail', panelSandboxId] : null,
    () => getSandboxEndpoint(panelSandboxId!),
    { revalidateOnFocus: false }
  );
  const backToAccessList = () =>
    onOpenPanel({ type: 'access_list', nodeId: panelScopePath });

  const isPageSheet = !editorTarget && panelState.type !== 'none';

  return (
    <WorkspaceInspectorRegion
      visible={!!editorTarget || (
        panelState.type !== 'none'
        && panelState.type !== 'workspace_chat'
        && panelState.type !== 'access_list'
      )}
      onClose={editorTarget ? closeDocumentEditor : onClose}
      background={isPageSheet ? 'var(--po-canvas)' : 'var(--po-panel)'}
    >
      {editorTarget && (
        <DocumentEditor
          path={editorTarget.path}
          value={documentSession.content}
          dirty={documentSession.dirty}
          saveStatus={documentSession.status}
          saveError={documentSession.error}
          onChange={documentSession.onChange}
          onSave={documentSession.save}
          onDiscard={documentSession.discard}
          onClose={closeDocumentEditor}
          isFullScreen={isEditorFullScreen}
          onToggleFullScreen={onToggleEditorFullScreen}
        />
      )}

      {!editorTarget &&
        panelState.type === 'version_history' &&
        panelState.nodeId && (
          <VersionHistoryPanel
            nodeId={panelState.nodeId}
            projectId={projectId}
            onClose={onClose}
            onRollbackComplete={onRollbackComplete}
          />
        )}

      {!editorTarget && panelState.type === 'sync_config' && syncConfigId && (
        <SyncConfigPanel
          mode='detail'
          syncId={syncConfigId}
          projectId={projectId}
          onClose={onClose}
          onBack={backToAccessList}
        />
      )}

      {!editorTarget && panelState.type === 'sync_config' && !activeSyncId && (
        <div
          style={{
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            height: '100%',
            gap: 12,
          }}
        >
          {!syncStatusData ? (
            <PageLoading variant='fill' />
          ) : (
            <>
              <span style={{ color: 'var(--po-text-disabled)', fontSize: 13 }}>
                No access configured
              </span>
              <button
                onClick={() => {
                  const nodeId = panelState.nodeId ?? panelScopePath;
                  const segs = nodeId.split('/').filter(Boolean);
                  onOpenSyncSetting('_generic', {
                    path: nodeId,
                    nodeName: segs.length > 0 ? segs[segs.length - 1] : 'Root',
                    nodeType: 'folder',
                    readonly: true,
                  });
                  onCreateIntegration(nodeId);
                }}
                style={{
                  height: 30,
                  padding: '0 14px',
                  fontSize: 12,
                  fontWeight: 500,
                  background: 'var(--po-control)',
                  border: '1px solid var(--po-border-strong)',
                  borderRadius: 6,
                  color: 'var(--po-text)',
                  cursor: 'pointer',
                }}
              >
                + New Integration
              </button>
            </>
          )}
        </div>
      )}

      {!editorTarget && panelState.type === 'sync_create' && (
        <SyncConfigPanel
          mode='create'
          syncId={null}
          projectId={projectId}
          onClose={onClose}
          onBack={backToAccessList}
          onSyncCreated={onSyncCreated}
          scopeBoundary={currentScope?.path}
          scopeBoundaryLabel={currentScope?.name}
          presetAgentType={panelState.agentTypePreselect}
        />
      )}

      {!editorTarget &&
        panelState.type === 'mcp_config' &&
        panelState.mcpEndpointId && (
          <McpConfigPanel
            endpoint={mcpEndpointDetail}
            onClose={onClose}
            onBack={backToAccessList}
          />
        )}

      {!editorTarget &&
        panelState.type === 'sandbox_config' &&
        panelState.sandboxEndpointId && (
          <SandboxConfigPanel
            endpoint={sandboxEndpointDetail}
            onClose={onClose}
            onBack={backToAccessList}
          />
        )}

      {/* agent_chat view — gated on the AI_AGENT_ENABLED feature flag.
          With the flag off, every entry point that opens this view
          (the AI Agent MethodCard's "Open chat" button, the access
          page's AgentBody, etc.) is also hidden, so this branch
          shouldn't be reachable through normal navigation. We still
          gate here defensively in case stale `panelState` from a
          previous session (or a hand-crafted URL) lands us with
          `type: 'agent_chat'` — under the flag we render nothing
          and the panel just collapses to its empty state. */}
      {AI_AGENT_ENABLED &&
        panelState.type === 'agent_chat' &&
        (() => {
          const agentId = panelState.agentId;
          const chatAgent = agentId
            ? savedAgents.find(agent => agent.id === agentId)
            : null;
          if (!chatAgent) {
            return !editorTarget ? (
              <PanelShell
                title='Chat Agent'
                onClose={onClose}
                onBack={backToAccessList}
              >
                <div
                  style={{
                    flex: 1,
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    color: 'var(--po-text-disabled)',
                    fontSize: 13,
                  }}
                >
                  Agent not found
                </div>
              </PanelShell>
            ) : null;
          }

          const tools: AccessOption[] = [];
          if (chatAgent.resources) {
            for (const res of chatAgent.resources) {
              tools.push({
                id: `bash:${res.path}`,
                label: `${res.nodeName || res.path} · Bash${res.readonly ? ' (Read-only)' : ''}`,
                type: 'bash' as const,
                tableId: res.path,
                tableName: res.nodeName || res.path,
              });
            }
          }

          return (
            <div style={{ display: editorTarget ? 'none' : 'contents' }}>
              <ChatRuntimeView
                active={!editorTarget}
                availableTools={tools}
                tableData={currentTableData?.data}
                tableId={activeNodeId}
                projectId={projectId}
                onDataUpdate={onDataUpdate}
                projectTools={projectTools}
                onClose={onClose}
                seamlessHeader
                onBack={backToAccessList}
              />
            </div>
          );
        })()}
    </WorkspaceInspectorRegion>
  );
}
