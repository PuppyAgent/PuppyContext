'use client';

import { useState, type ReactNode } from 'react';
import { StatusDot, StatusIndicator } from '@/components/ui/StatusDot';
import type { AccessSurface, RepositoryView } from '@/lib/repoApi';
import { T } from '@/features/access/lib/tokens';
import { STATUS_LABEL } from '@/features/access/lib/constants';
import { SectionLabel } from '@/features/access/components/ui-blocks';

// ─── ScopePageHeader ─────────────────────────────────────────────────
//
// h1 of the right pane. Displays the scope's *name* (first-class
// editable field independent of the scope path) and a tiny meta
// line summarizing aggregate health across the scope's connectors.
//
// The visual pattern mirrors `ConnectorCard`'s own header so the user
// can instantly tell that a scope is conceptually "the same kind of
// surface" as a connector — just one level up. Path information
// (where this scope lives on disk + its read/write mode + perm
// badges) lives in the compact strip below; we deliberately keep the
// title lean.
//
// Aggregate status:
//   - any errored             → 'Error'   (red dot)
//   - any syncing             → 'Syncing' (blue dot)
//   - all active              → 'Active'  (green dot)
//   - all paused              → 'Paused'  (amber dot)
//   - mixed (some on/off)     → 'Mixed'   (amber dot)
//   - empty                   → no meta line
//
interface ScopeAggregateStatus {
  readonly key: 'empty' | 'active' | 'syncing' | 'paused' | 'mixed' | 'error';
  readonly label: string;
}

function computeAggregate(connectors: readonly AccessSurface[]): ScopeAggregateStatus {
  if (connectors.length === 0) {
    return { key: 'empty', label: 'No connectors' };
  }
  if (connectors.some((c) => c.status === 'error')) {
    return { key: 'error', label: STATUS_LABEL.error };
  }
  if (connectors.some((c) => c.status === 'syncing')) {
    return { key: 'syncing', label: STATUS_LABEL.syncing };
  }
  if (connectors.every((c) => c.status === 'active')) {
    return { key: 'active', label: STATUS_LABEL.active };
  }
  if (connectors.every((c) => c.status === 'paused')) {
    return { key: 'paused', label: STATUS_LABEL.paused };
  }
  return { key: 'mixed', label: 'Mixed' };
}

export function ScopePageHeader({
  scope,
  connectors,
  settingsOpen,
  settingsDirty,
  onToggleSettings,
  canManage,
}: {
  readonly scope: RepositoryView | undefined;
  readonly connectors: readonly AccessSurface[];
  readonly settingsOpen: boolean;
  readonly settingsDirty: boolean;
  readonly onToggleSettings: () => void;
  readonly canManage: boolean;
}) {
  const titleText = scope?.name?.trim() || 'Untitled scope';
  const aggregate = computeAggregate(connectors);
  const isWorkspaceWide = scope?.target.kind === 'project_root';
  const pathLabel = isWorkspaceWide ? '/' : `/${scope?.path ?? ''}`;
  const modeLabel = scope?.max_mode === 'rw' ? 'Read & write' : 'Read only';

  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'flex-start',
        gap: 12,
        marginBottom: 28,
        minWidth: 0,
      }}
    >
      <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 6 }}>
        <h1
          style={{
            margin: 0,
            fontSize: 20,
            lineHeight: 1.25,
            fontWeight: 600,
            letterSpacing: '-0.015em',
            color: T.text1,
            fontFamily: T.fontSans,
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}
          title={titleText}
        >
          {titleText}
        </h1>
        {connectors.length > 0 && aggregate.key !== 'empty' ? (
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              fontSize: 12,
              color: T.text2,
              fontFamily: T.fontSans,
              minWidth: 0,
            }}
          >
            <StatusIndicator status={aggregate.key} label={aggregate.label} style={{ flexShrink: 0 }} />
            <span style={{ color: T.text4, flexShrink: 0 }}>·</span>
            <span style={{ color: T.text2, flexShrink: 0, fontWeight: 400 }}>
              {connectors.length === 1 ? '1 connector' : `${connectors.length} connectors`}
            </span>
          </div>
        ) : null}
        <div
          style={{
            display: 'flex',
            alignItems: 'baseline',
            gap: 8,
            minWidth: 0,
            fontFamily: T.fontSans,
            fontSize: 12,
            lineHeight: '16px',
            flexWrap: 'wrap',
          }}
        >
          <span
            style={{
              flexShrink: 0,
              color: T.text2,
              fontWeight: 400,
            }}
          >
            {isWorkspaceWide ? 'Project repository' : 'Scope'}
          </span>
          <span
            style={{
              minWidth: 0,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
              color: T.text2,
              fontFamily: T.fontMono,
            }}
            title={pathLabel}
          >
            {pathLabel}
          </span>
          <span aria-hidden style={{ color: T.text4, flexShrink: 0 }}>·</span>
          <span style={{ color: T.text2, flexShrink: 0 }}>
            {modeLabel}
          </span>
        </div>
      </div>
      <div style={{ flexShrink: 0, marginTop: 2, display: 'flex', alignItems: 'center', gap: 8 }}>
        {canManage ? <SettingsHeaderButton
          active={settingsOpen}
          dirty={settingsDirty}
          onClick={onToggleSettings}
        /> : null}
      </div>
    </div>
  );
}

export function formatScopePath(scope: RepositoryView): string {
  if (scope.target.kind === 'project_root') return '/';
  return `/${scope.path}`;
}

function SettingsHeaderButton({
  active,
  dirty,
  onClick,
}: {
  readonly active: boolean;
  readonly dirty: boolean;
  readonly onClick: () => void;
}) {
  const [hovered, setHovered] = useState(false);
  return (
    <button
      type='button'
      onClick={onClick}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      aria-pressed={active}
      aria-label={active ? 'Close scope settings' : 'Open scope settings'}
      title={active ? 'Close settings' : 'Open settings'}
      style={{
        position: 'relative',
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        width: 30,
        height: 30,
        borderRadius: 6,
        border: `1px solid ${active ? 'var(--po-border-strong)' : T.border}`,
        background: active ? 'var(--po-hover)' : hovered ? 'var(--po-hover)' : 'transparent',
        color: active || hovered ? T.text1 : T.text2,
        cursor: 'pointer',
        transition: `background 0.15s ${T.ease}, color 0.15s ${T.ease}, border-color 0.15s ${T.ease}`,
      }}
    >
      <GearIcon size={13} />
      {dirty ? (
        <StatusDot
          status="warning"
          style={{
            position: 'absolute',
            top: 5,
            right: 5,
          }}
        />
      ) : null}
    </button>
  );
}

export function SettingsSection({
  open,
  children,
}: {
  readonly open: boolean;
  readonly children: ReactNode;
}) {
  if (!open) return null;
  return (
    <div style={{ marginBottom: 22 }}>
      <SectionLabel>Settings</SectionLabel>
      <div
        id='puppyone-access-scope-settings-body'
        style={{
          padding: '14px 14px 12px',
          borderRadius: 10,
          background: 'var(--po-control)',
          border: `1px solid ${T.cardBorder}`,
          animation: `puppyone-access-settings-slide 180ms ${T.ease}`,
        }}
      >
        {children}
      </div>
    </div>
  );
}

export const GearIcon = ({ size = 13 }: { readonly size?: number }) => (
  <svg width={size} height={size} viewBox='0 0 24 24' fill='none' stroke='currentColor' strokeWidth='2' strokeLinecap='round' strokeLinejoin='round' aria-hidden>
    <circle cx='12' cy='12' r='3' />
    <path d='M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.6 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 8.92 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9c.14.3.22.63.22 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1Z' />
  </svg>
);
