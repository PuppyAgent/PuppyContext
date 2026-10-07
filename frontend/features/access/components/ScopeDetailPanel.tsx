'use client';

/**
 * ScopeDetailPanel — right rail of the access page.
 *
 * The user's mental model:
 *
 *   1. The left sidebar lists *mount points* (paths). Pick one.
 *   2. Each mount point has multiple *Access Points* bound to it
 *      (a CLI access point, an Agent access point, an MCP one, …).
 *      Each AP has its own database-stored name, status, and prompt.
 *   3. CLI / Agent / MCP / Sandbox / Third-party are *not* "tabs of a
 *      configuration page" — they're fundamentally distinct entities.
 *      So the switcher renders one card *per access point*, not one
 *      tab per type bucket. Picking a card swaps the detail view to
 *      that AP's name + attributes + prompt + configuration.
 *
 * Layout:
 *
 *   ┌───────────────────────────────────────────────────────────┐
 *   │  📁 Workspace root  /  [r] [w]                  [Edit]    │  ← compact scope strip
 *   │                                                            │
 *   │  ACCESS POINTS                                             │
 *   │  ┌───────────┐ ┌───────────┐                              │
 *   │  │ [icon]    │ │ [icon]    │                              │
 *   │  │ CLI    ●  │ │ AGENT  ●  │                              │
 *   │  │ Puppyone… │ │ Hello AI  │                              │
 *   │  └───────────┘ └───────────┘                              │
 *   │   ↑ selected     unselected                                │
 *   │                                                            │
 *   │  Puppyone CLI                       [Pause] [⋮]           │  ← AP NAME (page header)
 *   │  CLI agent · Two-way · ● Active · Never                   │
 *   │  ─────────────────────────────────────                    │
 *   │  PROMPT FOR AI AGENT                                       │
 *   │  [prompt block with centered Copy CTA]                    │
 *   │  CONFIGURATION                                             │
 *   │  [config table]                                            │
 *   └───────────────────────────────────────────────────────────┘
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { StatusIndicator } from '@/components/ui/StatusDot';
import { ToggleSwitch } from '@/components/ui/ToggleSwitch';
import { getAccessProviderMethodMeta, isMcpProvider } from '@/lib/accessProviderRegistry';
import type { AccessSurface, RepositoryView } from '@/lib/repoApi';
import { PROJECT_CONTENT_RAIL_WIDTH } from '@/lib/layout';
import { T } from '@/features/access/lib/tokens';
import { STATUS_LABEL } from '@/features/access/lib/constants';
import { McpConnectCard } from '@/features/access/components/McpConnectCard';
import { SandboxConnectCard } from '@/features/access/components/SandboxConnectCard';
import { SyncActivityPanel } from '@/features/access/components/SyncActivityPanel';
import { ScopeSettingsBlock } from '@/features/files/components/access-points/ScopeSettingsBlock';
import type { ConnectorEditPatch } from '@/features/access/hooks/useAccessData';
import { ConnectorList } from '@/features/access/components/ConnectorMethodList';
import { ScopePageHeader, SettingsSection } from '@/features/access/components/ScopeHeader';
import { ProviderIcon } from '@/features/access/components/icons';
import { getProviderIconSize, getProviderTileSize, getProviderTileStyle } from '@/features/access/components/connectorVisuals';

// ─── Detail pane root ────────────────────────────────────────────────

export function ScopeDetailPanel({
  scope,
  connectors,
  projectId,
  onPauseResume,
  onUpdate,
  onDelete,
  pendingConnectorIds,
  onScopeMutated,
  onScopeDeleted,
  canManage,
  enablingStandardAccess,
  enableStandardAccessError,
  onEnableStandardAccess,
  onNavigationGuardChange,
}: {
  readonly scope: RepositoryView | undefined;
  readonly connectors: readonly AccessSurface[];
  readonly projectId: string;
  readonly onPauseResume: (id: string) => void;
  readonly onUpdate: (id: string, patch: ConnectorEditPatch) => Promise<void>;
  readonly onDelete: (id: string) => Promise<void>;
  readonly pendingConnectorIds: ReadonlySet<string>;
  /** Refresh both `repo-scopes` and `repo-connectors` SWR caches after
   *  a save / rotate / delete inside the inline settings block. */
  readonly onScopeMutated: () => Promise<unknown>;
  /** Notify the parent that the active scope was deleted, so it can
   *  clear its `selectedTargetKey` and let the auto-select-first effect
   *  pick up an adjacent scope on the next render. */
  readonly onScopeDeleted: () => void;
  readonly canManage: boolean;
  readonly enablingStandardAccess: boolean;
  readonly enableStandardAccessError: string | null;
  readonly onEnableStandardAccess: () => void;
  readonly onNavigationGuardChange?: (guard: (() => boolean) | null) => void;
}) {
  // Track the currently-expanded access point. Defaults to collapsed
  // so first-time users see the compact access point list before drilling
  // into setup/configuration details.
  const [selectedConnectorId, setSelectedConnectorId] = useState<string | null>(null);

  // Inline scope-settings toggle. The `Edit` button on the strip flips
  // this; we mount `ScopeSettingsBlock` right under the strip so the
  // user never leaves the access page. Auto-collapses when the user
  // navigates to a different scope so dirty edits don't ride along.
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsDirty, setSettingsDirty] = useState(false);

  const canLeave = useCallback(() => {
    if (!settingsOpen || !settingsDirty) return true;
    return globalThis.confirm('Discard unsaved scope edits?');
  }, [settingsDirty, settingsOpen]);

  useEffect(() => {
    onNavigationGuardChange?.(settingsOpen && settingsDirty ? canLeave : null);
    return () => onNavigationGuardChange?.(null);
  }, [canLeave, onNavigationGuardChange, settingsDirty, settingsOpen]);

  useEffect(() => {
    setSettingsOpen(false);
    setSettingsDirty(false);
    setSelectedConnectorId(null);
  }, [scope?.id]);

  const handleSelectConnector = useCallback((connectorId: string) => {
    setSelectedConnectorId((current) => {
      if (current === connectorId) {
        return null;
      }
      return connectorId;
    });
  }, []);

  const handleToggleSettings = useCallback(() => {
    if (!canManage) return;
    if (settingsOpen && settingsDirty) {
      const ok = globalThis.confirm(
        'Discard unsaved scope edits?',
      );
      if (!ok) return;
      setSettingsDirty(false);
    }
    setSettingsOpen((v) => !v);
  }, [canManage, settingsOpen, settingsDirty]);

  const handleScopeDeleted = useCallback(() => {
    onNavigationGuardChange?.(null);
    setSettingsOpen(false);
    setSettingsDirty(false);
    onScopeDeleted();
  }, [onNavigationGuardChange, onScopeDeleted]);

  const visibleConnectors = connectors;
  const hasStandardAccess = visibleConnectors.some(
    (connector) => connector.kind === 'git_remote' || connector.kind === 'cli',
  );
  const hasMcpMethod = visibleConnectors.some((connector) => isMcpProvider(connector.kind));
  const methodCount = visibleConnectors.length + (scope ? 1 : 0) + (scope && !hasMcpMethod ? 1 : 0);

  useEffect(() => {
    if (visibleConnectors.length === 0) {
      setSelectedConnectorId(null);
      return;
    }
    if (selectedConnectorId == null) return;
    const stillExists = visibleConnectors.some((c) => c.id === selectedConnectorId);
    if (!stillExists) setSelectedConnectorId(null);
  }, [selectedConnectorId, visibleConnectors]);

  const selectedConnector = useMemo(
    () =>
      visibleConnectors.find((c) => c.id === selectedConnectorId) ?? null,
    [selectedConnectorId, visibleConnectors],
  );

  return (
    <div
      key={scope?.id ?? 'no-scope'}
      style={{
        flex: 1,
        minWidth: 0,
        overflow: 'auto',
        background: T.bg,
        animation: `puppyone-access-fade-in 200ms ${T.ease}`,
      }}
    >
      <div
        style={{
          maxWidth: PROJECT_CONTENT_RAIL_WIDTH,
          margin: '0 auto',
          padding: '20px 24px 40px',
        }}
      >
        {/* PAGE HEADER — `scope.name` at h1 scale with aggregate status
            beneath. The right side stays intentionally light: settings
            live here, while pause/resume belongs to each access point. */}
        <ScopePageHeader
          scope={scope}
          connectors={visibleConnectors}
          settingsOpen={settingsOpen}
          settingsDirty={settingsDirty}
          onToggleSettings={handleToggleSettings}
          canManage={canManage}
        />

        {/* SETTINGS — opened from the header gear. The collapsed
            placeholder row is intentionally gone so the boundary row
            can stay visually adjacent to the title. */}
        {scope && canManage ? (
          <SettingsSection open={settingsOpen}>
            <ScopeSettingsBlock
              scope={scope}
              projectId={projectId}
              onMutated={onScopeMutated}
              onScopeDeleted={handleScopeDeleted}
              onDirtyChange={setSettingsDirty}
              accessMethods={
                <ScopeAccessMethodsSettings
                  connectors={visibleConnectors}
                  showMcpPlaceholder={!hasMcpMethod}
                  pendingConnectorIds={pendingConnectorIds}
                  onPauseResume={onPauseResume}
                />
              }
            />
          </SettingsSection>
        ) : null}

        {scope && canManage && !hasStandardAccess ? (
          <div
            style={{
              marginTop: 18,
              padding: '14px 16px',
              borderRadius: 8,
              border: `1px dashed ${T.cardBorder}`,
              background: T.cardBg,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              gap: 16,
              fontFamily: T.fontSans,
            }}
          >
            <div style={{ minWidth: 0 }}>
              <div style={{ color: T.text1, fontSize: 13, fontWeight: 600 }}>
                Enable Git and PuppyOne CLI
              </div>
              <div style={{ color: T.text3, fontSize: 12, lineHeight: '18px', marginTop: 3 }}>
                This target exists independently. Enable its standard access methods when needed.
              </div>
              {enableStandardAccessError ? (
                <div style={{ color: 'var(--po-danger)', fontSize: 12, marginTop: 6 }}>
                  {enableStandardAccessError}
                </div>
              ) : null}
            </div>
            <button
              type='button'
              onClick={onEnableStandardAccess}
              disabled={enablingStandardAccess}
              style={{
                height: 32,
                padding: '0 12px',
                borderRadius: 6,
                border: `1px solid ${T.border}`,
                background: 'var(--po-control)',
                color: T.text1,
                cursor: enablingStandardAccess ? 'wait' : 'pointer',
                fontSize: 12,
                fontWeight: 600,
                whiteSpace: 'nowrap',
              }}
            >
              {enablingStandardAccess ? 'Enabling…' : 'Enable access'}
            </button>
          </div>
        ) : null}

        {/* ACCESS METHODS — cards start directly under the scope header. */}
        {methodCount > 0 ? (
          <div style={{ marginTop: 18 }}>
            {visibleConnectors.length > 0 ? (
              <ConnectorList
                scope={scope}
                connectors={visibleConnectors}
                selectedId={selectedConnector?.id ?? null}
                onSelect={handleSelectConnector}
                onPauseResume={onPauseResume}
                onUpdate={onUpdate}
                pendingConnectorIds={pendingConnectorIds}
                canManage={canManage}
              />
            ) : null}
            {canManage && scope && !hasMcpMethod ? (
              <McpConnectCard
                scope={scope}
                projectId={projectId}
                onCreated={onScopeMutated}
              />
            ) : null}
            {canManage && scope?.target.kind === 'scope'
              ? <SandboxConnectCard scope={scope} projectId={projectId} />
              : null}
          </div>
        ) : (
          <div
            style={{
              marginTop: 18,
              padding: '14px 16px',
              borderRadius: 8,
              border: `1px dashed ${T.cardBorder}`,
              background: T.cardBg,
              fontSize: 14,
              color: T.text2,
              fontFamily: T.fontSans,
              fontStyle: 'italic',
            }}
          >
            No connectors bound to this scope yet.
          </div>
        )}
        {/* Sync activity + stats (M6 observability) — the managed-sync event
            log for this scope. Self-hides when there's no sync history. */}
        {scope ? <SyncActivityPanel scope={scope} projectId={projectId} /> : null}
      </div>

      <style>{`
        @keyframes puppyone-access-fade-in {
          from { opacity: 0; transform: translateY(2px); }
          to   { opacity: 1; transform: translateY(0); }
        }
        @keyframes puppyone-access-settings-slide {
          from { opacity: 0; transform: translateY(-4px); }
          to   { opacity: 1; transform: translateY(0); }
        }
      `}</style>
    </div>
  );
}

function ScopeAccessMethodsSettings({
  connectors,
  showMcpPlaceholder,
  pendingConnectorIds,
  onPauseResume,
}: {
  readonly connectors: readonly AccessSurface[];
  readonly showMcpPlaceholder: boolean;
  readonly pendingConnectorIds: ReadonlySet<string>;
  readonly onPauseResume: (id: string) => void;
}) {
  const wayCount = connectors.length + 1 + (showMcpPlaceholder ? 1 : 0);
  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        gap: 10,
        padding: '0 0 16px',
        borderBottom: '1px solid var(--po-divider)',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 12 }}>
        <div
          style={{
            fontSize: 14,
            fontWeight: 600,
            color: T.text1,
            fontFamily: T.fontSans,
          }}
        >
          Access methods
        </div>
        <div
          style={{
            fontSize: 12,
            color: T.text3,
            fontFamily: T.fontSans,
            whiteSpace: 'nowrap',
          }}
        >
          {wayCount} ways
        </div>
      </div>
      <div
        style={{
          borderRadius: 8,
          border: `1px solid ${T.cardBorder}`,
          background: 'color-mix(in srgb, var(--po-canvas) 72%, var(--po-control) 28%)',
          overflow: 'hidden',
        }}
      >
        {connectors.map((connector, index) => (
          <ScopeAccessMethodRow
            key={connector.id}
            connector={connector}
            pending={pendingConnectorIds.has(connector.id)}
            isFirst={index === 0}
            onPauseResume={() => onPauseResume(connector.id)}
          />
        ))}
        {showMcpPlaceholder ? <McpSettingsRow isFirst={connectors.length === 0} /> : null}
        <RemoteWorkspaceSettingsRow isFirst={connectors.length === 0 && !showMcpPlaceholder} />
      </div>
    </div>
  );
}

function McpSettingsRow({ isFirst }: { readonly isFirst: boolean }) {
  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: 'minmax(0, 1fr) max-content',
        alignItems: 'center',
        gap: 14,
        minHeight: 58,
        padding: '10px 12px',
        borderTop: isFirst ? 'none' : `1px solid ${T.cardBorder}`,
        opacity: 0.72,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, minWidth: 0 }}>
        <div
          style={{
            width: 28,
            height: 28,
            borderRadius: 6,
            background: 'color-mix(in srgb, var(--po-control) 76%, var(--po-canvas) 24%)',
            border: `1px solid ${T.cardBorder}`,
            color: T.text2,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            flexShrink: 0,
          }}
        >
          <ProviderIcon provider='mcp' size={15} variant='mono' />
        </div>
        <div style={{ minWidth: 0, display: 'flex', flexDirection: 'column', gap: 2 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 7, minWidth: 0 }}>
            <span
              style={{
                fontSize: 12,
                fontWeight: 600,
                color: T.text1,
                fontFamily: T.fontSans,
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}
            >
              MCP Server
            </span>
            <span aria-hidden style={{ color: T.text4, fontSize: 12 }}>·</span>
            <StatusIndicator status='inactive' label='Off' style={{ flexShrink: 0 }} />
          </div>
          <span
            style={{
              color: T.text3,
              fontSize: 11,
              lineHeight: '15px',
              fontFamily: T.fontSans,
              whiteSpace: 'nowrap',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
            }}
          >
            Create an endpoint from the MCP card.
          </span>
        </div>
      </div>
      <span
        style={{
          color: T.text3,
          fontSize: 12,
          lineHeight: '16px',
          fontFamily: T.fontSans,
          whiteSpace: 'nowrap',
        }}
      >
        Create from card
      </span>
    </div>
  );
}

function ScopeAccessMethodRow({
  connector,
  pending,
  isFirst,
  onPauseResume,
}: {
  readonly connector: AccessSurface;
  readonly pending: boolean;
  readonly isFirst: boolean;
  readonly onPauseResume: () => void;
}) {
  const meta = getAccessProviderMethodMeta(connector.kind, connector.name);
  const enabled = connector.status === 'active' || connector.status === 'syncing';
  const errored = connector.status === 'error';
  const tile = getProviderTileStyle(connector.kind, false);
  const tileSize = getProviderTileSize(connector.kind);
  const iconSize = getProviderIconSize(connector.kind);
  const statusLabel = STATUS_LABEL[connector.status] ?? connector.status;

  const handleToggle = () => {
    if (pending || errored) return;
    if (enabled) {
      const ok = globalThis.confirm(
        'Disable this access method? Existing copied commands and workflows using it may stop working.',
      );
      if (!ok) return;
    }
    onPauseResume();
  };

  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: 'minmax(0, 1fr) max-content',
        alignItems: 'center',
        gap: 14,
        minHeight: 58,
        padding: '10px 12px',
        borderTop: isFirst ? 'none' : `1px solid ${T.cardBorder}`,
        opacity: enabled || errored ? 1 : 0.58,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, minWidth: 0 }}>
        <div
          style={{
            height: tileSize,
            width: tileSize,
            borderRadius: 7,
            background: tile.background,
            border: `1px solid ${tile.border}`,
            color: tile.color,
            boxShadow: tile.shadow,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            flexShrink: 0,
            overflow: 'hidden',
          }}
        >
          <ProviderIcon provider={connector.kind} size={iconSize} />
        </div>
        <div style={{ minWidth: 0, display: 'flex', flexDirection: 'column', gap: 3 }}>
          <div
            style={{
              color: T.text1,
              fontFamily: T.fontSans,
              fontSize: 13,
              lineHeight: '17px',
              fontWeight: 600,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
          >
            {meta.title}
          </div>
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 7,
              minWidth: 0,
              color: T.text3,
              fontFamily: T.fontSans,
              fontSize: 12,
              lineHeight: '16px',
            }}
          >
            <StatusIndicator status={connector.status} label={statusLabel} />
            <span aria-hidden style={{ color: T.text4 }}>·</span>
            <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {meta.description}
            </span>
          </div>
        </div>
      </div>
      <label
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          justifyContent: 'flex-end',
          gap: 9,
          color: enabled ? T.text1 : T.text2,
          fontFamily: T.fontSans,
          fontSize: 12,
          lineHeight: '16px',
          fontWeight: 500,
          cursor: pending || errored ? 'not-allowed' : 'pointer',
          whiteSpace: 'nowrap',
        }}
      >
        <span>{enabled ? 'Enabled' : errored ? 'Fix required' : 'Disabled'}</span>
        <ToggleSwitch
          checked={enabled}
          pending={pending}
          disabled={errored}
          ariaLabel={`${meta.title} ${enabled ? 'enabled' : 'disabled'}`}
          title={errored ? 'Fix the connector before enabling' : undefined}
          size='xs'
          stopPropagation
          onCheckedChange={handleToggle}
        />
      </label>
    </div>
  );
}

function RemoteWorkspaceSettingsRow({ isFirst }: { readonly isFirst: boolean }) {
  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: 'minmax(0, 1fr) max-content',
        alignItems: 'center',
        gap: 14,
        minHeight: 58,
        padding: '10px 12px',
        borderTop: isFirst ? 'none' : `1px solid ${T.cardBorder}`,
        opacity: 0.62,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, minWidth: 0 }}>
        <div
          style={{
            height: 34,
            width: 34,
            borderRadius: 8,
            background: 'var(--po-text)',
            border: '1px solid color-mix(in srgb, var(--po-text) 72%, var(--po-border-subtle) 28%)',
            color: 'var(--po-text-inverse)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            flexShrink: 0,
          }}
        >
          <WorkspaceGlyph size={20} />
        </div>
        <div style={{ minWidth: 0, display: 'flex', flexDirection: 'column', gap: 3 }}>
          <div
            style={{
              color: T.text1,
              fontFamily: T.fontSans,
              fontSize: 13,
              lineHeight: '17px',
              fontWeight: 600,
            }}
          >
            Remote Workspace
          </div>
          <div
            style={{
              color: T.text3,
              fontFamily: T.fontSans,
              fontSize: 12,
              lineHeight: '16px',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
          >
            Requires SSH key setup from the Remote Workspace card.
          </div>
        </div>
      </div>
      <span
        style={{
          height: 26,
          display: 'inline-flex',
          alignItems: 'center',
          justifyContent: 'center',
          padding: '0 10px',
          borderRadius: 999,
          border: `1px solid ${T.cardBorder}`,
          color: T.text2,
          fontSize: 12,
          fontFamily: T.fontSans,
          whiteSpace: 'nowrap',
        }}
      >
        Setup from card
      </span>
    </div>
  );
}

const WorkspaceGlyph = ({ size = 20 }: { readonly size?: number }) => (
  <svg width={size} height={size} viewBox="0 0 28 28" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
    <rect x="5.25" y="7" width="17.5" height="12.25" rx="2.6" />
    <path d="M10.1 22h7.8" />
    <path d="M14 19.25V22" />
    <path d="M10.2 14.2c.35-1.55 1.58-2.65 3.08-2.65 1.11 0 2.08.56 2.64 1.42.33-.13.69-.2 1.07-.2 1.37 0 2.48 1.04 2.48 2.31s-1.11 2.31-2.48 2.31h-6.42c-1.09 0-1.98-.76-1.98-1.7 0-.78.65-1.36 1.61-1.49Z" />
  </svg>
);
