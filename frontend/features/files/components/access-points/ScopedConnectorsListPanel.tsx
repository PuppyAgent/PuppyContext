'use client';

/**
 * ScopedConnectorsListPanel — per-scope connector list (redesign-2026-05-08).
 *
 * Replaces the project-wide AccessPointsListPanel. Once a scope is
 * selected, the body stacks two primary sections vertically:
 *
 *   ① ConnectMethodsBlock        — Puppyone CLI, Git Remote, AI Agent
 *                                  (the three default ways to access this
 *                                  folder — DB-trigger-backed cli + agent
 *                                  connectors fan out into three UI cards)
 *   ② Workflows                  — third-party providers (notion / gmail
 *                                  / github / url / ...) with a + Add CTA
 *
 * The header is intentionally one line. The scope's access methods
 * lead the detail view; Path lives as quiet body metadata below them.
 * Settings is a sibling sub-page, not an inline detail expansion.
 *
 * Dirty edits in Settings get reported up via onDirtyChange; the
 * panel's back / close chrome confirms before silently discarding
 * them.
 *
 * No parent-child inheritance: a folder shows ONLY connectors of its
 * exact-match scope (per Q1 decision 2026-05-03). Folders that aren't
 * scopes show CreateAccessPointCTACard + AllAccessPointsList instead.
 *
 * Sub-components live in sibling files (see ./AccessPointRow,
 * ./ScopeSettingsBlock, etc.) — this file is the orchestrator that
 * decides which of those to mount based on the current scope state.
 */

import { CountBadge } from '@/components/ui/CountBadge';
import { PanelShell } from '@/features/files/components/PanelShell';
import { AllAccessPointsList } from '@/features/files/components/access-points/AllAccessPointsList';
import { ConnectMethodsBlock } from '@/features/files/components/access-points/ConnectMethods';
import { ConnectorCard } from '@/features/files/components/access-points/ConnectorCard';
import { CreateAccessPointCTACard } from '@/features/files/components/access-points/CreateAccessPointCTACard';
import { ScopeSettingsBlock } from '@/features/files/components/access-points/ScopeSettingsBlock';
import { getApiBase } from '@/features/files/components/access-points/labels';
import {
  COLOR_BORDER_HOVER,
  COLOR_FG,
  COLOR_FG_DIM,
  PANEL_BG,
} from '@/features/files/components/access-points/tokens';
import type { ProviderIconLookup } from '@/features/files/components/access-points/types';
import { FolderIcon } from '@/features/files/components/explorer';
import {
  isAgentProvider,
  isBuiltInAccessProvider,
  isCliProvider,
  isGitRemoteProvider,
} from '@/lib/accessProviderRegistry';
import { GithubIcon, GmailIcon, NotionIcon } from '@/lib/nodeTypeConfig';
import type { AccessSurface, RepositoryView } from '@/lib/repoApi';
import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';

interface Props {
  readonly scope: RepositoryView | null;
  /** All scopes in the project — drives the AllScopesList (non-scope state)
   *  and the AllScopesList is the only navigation surface for scope CRUD. */
  readonly scopes: readonly RepositoryView[];
  /** Canonical path of the folder the user has navigated into. Used by
   *  MakeScopeCTA to pre-fill `path` when creating a scope, and by the
   *  AllScopesList to disable the "Open" button on the current row. */
  readonly currentScopePath: string;
  /** Project id needed for the create/update/delete scope API calls. */
  readonly projectId: string;
  /** Connectors for the *current* scope (filtered up at the page
   *  level). Drives the detail-view ConnectMethodsBlock + Workflows. */
  readonly connectors: readonly AccessSurface[];
  /** Project-wide connectors keyed by scope_id — used in the Overview
   *  state so each AccessPointRow can render its own connect / integration
   *  chip rows without per-row API requests. */
  readonly connectorsByTarget: ReadonlyMap<string, AccessSurface[]>;
  readonly providerIcons: ProviderIconLookup;
  readonly onClose: () => void;
  readonly onAddRequested: () => void;
  /**
   * Click handler for a third-party connector card. Cli + agent built-ins
   * are NOT routed through this — they're handled inline by
   * ConnectMethodsBlock. Page-level wiring opens the sync_config detail
   * panel for the clicked connector.
   */
  readonly onConnectorClick: (c: AccessSurface) => void;
  /**
   * Hover feedback up into the explorer sidebar: while a row is hovered we
   * pass the scope's path so the matching folder gets the access-point
   * highlight; on leave / unmount we send null.
   */
  readonly onScopeHover?: (path: string | null) => void;
  /** Refresh scopes / connectors / repo identity after a CRUD mutation.
   *  Wired to useDataLayout().mutateRepo at page level. Typed as
   *  `Promise<unknown>` to match the SWR mutate() return value. */
  readonly onScopeMutated: () => Promise<unknown>;
  readonly onOpenAgentChat: (agentId: string, scopePath: string) => void;
  /** Drill into a specific scope's detail view from the Overview list,
   *  WITHOUT touching the file explorer. The panel maintains its own
   *  navigation state via selectedTargetKey in usePanelStore. The reverse
   *  direction (panel → file tree) is intentionally not wired: clicking
   *  a row should not yank the user out of their current document. */
  readonly onSelectScope: (targetKey: string) => void;
  /** Pop the drill-down state and return to the Overview. Only present
   *  when the user is currently *in* a drill-down (selectedTargetKey set);
   *  the natural folder-driven detail view has no back. */
  readonly onBack?: () => void;
  /** Open the shared create modal from Overview. The panel provides
   *  the entry point, but the modal owns folder selection, connector
   *  setup, loading, and error handling. */
  readonly onCreateRequested: () => void;
  readonly hideHeader?: boolean;
  readonly settingsPage?: boolean;
  readonly onOpenSettings?: () => void;
  readonly onNavigationGuardChange?: (
    guard: { readonly canLeave: () => boolean } | null,
  ) => void;
}

export function ScopedConnectorsListPanel({
  scope,
  scopes,
  currentScopePath,
  projectId,
  connectors,
  connectorsByTarget,
  providerIcons,
  onClose,
  onAddRequested,
  onConnectorClick,
  onScopeHover,
  onScopeMutated,
  onOpenAgentChat,
  onSelectScope,
  onBack,
  onCreateRequested,
  hideHeader = false,
  settingsPage = false,
  onOpenSettings,
  onNavigationGuardChange,
}: Props) {
  const cliConnector = useMemo(
    () => connectors.find((c) => isCliProvider(c.kind)),
    [connectors],
  );
  const gitRemoteConnector = useMemo(
    () => connectors.find((c) => isGitRemoteProvider(c.kind)),
    [connectors],
  );
  const agentConnector = useMemo(
    () => connectors.find((c) => isAgentProvider(c.kind)),
    [connectors],
  );
  // Workflows = third-party connectors. Built-ins
  // (cli / agent / git_remote) are surfaced via the CONNECT block
  // above (Terminal CLI / AI Agent / Git Remote cards), so they
  // must be excluded from this row to avoid double-rendering.
  const integrations = useMemo(
    () =>
      connectors.filter(
        (c) => !isBuiltInAccessProvider(c.kind),
      ),
    [connectors],
  );
  const apiBase = useMemo(() => getApiBase(), []);

  // Reported up by ScopeSettingsBlock — used to gate the panel chrome
  // (back / close) so we don't silently drop unsaved edits. Stable
  // callback below pushes this into local state.
  const [settingsDirty, setSettingsDirty] = useState(false);
  const handleSettingsDirtyChange = useCallback((dirty: boolean) => {
    setSettingsDirty(dirty);
  }, []);
  // Keep a ref alongside the state so close handlers see the latest
  // dirty value without depending on it (avoids stale closures in
  // event handlers wired up through PanelShell).
  const settingsDirtyRef = useRef(false);
  useEffect(() => {
    settingsDirtyRef.current = settingsDirty;
  }, [settingsDirty]);

  useEffect(() => {
    setSettingsDirty(false);
    settingsDirtyRef.current = false;
  }, [scope?.id, settingsPage]);

  // Confirm before discarding unsaved Settings edits when the user
  // backs out of Settings or closes the side panel entirely. Confirm()
  // is plain native — good enough for this destructive-but-recoverable
  // case. Returns true if the caller may proceed.
  const confirmDiscardIfDirty = useCallback((): boolean => {
    if (!settingsDirtyRef.current) return true;
    return globalThis.window?.confirm?.(
      'Discard unsaved Settings changes?',
    ) ?? true;
  }, []);

  useEffect(() => {
    if (!settingsPage || !onNavigationGuardChange) {
      onNavigationGuardChange?.(null);
      return undefined;
    }

    onNavigationGuardChange({ canLeave: confirmDiscardIfDirty });
    return () => onNavigationGuardChange(null);
  }, [
    settingsPage,
    confirmDiscardIfDirty,
    onNavigationGuardChange,
  ]);

  const handleClose = useCallback(() => {
    if (!confirmDiscardIfDirty()) return;
    onClose();
  }, [confirmDiscardIfDirty, onClose]);

  const handleBack = useCallback(() => {
    if (!onBack) return;
    if (!confirmDiscardIfDirty()) return;
    onBack();
  }, [onBack, confirmDiscardIfDirty]);

  const handleScopeDeleted = useCallback(() => {
    // Scope is gone — settings dirtiness is moot. Force the ref off so
    // the close handler doesn't double-confirm on the way out.
    settingsDirtyRef.current = false;
    setSettingsDirty(false);
    onClose();
  }, [onClose]);

  // Clear any lingering hover highlight when the panel unmounts or the
  // hovered scope changes (defensive: onMouseLeave handles the common case,
  // but a fast scope-switch can skip leave events).
  useEffect(() => {
    return () => onScopeHover?.(null);
  }, [scope?.id, onScopeHover]);

  const handleHoverEnter = useCallback(() => {
    if (scope) onScopeHover?.(scope.path);
  }, [scope, onScopeHover]);
  const handleHoverLeave = useCallback(() => {
    onScopeHover?.(null);
  }, [onScopeHover]);

  // Header content.
  //
  // Overview state: title reads "Access  N" with the count rendered
  // inline at the same font-size as the label, just muted — so the
  // user reads "I have 5 access points" the moment the panel opens
  // without having to scan a subtitle. (2026-05-08 UX feedback: the
  // count should not live on the second line as a faint metadata
  // line; it belongs in the headline.) Subtitle stays empty in this
  // state — the panel header alone is enough headline.
  //
  // Scope-selected state: header is only the scope's name. Path,
  // permission mode, and Settings live in the body metadata block.
  const headerTitle: ReactNode = scope ? (
    settingsPage ? 'Settings' : scope.name
  ) : (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
      <span>Access</span>
      <CountBadge
        value={scopes.length}
        size="md"
        tone="neutral"
      />
    </span>
  );
  const headerSubtitle = undefined;

  // [Settings] navigates to a sibling sub-page for the current scope.
  // It is intentionally not an inline expand/collapse affordance.
  const settingsButton = scope && onOpenSettings ? (
    <button
      type="button"
      onClick={onOpenSettings}
      title="Settings"
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        position: 'relative',
        width: 30,
        height: 30,
        padding: 0,
        fontSize: 12,
        fontWeight: 500,
        color: COLOR_FG_DIM,
        background: 'var(--po-hover)',
        border: '1px solid var(--po-border-subtle)',
        borderRadius: 7,
        cursor: 'pointer',
        flexShrink: 0,
        transition: 'background 150ms ease, color 150ms ease, border-color 150ms ease',
      }}
    >
      <SettingsGearIcon />
    </button>
  ) : undefined;

  return (
    <PanelShell
      title={headerTitle}
      subtitle={headerSubtitle}
      onClose={handleClose}
      onBack={onBack ? handleBack : undefined}
      headerRight={scope && !hideHeader ? settingsButton : undefined}
      hideHeader={hideHeader}
    >
      <div
        style={{
          display: 'flex',
          flexDirection: 'column',
          height: '100%',
          background: PANEL_BG,
        }}
      >
        <div
          style={{
            flex: 1,
            minHeight: 0,
            overflowY: 'auto',
            padding: '14px 12px 24px',
            display: 'flex',
            flexDirection: 'column',
            gap: 18,
          }}
        >
          {scope && settingsPage ? (
            <ScopeSettingsBlock
              scope={scope}
              projectId={projectId}
              onMutated={onScopeMutated}
              onScopeDeleted={handleScopeDeleted}
              onDirtyChange={handleSettingsDirtyChange}
            />
          ) : scope ? (
            <>
              {/* The three default ways to access this scope. cli + agent
                  rows in `connectors` back the scope key and in-app chat
                  agent surfaces. Puppyone CLI and Git Remote share the
                  scope access key; AI Agent opens the chat runtime. */}
              <ConnectMethodsBlock
                scope={scope}
                cliConnector={cliConnector}
                gitRemoteConnector={gitRemoteConnector}
                agentConnector={agentConnector}
                projectId={projectId}
                apiBase={apiBase}
                onScopeMutated={onScopeMutated}
                onOpenAgentChat={onOpenAgentChat}
              />

              <ScopeSummaryBar scope={scope} />

              {/* Workflows: third-party providers (notion / gmail /
                  github / url / ...). Empty state is itself the add
                  affordance, so it reads like an available slot instead
                  of adding another competing button to the panel. */}
              <section style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    padding: '0 2px',
                  }}
                >
                  <div
                    style={{
                      fontSize: 12,
                      fontWeight: 500,
                      color: COLOR_FG_DIM,
                    }}
                  >
                    Workflows
                  </div>
                </div>
                {integrations.length === 0 ? (
                  <EmptyWorkflowsSlot onClick={onAddRequested} />
                ) : (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                    {integrations.map((c) => (
                      <ConnectorCard
                        key={c.id}
                        connector={c}
                        providerIcons={providerIcons}
                        onClick={() => onConnectorClick(c)}
                        onHoverEnter={handleHoverEnter}
                        onHoverLeave={handleHoverLeave}
                      />
                    ))}
                  </div>
                )}
              </section>
            </>
          ) : (
            // Pp.1 Overview body:
            //   1. AllAccessPointsList — every scope in the project,
            //      click a row to drill into Pp.2a Detail.
            //   2. CreateAccessPointCTACard — sits beneath the list
            //      and opens the shared create modal. No inline form
            //      here so the Overview stays scannable and create
            //      state has one owner.
            <>
              <AllAccessPointsList
                scopes={scopes}
                connectorsByTarget={connectorsByTarget}
                providerIcons={providerIcons}
                currentScopePath={currentScopePath}
                onSelectScope={onSelectScope}
              />
              <CreateAccessPointCTACard onCreate={onCreateRequested} />
            </>
          )}
        </div>
      </div>
    </PanelShell>
  );
}

function EmptyWorkflowsSlot({ onClick }: { readonly onClick: () => void }) {
  const [hovered, setHovered] = useState(false);

  return (
    <button
      type="button"
      onClick={onClick}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        width: '100%',
        minHeight: 74,
        padding: '13px 14px',
        borderRadius: 8,
        border: `1px dashed ${hovered ? COLOR_BORDER_HOVER : 'var(--po-border)'}`,
        background: hovered
          ? 'color-mix(in srgb, var(--po-text) 4%, var(--po-panel) 96%)'
          : 'transparent',
        color: COLOR_FG,
        cursor: 'pointer',
        textAlign: 'left',
        transition: 'background 0.15s ease, border-color 0.15s ease',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, minWidth: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0 }}>
          <WorkflowHintIcon label="GitHub"><GithubIcon size={15} /></WorkflowHintIcon>
          <WorkflowHintIcon label="Notion"><NotionIcon size={15} /></WorkflowHintIcon>
          <WorkflowHintIcon label="Gmail"><GmailIcon size={15} /></WorkflowHintIcon>
        </div>
        <div style={{ minWidth: 0 }}>
          <div
            style={{
              fontSize: 12,
              fontWeight: 500,
              lineHeight: 1.35,
              color: COLOR_FG,
            }}
          >
            Add workflow
          </div>
          <div
            style={{
              marginTop: 2,
              fontSize: 12,
              lineHeight: 1.4,
              color: COLOR_FG_DIM,
            }}
          >
            Connect sources like GitHub, Notion, or Gmail.
          </div>
        </div>
      </div>
    </button>
  );
}

function WorkflowHintIcon({
  label,
  children,
}: {
  readonly label: string;
  readonly children: ReactNode;
}) {
  return (
    <span
      aria-label={label}
      title={label}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        width: 24,
        height: 24,
        borderRadius: 6,
        border: '1px solid var(--po-border-subtle)',
        background: 'color-mix(in srgb, var(--po-text) 3%, transparent)',
        color: COLOR_FG_DIM,
      }}
    >
      {children}
    </span>
  );
}

function ScopeSummaryBar({
  scope,
}: {
  scope: RepositoryView;
}) {
  const modeLabel = scope.max_mode === 'rw' ? 'Read & Write' : 'Read-only';
  const pathSegments = scope.path === ''
    ? []
    : scope.path.split('/').filter(Boolean);
  const pathTitle = scope.path === '' ? '/' : `/${scope.path}`;

  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: 'minmax(0, 1fr)',
        alignItems: 'start',
        padding: '4px 8px 0',
        minWidth: 0,
      }}
    >
      <div
        style={{
          display: 'flex',
          flexDirection: 'column',
          gap: 7,
          minWidth: 0,
        }}
      >
        <ScopeMetaLabel>Path</ScopeMetaLabel>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            minWidth: 0,
          }}
        >
          <ScopePathTrail segments={pathSegments} title={pathTitle} />
          <span
            style={{
              flexShrink: 0,
              fontSize: 12,
              fontWeight: 500,
              color: COLOR_FG_DIM,
              lineHeight: 1.2,
              whiteSpace: 'nowrap',
            }}
          >
            {modeLabel}
          </span>
        </div>
      </div>

    </div>
  );
}

function ScopeMetaLabel({ children }: { readonly children: ReactNode }) {
  return (
    <div
      style={{
        fontSize: 12,
        fontWeight: 500,
        color: COLOR_FG_DIM,
        lineHeight: 1.25,
      }}
    >
      {children}
    </div>
  );
}

function ScopePathTrail({
  segments,
  title,
}: {
  segments: readonly string[];
  title: string;
}) {
  const items = segments.length > 0 ? segments : ['Root'];

  return (
    <div
      title={title}
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 5,
        minWidth: 0,
        overflow: 'hidden',
      }}
    >
      {items.map((segment, index) => (
        <Fragment key={`${segment}-${index}`}>
          {index > 0 && (
            <span
              aria-hidden
              style={{
                flexShrink: 0,
                color: COLOR_FG_DIM,
                fontSize: 12,
                lineHeight: 1,
              }}
            >
              /
            </span>
          )}
          <span
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              gap: 5,
              minWidth: 0,
              flexShrink: index === items.length - 1 ? 1 : 0,
              color: index === items.length - 1 ? COLOR_FG : COLOR_FG_DIM,
              fontSize: 12,
              fontWeight: index === items.length - 1 ? 600 : 500,
              lineHeight: 1.2,
              whiteSpace: 'nowrap',
            }}
          >
            <span
              aria-hidden
              style={{
                display: 'inline-flex',
                width: 15,
                height: 15,
                flexShrink: 0,
              }}
            >
              <FolderIcon />
            </span>
            <span
              style={{
                minWidth: 0,
                overflow: 'hidden',
                textOverflow: 'ellipsis',
              }}
            >
              {segment}
            </span>
          </span>
        </Fragment>
      ))}
    </div>
  );
}

/** Lucide `settings-2` glyph: 4 sliders. Cleaner at 13×13 than the
 *  stock cog (which has too much detail to read at small sizes). */
function SettingsGearIcon() {
  return (
    <svg
      width="13"
      height="13"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="M20 7h-9" />
      <path d="M14 17H5" />
      <circle cx="17" cy="17" r="3" />
      <circle cx="7" cy="7" r="3" />
    </svg>
  );
}
