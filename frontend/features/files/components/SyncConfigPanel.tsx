'use client';

import { SyncDetailView } from '@/components/agent/views/SyncDetailView';
import { ChatAgentConfig, type AgentConfigProps } from '@/components/agent/views/configs/ChatAgentConfig';
import { SaaSyncConfig, type SaaSConfigField } from '@/components/agent/views/configs/SaaSyncConfig';
import type { AcceptedNodeType } from '@/components/agent/views/configs/SyncPreview';
import { SyncPreview } from '@/components/agent/views/configs/SyncPreview';
import { useAgent } from '@/contexts/AgentContext';
import { PanelShell } from '@/features/files/components/PanelShell';
import { GithubIntegrationPanel } from '@/features/files/components/github-integration/GithubIntegrationPanel';
import { useProjectSession } from '@/features/workspace/session';
import { useConnectorSpecs } from '@/lib/hooks/useData';
import type { SaasType } from '@/lib/oauthApi';
import { resolveProviderIconUrl } from '@/lib/providerIcons';
import { createSynchronizeBinding } from '@/lib/synchronizeApi';
import React, { useCallback, useEffect, useMemo, useState } from 'react';

/* ================================================================
   Types
   ================================================================ */

type AgentTypeId = 'chat';

interface SyncProviderDef {
  id: string;
  label: string;
  description: string;
  icon: React.ReactNode;
  oauthType?: SaasType;
  requiresAuth: boolean;
  creationMode: 'direct' | 'bootstrap';
  direction: 'inbound' | 'outbound' | 'bidirectional';
  accept: AcceptedNodeType[];
  configFields: SaaSConfigField[];
}

type EndpointTypeId = 'mcp' | 'sandbox';

interface EndpointOptionDef {
  id: EndpointTypeId;
  label: string;
  description: string;
  icon: React.ReactNode;
}

/* ================================================================
   Provider & Agent Definitions
   ================================================================ */

// Git Remote and FS CLI are built-in Access surfaces managed from the
// scope detail. This picker only creates user-added endpoints and SaaS
// connections.

const ENDPOINT_OPTIONS: EndpointOptionDef[] = [
  { id: 'mcp', label: 'MCP Server', description: 'Model Context Protocol endpoint', icon: <McpMini /> },
  { id: 'sandbox', label: 'Sandbox', description: 'Isolated script execution environment', icon: <SandboxMini /> },
];

// Providers whose backend code exists but isn't production-ready yet.
// Rendered as disabled "Coming soon" rows; remove an id here to re-enable.
//
// GitHub in this picker is a durable project/repository branch binding.
// One-time GitHub repository snapshots live only in the empty-workspace
// Import flow under projects/components/imports/github.
const COMING_SOON_PROVIDERS: ReadonlySet<string> = new Set([
  'google_search_console',
]);

/* ================================================================
   Mini Icon Components
   ================================================================ */

function ProviderImg({ src }: { src: string }) {
  return <img src={src} alt="" width={16} height={16} style={{ display: 'block', borderRadius: 2 }} />;
}

function McpMini() {
  return (
    <svg width={16} height={16} viewBox="0 0 24 24" fill="none" stroke="var(--po-accent)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="2" y="3" width="20" height="14" rx="2" /><line x1="8" y1="21" x2="16" y2="21" /><line x1="12" y1="17" x2="12" y2="21" />
    </svg>
  );
}

function SandboxMini() {
  return (
    <svg width={16} height={16} viewBox="0 0 24 24" fill="none" stroke="var(--po-warning)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="4 17 10 11 4 5" /><line x1="12" y1="19" x2="20" y2="19" />
    </svg>
  );
}

/* ================================================================
   Config map for agent types
   ================================================================ */

const AGENT_CONFIG_MAP: Record<AgentTypeId, React.ComponentType<AgentConfigProps>> = {
  chat: ChatAgentConfig,
};

/* ================================================================
   SyncConfigPanel
   ================================================================ */

interface SyncConfigPanelProps {
  mode: 'create' | 'detail';
  syncId: string | null;
  projectId: string;
  onClose: () => void;
  onBack?: () => void;
  onSyncCreated?: (nodeId: string, synchronizeBindingId?: string) => void;
  /** When opened from a scope context, restricts drag-drop targets to
   *  paths inside this scope (see isWithinScope). Forwarded to all
   *  inner config components (ChatAgentConfig /
   *  SaaSyncConfig). */
  scopeBoundary?: string;
  scopeBoundaryLabel?: string;
  /** Pre-select the agent type when entering create mode. Used by the
   *  "AI Agent" default click in ScopedConnectorsListPanel — skips the
   *  type-picker and lands directly on the chat-agent form. */
  presetAgentType?: AgentTypeId;
}

export function SyncConfigPanel({
  mode, syncId, projectId, onClose, onBack, onSyncCreated,
  scopeBoundary, scopeBoundaryLabel, presetAgentType,
}: SyncConfigPanelProps) {
  if (mode === 'detail' && syncId) {
    return (
      <SyncDetailView syncId={syncId} projectId={projectId} onClose={onClose} onBack={onBack} />
    );
  }

  return (
    <CreateView
      projectId={projectId}
      onClose={onClose}
      onBack={onBack}
      onSyncCreated={onSyncCreated}
      scopeBoundary={scopeBoundary}
      scopeBoundaryLabel={scopeBoundaryLabel}
      presetAgentType={presetAgentType}
    />
  );
}

/* ================================================================
   PanelHeader
   ================================================================ */

/* ================================================================
   CreateView — unified creation panel for agents & syncs
   ================================================================ */

function CreateView({
  projectId, onClose, onBack, onSyncCreated,
  scopeBoundary, scopeBoundaryLabel, presetAgentType,
}: {
  projectId: string;
  onClose: () => void;
  onBack?: () => void;
  onSyncCreated?: (nodeId: string, synchronizeBindingId?: string) => void;
  scopeBoundary?: string;
  scopeBoundaryLabel?: string;
  presetAgentType?: AgentTypeId;
}) {
  const {
    deployAgent, deploySyncEndpoint, setDraftType, draftResources,
    pendingSyncProvider, setDraftResources,
    draftSyncMode, setDraftSyncMode, draftTriggerConfig,
  } = useAgent();

  const { specs: connectorSpecs } = useConnectorSpecs();

  const syncProviders = useMemo<SyncProviderDef[]>(() => connectorSpecs.map(spec => {
    const iconUrl = resolveProviderIconUrl({
      provider: spec.provider,
      icon: spec.icon,
      iconUrl: spec.icon_url,
    });

    return {
      id: spec.provider,
      label: spec.display_name,
      description: spec.description || '',
      icon: iconUrl
        ? <ProviderImg src={iconUrl} />
        : <span style={{ fontSize: 14 }}>{spec.icon || '📄'}</span>,
      oauthType: spec.oauth_ui_type ? spec.oauth_ui_type as SaasType : undefined,
      requiresAuth: spec.auth !== 'none',
      creationMode: spec.creation_mode,
      direction: (spec.supported_directions?.[0] || 'inbound') as 'inbound' | 'outbound' | 'bidirectional',
      accept: (spec.accept_types ?? []) as AcceptedNodeType[],
      configFields: (spec.config_fields ?? []).map(f => ({
        key: f.key,
        label: f.label,
        type: (f.type === 'url' ? 'text' : f.type) as 'select' | 'text' | 'number',
        placeholder: f.placeholder || undefined,
        options: f.options || undefined,
        defaultValue: f.default != null ? String(f.default) : undefined,
        required: f.required || undefined,
        hint: f.hint || undefined,
      })),
    };
  }), [connectorSpecs]);

  const [selectedAgentType, setSelectedAgentType] = useState<AgentTypeId | null>(null);
  const [selectedEndpointType, setSelectedEndpointType] = useState<EndpointTypeId | null>(null);
  const [selectedSyncProvider, setSelectedSyncProvider] = useState<string | null>(null);
  const [displayName, setDisplayName] = useState('');
  const [deploying, setDeploying] = useState(false);
  const [deployError, setDeployError] = useState<string | null>(null);
  const [syncConfigValues, setSyncConfigValues] = useState<Record<string, string>>({});

  // Picker section toggles. Keys correspond to the three remaining
  // sections (inbound SaaS sources / terminal / endpoint exposure) —
  // the deprecated `agent` (Chat Agent)
  // sections were removed when those connectors became built-ins.
  const [expandedSections, setExpandedSections] = useState<Record<string, boolean>>({
    inbound: true,
    tools: true,
    build: true,
  });

  const toggleSection = (section: string) => {
    setExpandedSections(prev => ({
      ...prev,
      [section]: !prev[section]
    }));
  };

  useEffect(() => {
    if (pendingSyncProvider && syncProviders.length > 0) {
      const found = syncProviders.find(p => p.id === pendingSyncProvider);
      if (found) {
        setSelectedSyncProvider(found.id);
        setSelectedAgentType(null);
        const defaults: Record<string, string> = {};
        for (const f of found.configFields) {
          if (f.defaultValue) defaults[f.key] = f.defaultValue;
        }
        setSyncConfigValues(defaults);
      }
    }
  }, [pendingSyncProvider, syncProviders]);

  // Skip the type-picker when the panel was opened with a pre-selected
  // agent type (e.g. clicking "AI Agent" default in the scope panel).
  // Run once on mount — re-running on later prop changes would override
  // a user's intentional Back-to-picker click.
  useEffect(() => {
    if (presetAgentType) {
      setSelectedAgentType(presetAgentType);
      setSelectedEndpointType(null);
      setSelectedSyncProvider(null);
      setDraftType(presetAgentType);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // CRITICAL: handleSelect*/handleBack must NOT clear draftResources.
  // The previous version called `setDraftResources([])` on every
  // provider/endpoint pick, which silently wiped any target
  // pre-filled by an external caller (sidebar `Expose as...`, header
  // expose, etc.).  Symptom: user exposed a folder from the sidebar,
  // panel opened with that folder set as target, user clicked a
  // provider → target zone snapped back to "Drag a folder into this
  // zone" and the panel looked broken.  Switching providers within
  // the same panel session is NOT a "I want a different target" gesture
  // — the user is just exploring sync options for the same folder.
  // Only handleBack-then-explicit-cancel-and-reopen would justify
  // clearing target, and that flow already runs through
  // setDraftResources upstream from page.tsx.

  const handleSelectEndpointType = (type: EndpointTypeId) => {
    setSelectedEndpointType(type);
    setSelectedAgentType(null);
    setSelectedSyncProvider(null);
    setDisplayName('');
    setDeployError(null);
  };

  const handleSelectSyncProvider = (id: string) => {
    setSelectedSyncProvider(id);
    setSelectedAgentType(null);
    setSelectedEndpointType(null);
    setDisplayName('');
    setDeployError(null);
    const provider = syncProviders.find(p => p.id === id);
    const defaults: Record<string, string> = {};
    if (provider) {
      for (const f of provider.configFields) {
        if (f.defaultValue) defaults[f.key] = f.defaultValue;
      }
    }
    setSyncConfigValues(defaults);
  };

  const handleBack = () => {
    setSelectedAgentType(null);
    setSelectedEndpointType(null);
    setSelectedSyncProvider(null);
    setSyncConfigValues({});
    setDisplayName('');
    setDeployError(null);
  };

  // Deploy agent
  const openPanel = useProjectSession(state => state.openPanel);
  const handleAgentDeploy = useCallback(async () => {
    if (!selectedAgentType || deploying) return;
    setDeploying(true);
    try {
      const name = displayName.trim() || 'Chat Agent';
      const agentId = await deployAgent(name, '💬');
      if (agentId) {
        openPanel({ type: 'agent_chat', agentId, nodeId: draftResources[0]?.path ?? scopeBoundary });
      }
    } finally {
      setDeploying(false);
    }
  }, [selectedAgentType, displayName, deploying, deployAgent, openPanel, draftResources, scopeBoundary]);

  // Deploy endpoint (MCP / Sandbox)
  const handleEndpointDeploy = useCallback(async () => {
    if (!selectedEndpointType || deploying) return;
    setDeploying(true);
    try {
      const name = displayName.trim() || (selectedEndpointType === 'mcp' ? 'MCP Server' : 'Sandbox');
      await deploySyncEndpoint({
        provider: selectedEndpointType,
        direction: 'bidirectional',
        config: { name },
        uiMode: 'inline',
      });
      onClose();
    } catch (err) {
      console.error('Failed to create endpoint:', err);
    } finally {
      setDeploying(false);
    }
  }, [selectedEndpointType, displayName, deploying, deploySyncEndpoint, onClose]);

  const syncRequiredFieldsMissing = selectedSyncProvider
    ? syncProviders.find(p => p.id === selectedSyncProvider)?.configFields
        .filter(f => f.required)
        .some(f => !syncConfigValues[f.key]?.trim()) ?? false
    : false;

  // Deploy sync
  const handleSyncDeploy = useCallback(async () => {
    if (!selectedSyncProvider || deploying) return;
    const providerDef = syncProviders.find(p => p.id === selectedSyncProvider);

    const BOOTSTRAP_PROVIDERS: Record<string, { direction: 'inbound' | 'outbound' | 'bidirectional' }> = {};
    const bootstrapFallback = BOOTSTRAP_PROVIDERS[selectedSyncProvider];

    if (!providerDef && !bootstrapFallback) return;

    const creationMode = providerDef?.creationMode ?? (bootstrapFallback ? 'bootstrap' : 'direct');

    if (providerDef) {
      const missingRequired = providerDef.configFields
        .filter(f => f.required && !syncConfigValues[f.key]?.trim());
      if (missingRequired.length > 0) return;
    }

    setDeploying(true);
    setDeployError(null);
    try {
      const target = draftResources[0];
      if (!target) return;

      const config: Record<string, unknown> = { ...syncConfigValues };
      let createdNodeId: string | null = null;
      let createdBindingId: string | undefined;

      if (creationMode === 'bootstrap') {
        await deploySyncEndpoint({
          provider: providerDef?.id ?? selectedSyncProvider,
          direction: providerDef?.direction ?? bootstrapFallback?.direction ?? 'bidirectional',
          config,
          uiMode: 'inline',
        });
        createdNodeId = target.path;
      } else {
        if (!providerDef) return;
        const result = await createSynchronizeBinding({
          project_id: projectId,
          provider: providerDef.id,
          config,
          target_path: target.path,
          direction: providerDef.direction,
          sync_mode: draftSyncMode as 'manual' | 'scheduled',
          trigger: draftSyncMode === 'scheduled'
            ? {
                type: 'scheduled',
                schedule: draftTriggerConfig?.schedule,
                timezone: draftTriggerConfig?.timezone,
              }
            : { type: 'manual' },
        });
        createdNodeId = result.binding.path;
        createdBindingId = result.binding.id;
        if (createdNodeId == null) {
          throw new Error('Synchronize binding was created without a destination path.');
        }
        if (result.binding.status === 'error' && result.binding.error_message) {
          throw new Error(result.binding.error_message);
        }
      }

      if (createdNodeId !== null && onSyncCreated) {
        await onSyncCreated(createdNodeId, createdBindingId);
      } else {
        onClose();
      }
    } catch (err: any) {
      console.error('Failed to create sync:', err);
      // User-friendly messages for known error types
      if (err?.isDuplicate || err?.status === 409 || err?.code === 409) {
        setDeployError('This folder already has an integration of this type. Remove the existing one or choose a different folder.');
      } else if (err?.status === 403) {
        setDeployError('You do not have permission to create integrations in this project.');
      } else {
        setDeployError(err instanceof Error ? err.message : 'Creation failed. Please try again.');
      }
    } finally {
      setDeploying(false);
    }
  }, [selectedSyncProvider, deploying, draftResources, syncConfigValues, draftSyncMode, draftTriggerConfig, deploySyncEndpoint, onSyncCreated, onClose, projectId, syncProviders]);

  // If an agent type is selected, show config
  if (selectedAgentType) {
    const ConfigComponent = AGENT_CONFIG_MAP[selectedAgentType];
    return (
      <PanelShell title="Chat Agent" onClose={onClose} onBack={handleBack}>
        <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
          <div style={{ flex: 1, overflowY: 'auto', padding: '12px 12px 24px' }}>
            <SyncPreview
              provider="agent"
              providerLabel="Chat Agent"
              direction="outbound"
              targetName={draftResources[0]?.nodeName || null}
              targetType={(draftResources[0]?.nodeType as AcceptedNodeType) || 'folder'}
              isActive={draftResources.length > 0}
            />
            <div style={{ marginBottom: 12, marginTop: 12 }}>
              <label style={{ fontSize: 13, fontWeight: 500, color: 'var(--po-text)', marginBottom: 6, display: 'block' }}>Name</label>
              <input
                value={displayName}
                onChange={e => setDisplayName(e.target.value)}
                placeholder="Chat Agent"
                style={{
                  width: '100%', height: 36, padding: '0 12px', fontSize: 13, background: 'var(--po-panel)',
                  border: '1px solid var(--po-border)', borderRadius: 6, color: 'var(--po-text)',
                  outline: 'none', transition: 'border-color 0.2s'
                }}
                onFocus={e => e.currentTarget.style.borderColor = 'var(--po-focus-ring)'}
                onBlur={e => e.currentTarget.style.borderColor = 'var(--po-border)'}
                onMouseEnter={e => { if (document.activeElement !== e.currentTarget) e.currentTarget.style.borderColor = 'color-mix(in srgb, var(--po-text) 22%, transparent)' }}
                onMouseLeave={e => { if (document.activeElement !== e.currentTarget) e.currentTarget.style.borderColor = 'var(--po-border)' }}
              />
            </div>
            <ConfigComponent scopeBoundary={scopeBoundary} scopeBoundaryLabel={scopeBoundaryLabel} />
          </div>

          <div style={{
            padding: '12px',
            borderTop: '1px solid var(--po-border-subtle)',
            background: 'var(--po-canvas)',
            flexShrink: 0
          }}>
            <button
              onClick={handleAgentDeploy}
              disabled={deploying || draftResources.length === 0}
              style={{
                width: '100%', height: 36,
                background: (deploying || draftResources.length === 0) ? 'var(--po-filetree-rail)' : 'var(--po-accent)',
                color: (deploying || draftResources.length === 0) ? 'var(--po-text-subtle)' : 'var(--po-text-inverse)',
                border: 'none', borderRadius: 6, fontSize: 13, fontWeight: 500,
                cursor: (deploying || draftResources.length === 0) ? 'not-allowed' : 'pointer',
                transition: 'all 0.2s',
              }}
            >
              {deploying ? 'Adding…' : 'Add workflow'}
            </button>
          </div>
        </div>
      </PanelShell>
    );
  }

  // If an endpoint type is selected (MCP / Sandbox), show config
  if (selectedEndpointType) {
    const endpointDef = ENDPOINT_OPTIONS.find(e => e.id === selectedEndpointType)!;
    return (
      <PanelShell title={endpointDef.label} onClose={onClose} onBack={handleBack}>
        <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
          <div style={{ flex: 1, overflowY: 'auto', padding: '12px 12px 24px' }}>
            <SyncPreview
              provider={selectedEndpointType === 'mcp' ? 'mcp' : 'sandbox'}
              providerLabel={endpointDef.label}
              direction="bidirectional"
              targetName={draftResources[0]?.nodeName || null}
              targetType={(draftResources[0]?.nodeType as AcceptedNodeType) || 'folder'}
              isActive={draftResources.length > 0}
            />
            <div style={{ marginBottom: 12, marginTop: 12 }}>
              <label style={{ fontSize: 13, fontWeight: 500, color: 'var(--po-text)', marginBottom: 6, display: 'block' }}>Name</label>
              <input
                value={displayName}
                onChange={e => setDisplayName(e.target.value)}
                placeholder={endpointDef.label}
                style={{
                  width: '100%', height: 36, padding: '0 12px', fontSize: 13, background: 'var(--po-panel)',
                  border: '1px solid var(--po-border)', borderRadius: 6, color: 'var(--po-text)',
                  outline: 'none', transition: 'border-color 0.2s'
                }}
                onFocus={e => e.currentTarget.style.borderColor = 'var(--po-focus-ring)'}
                onBlur={e => e.currentTarget.style.borderColor = 'var(--po-border)'}
                onMouseEnter={e => { if (document.activeElement !== e.currentTarget) e.currentTarget.style.borderColor = 'color-mix(in srgb, var(--po-text) 22%, transparent)' }}
                onMouseLeave={e => { if (document.activeElement !== e.currentTarget) e.currentTarget.style.borderColor = 'var(--po-border)' }}
              />
            </div>
            <div style={{ padding: '12px', background: 'var(--po-panel)', border: '1px solid var(--po-border)', borderRadius: 8, marginBottom: 12 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
                {endpointDef.icon}
                <span style={{ fontSize: 13, fontWeight: 500, color: 'var(--po-text)' }}>{endpointDef.label}</span>
              </div>
              <p style={{ fontSize: 12, color: 'var(--po-text-muted)', lineHeight: 1.5, margin: 0 }}>
                {selectedEndpointType === 'mcp'
                  ? 'Creates a Model Context Protocol endpoint. Configure tool bindings and node access from the detail page after creation.'
                  : 'Creates an isolated sandbox environment. Configure mounted nodes and execution permissions from the detail page after creation.'}
              </p>
            </div>
            <ChatAgentConfig
              targetLabel={selectedEndpointType === 'mcp' ? 'Data Access Target' : 'Workspace Mount'}
              targetDescription={selectedEndpointType === 'mcp'
                ? 'Drag and drop a folder or file to expose as MCP tools.'
                : 'Drag and drop a folder to mount into the sandbox environment.'}
              scopeBoundary={scopeBoundary}
              scopeBoundaryLabel={scopeBoundaryLabel}
            />
          </div>

          <div style={{
            padding: '12px',
            borderTop: '1px solid var(--po-border-subtle)',
            background: 'var(--po-canvas)',
            flexShrink: 0
          }}>
            <button
              onClick={handleEndpointDeploy}
              disabled={deploying || draftResources.length === 0}
              style={{
                width: '100%', height: 36,
                background: (deploying || draftResources.length === 0) ? 'var(--po-filetree-rail)' : 'var(--po-accent)',
                color: (deploying || draftResources.length === 0) ? 'var(--po-text-subtle)' : 'var(--po-text-inverse)',
                border: 'none', borderRadius: 6, fontSize: 13, fontWeight: 500,
                cursor: (deploying || draftResources.length === 0) ? 'not-allowed' : 'pointer',
                transition: 'all 0.2s',
              }}
            >
              {deploying ? 'Adding…' : 'Add workflow'}
            </button>
          </div>
        </div>
      </PanelShell>
    );
  }

  // If a sync provider is selected, show config
  if (selectedSyncProvider) {
  // GitHub is the project-level git-branch binding, not a scope-level
    // URL-import connector). It's surfaced under this same picker so all
    // third-party workflows live in one place, but the underlying API +
    // storage live in ``github_integrations``, not ``connectors``.
    // Re-uses the panel wrapper shared by the project-level workflow
    // surface.
    if (selectedSyncProvider === 'github') {
      return (
        <PanelShell title="GitHub" onClose={onClose} onBack={handleBack}>
          <div style={{ flex: 1, overflowY: 'auto', padding: '12px 12px 24px' }}>
            <GithubIntegrationPanel projectId={projectId} />
          </div>
        </PanelShell>
      );
    }

  const providerDef = syncProviders.find(p => p.id === selectedSyncProvider)!;
  return (
    <PanelShell title={providerDef.label} onClose={onClose} onBack={handleBack}>
      <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
        <div style={{ flex: 1, overflowY: 'auto', padding: '12px 12px 24px' }}>
          <SaaSyncConfig
            provider={providerDef.id}
            providerLabel={providerDef.label}
            oauthType={providerDef.oauthType}
            requiresAuth={providerDef.requiresAuth ?? false}
            icon={providerDef.icon}
            description={providerDef.description}
            configFields={providerDef.configFields}
            accept={providerDef.accept}
            direction={providerDef.direction}
            configValues={syncConfigValues}
            onConfigChange={(key, value) => setSyncConfigValues(prev => ({ ...prev, [key]: value }))}
            scopeBoundary={scopeBoundary}
            scopeBoundaryLabel={scopeBoundaryLabel}
          />
        </div>

        <div style={{
          padding: '12px',
          borderTop: '1px solid var(--po-border-subtle)',
          background: 'var(--po-canvas)', // Match panel background to prevent transparency issues when scrolling
          flexShrink: 0
        }}>
          {deployError && (
            <div style={{
              display: 'flex', alignItems: 'flex-start', gap: 8, marginBottom: 10,
              padding: '8px 10px', borderRadius: 6,
              background: 'color-mix(in srgb, var(--po-danger) 10%, transparent)', border: '1px solid color-mix(in srgb, var(--po-danger) 26%, transparent)',
            }}>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--po-danger)" strokeWidth="2" strokeLinecap="round" style={{ flexShrink: 0, marginTop: 1 }}>
                <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>
              </svg>
              <span style={{ fontSize: 12, color: 'var(--po-danger)', flex: 1, lineHeight: 1.5 }}>{deployError}</span>
              <button
                onClick={() => setDeployError(null)}
                style={{ width: 30, height: 30, background: 'none', border: 'none', cursor: 'pointer', padding: 0, color: 'var(--po-danger)', flexShrink: 0, opacity: 0.7, display: 'inline-flex', alignItems: 'center', justifyContent: 'center' }}
                onMouseEnter={e => (e.currentTarget.style.opacity = '1')}
                onMouseLeave={e => (e.currentTarget.style.opacity = '0.7')}
              >
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
              </button>
            </div>
          )}
          <button
            onClick={handleSyncDeploy}
            disabled={deploying || draftResources.length === 0 || syncRequiredFieldsMissing}
            style={{
              width: '100%', height: 36,
              background: (deploying || draftResources.length === 0 || syncRequiredFieldsMissing) ? 'var(--po-filetree-rail)' : 'var(--po-accent)',
              color: (deploying || draftResources.length === 0 || syncRequiredFieldsMissing) ? 'var(--po-text-subtle)' : 'var(--po-text-inverse)',
              border: 'none', borderRadius: 6, fontSize: 13, fontWeight: 500,
              cursor: (deploying || draftResources.length === 0 || syncRequiredFieldsMissing) ? 'not-allowed' : 'pointer',
              transition: 'all 0.2s',
            }}
          >
            {deploying ? 'Adding…' : 'Add workflow'}
          </button>
        </div>
      </div>
    </PanelShell>
    );
  }

  // Push "Coming soon" providers to the bottom of each section so they sit
  // visually beneath the active ones rather than mixed in.
  const sinkComingSoon = (a: { id: string }, b: { id: string }) =>
    Number(COMING_SOON_PROVIDERS.has(a.id)) - Number(COMING_SOON_PROVIDERS.has(b.id));
  const inboundProviders = syncProviders.filter(p => p.direction === 'inbound').sort(sinkComingSoon);

  // Default: show provider picker.
  //
  // Three remaining sections:
  //   1. Connect a source — durable third-party pulls (Gmail,
  //      Notion, Google Docs, etc.).
  //   2. Connect via terminal — currently SSH-only "coming soon"
  //      placeholder; FS CLI is managed from the scope detail.
  //   3. Expose data — MCP / Sandbox endpoint placeholders ("coming
  //      soon").
  //
  // What we removed (and why):
  //   - "Share data with an AI Agent / Chat Agent" — agent is now
  //     built-in too. Users reach the chat runtime from the scope's
  //     detail page (the Connect block's "AI Agent" MethodCard),
  //     not by minting a top-level Chat Agent connector here.
  return (
    <PanelShell title="New access" onClose={onClose} onBack={onBack}>
      <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
        <div style={{ flex: 1, overflowY: 'auto', padding: '12px 12px 24px', display: 'flex', flexDirection: 'column', gap: 16 }}>

          {/* 1. Connect a source */}
          <div style={{ display: 'flex', flexDirection: 'column' }}>
            <DirectionalSectionLabel
              type="inbound"
              title="Connect a source"
              hint="Gmail, Notion, GitHub..."
              isExpanded={expandedSections.inbound}
              onClick={() => toggleSection('inbound')}
            />
            {expandedSections.inbound && (
              <div style={{
                display: 'flex', flexDirection: 'column', gap: 4,
                paddingLeft: 24,
                paddingRight: 4,
                paddingBottom: 12,
                paddingTop: 4
              }}>
                {inboundProviders.map(p => {
                  const comingSoon = COMING_SOON_PROVIDERS.has(p.id);
                  return (
                    <ProviderRow
                      key={p.id}
                      icon={p.icon}
                      label={p.label}
                      description={comingSoon ? 'Coming soon' : p.description}
                      onClick={comingSoon ? () => {} : () => handleSelectSyncProvider(p.id)}
                      disabled={comingSoon}
                    />
                  );
                })}
              </div>
            )}
          </div>

          {/* 2. Connect via terminal */}
          <div style={{ display: 'flex', flexDirection: 'column' }}>
            <DirectionalSectionLabel
              type="tools"
              title="Connect via terminal"
              hint="Cursor, Claude Code, Codex"
              isExpanded={expandedSections.tools}
              onClick={() => toggleSection('tools')}
            />
            {expandedSections.tools && (
              <div style={{
                display: 'flex', flexDirection: 'column', gap: 4,
                paddingLeft: 24,
                paddingRight: 4,
                paddingBottom: 12,
                paddingTop: 4
              }}>
                <ProviderRow
                  icon={<svg width={16} height={16} viewBox="0 0 24 24" fill="none" stroke="var(--po-file-accent-audio)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="4 17 10 11 4 5" /><line x1="12" y1="19" x2="20" y2="19" /></svg>}
                  label="SSH Terminal"
                  description="Coming soon"
                  onClick={() => {}}
                  disabled
                />
              </div>
            )}
          </div>

          {/* 3. Expose data */}
          <div style={{ display: 'flex', flexDirection: 'column' }}>
            <DirectionalSectionLabel
              type="build"
              title="Expose data"
              hint="MCP, Sandbox"
              isExpanded={expandedSections.build}
              onClick={() => toggleSection('build')}
            />
            {expandedSections.build && (
              <div style={{
                display: 'flex', flexDirection: 'column', gap: 4,
                paddingLeft: 24,
                paddingRight: 4,
                paddingBottom: 12,
                paddingTop: 4
              }}>
                {ENDPOINT_OPTIONS.map(opt => (
                  <ProviderRow
                    key={opt.id}
                    icon={opt.icon}
                    label={opt.label}
                    description="Coming soon"
                    onClick={() => {}}
                    disabled
                  />
                ))}
              </div>
            )}
          </div>

        </div>
      </div>
    </PanelShell>
  );
}

/* ================================================================
   Shared UI Atoms
   ================================================================ */

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div style={{
      fontSize: 11, fontWeight: 600, color: 'var(--po-text-subtle)', textTransform: 'uppercase',
      letterSpacing: '0.04em', marginBottom: 6, padding: '0 4px',
    }}>
      {children}
    </div>
  );
}

function DirectionalSectionLabel({ type, title, hint, isExpanded, onClick }: {
  // Picker post-cleanup: only `inbound` (SaaS pulls), `tools` (terminal),
  // and `build` (endpoint exposure) remain.
  type: 'inbound' | 'tools' | 'build',
  title: string,
  hint?: string,
  isExpanded: boolean,
  onClick: () => void
}) {
  const [hovered, setHovered] = useState(false);
  let iconContent;

  if (type === 'inbound') {
    iconContent = <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>;
  } else if (type === 'tools') {
    iconContent = <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="4 17 10 11 4 5"></polyline><line x1="12" y1="19" x2="20" y2="19"></line></svg>;
  } else {
    iconContent = <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"></path></svg>;
  }

  const displayTitle = title;

  return (
    <button
      onClick={onClick}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        display: 'flex', alignItems: 'center', width: '100%', gap: 12,
        background: hovered ? 'var(--po-hover)' : 'transparent',
        border: 'none', cursor: 'pointer',
        padding: '8px 8px', textAlign: 'left',
        borderRadius: 6,
        transition: 'background 0.15s ease',
        marginLeft: '-8px' // Offset the padding to keep alignment
      }}
    >
      <div style={{
        display: 'flex', alignItems: 'center', gap: 8,
        flexShrink: 0
      }}>
        <div style={{
          color: hovered ? 'var(--po-text-muted)' : 'var(--po-text-subtle)', display: 'flex',
          transition: 'all 0.2s', transform: isExpanded ? 'rotate(90deg)' : 'rotate(0deg)',
          opacity: isExpanded ? 0.8 : 0.5
        }}>
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="9 18 15 12 9 6"></polyline></svg>
        </div>
        <div style={{
          color: (isExpanded || hovered) ? 'var(--po-text)' : 'var(--po-text-subtle)',
          display: 'flex', transition: 'all 0.2s'
        }}>
          {iconContent}
        </div>
        <div style={{
          fontSize: 13, fontWeight: 500,
          color: (isExpanded || hovered) ? 'var(--po-text)' : 'var(--po-text-subtle)',
          transition: 'color 0.2s',
          whiteSpace: 'nowrap',
        }}>
          {displayTitle}
          {hint && (
            <span style={{
              fontWeight: 400,
              color: (isExpanded || hovered) ? 'var(--po-text-subtle)' : 'var(--po-text-disabled)',
              fontSize: 12,
              marginLeft: 4,
              transition: 'color 0.2s',
            }}>
              ({hint})
            </span>
          )}
        </div>
      </div>
      <div style={{
        flex: 1,
        height: '1px',
        backgroundColor: (isExpanded || hovered) ? 'var(--po-active)' : 'var(--po-hover)',
        transition: 'background-color 0.2s'
      }} />
    </button>
  );
}

function ProviderRow({ icon, label, description, onClick, disabled }: {
  icon: React.ReactNode;
  label: string;
  description: string;
  onClick: () => void;
  disabled?: boolean;
}) {
  const [hovered, setHovered] = useState(false);
  return (
    <button
      onClick={disabled ? undefined : onClick}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        display: 'flex', alignItems: 'center', gap: 10, padding: '8px 10px',
        background: hovered && !disabled ? 'var(--po-border-subtle)' : 'var(--po-panel)',
        border: '1px solid',
        borderColor: hovered && !disabled ? 'var(--po-border-strong)' : 'var(--po-border-subtle)',
        borderRadius: 8, cursor: disabled ? 'default' : 'pointer', width: '100%',
        textAlign: 'left', transition: 'all 0.15s',
        opacity: disabled ? 0.45 : 1,
      }}
    >
      <div style={{
        width: 32, height: 32, borderRadius: 8, display: 'flex', alignItems: 'center', justifyContent: 'center',
        background: disabled ? 'var(--po-panel)' : 'var(--po-hover)', flexShrink: 0,
        filter: disabled ? 'grayscale(1)' : undefined,
      }}>
        {icon}
      </div>
      <div style={{ minWidth: 0, flex: 1 }}>
        <div style={{
          fontSize: 13, fontWeight: 500,
          color: disabled ? 'var(--po-text-disabled)' : (hovered ? 'var(--po-text)' : 'var(--po-text)'),
          transition: 'all 0.15s', lineHeight: 1.3,
        }}>{label}</div>
        <div style={{
          fontSize: 12,
          color: disabled ? 'var(--po-text-disabled)' : 'var(--po-text-subtle)',
          lineHeight: 1.3, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
        }}>
          {description}
        </div>
      </div>
      {!disabled && (
        <div style={{ color: hovered ? 'var(--po-text-subtle)' : 'var(--po-text-disabled)', transition: 'color 0.15s', flexShrink: 0 }}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="9 18 15 12 9 6"></polyline></svg>
        </div>
      )}
    </button>
  );
}
