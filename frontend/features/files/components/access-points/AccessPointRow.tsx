'use client';

import { StatusDot } from '@/components/ui/StatusDot';
import { ProviderIcon } from '@/features/access/components/icons';
import { AccessPointProviderIcon } from '@/features/files/components/access-points/AccessPointProviderIcon';
import { accessSurfaceIcon, providerLabel } from '@/features/files/components/access-points/labels';
import {
  ACCESS_PANEL_TYPOGRAPHY,
  COLOR_BORDER,
  COLOR_BORDER_HOVER,
  COLOR_FG,
  COLOR_FG_DIM,
  COLOR_FG_MUTED,
  FONT_MONO,
} from '@/features/files/components/access-points/tokens';
import type { ProviderIconLookup } from '@/features/files/components/access-points/types';
import {
  isAgentProvider,
  isBuiltInAccessProvider,
  isCliProvider,
  isGitRemoteProvider,
} from '@/lib/accessProviderRegistry';
import { AI_AGENT_ENABLED } from '@/lib/featureFlags';
import type { AccessSurface, RepositoryView } from '@/lib/repoApi';
import { useMemo, useState } from 'react';

/** Cap on inline integration glyphs before the strip collapses the
 *  remainder into a `+N` chip. Five fits cleanly in a single row
 *  alongside the three built-ins on most panel widths because chips
 *  are now icon-only (no inline label). The detail view always
 *  shows the full list. */
const INTEGRATION_VISIBLE_CAP = 5;

/** Inline provider signal size. These sit on the second line beside
 *  the title line, so they should read as small status marks rather
 *  than primary actions while still using the shared 11px meta type. */
const CHIP_SIZE = 20;

const ROW_BG =
  'color-mix(in srgb, var(--po-control) 42%, transparent)';
const ROW_BG_HOVER =
  'color-mix(in srgb, var(--po-control) 62%, transparent)';
const ROW_BG_CURRENT =
  'color-mix(in srgb, var(--po-access-active-bg) 70%, var(--po-control) 30%)';
const ROW_BORDER_CURRENT =
  'color-mix(in srgb, var(--po-access-active-border) 70%, var(--po-border) 30%)';

/**
 * AccessPointRow — one access-point row in the overview list.
 *
 * The row is the element: status, display name, path, permission, and
 * provider logos all live inside one clickable access-point object.
 */
export function AccessPointRow({
  scope,
  connectors,
  providerIcons,
  isCurrent,
  onClick,
}: {
  readonly scope: RepositoryView;
  /** Connectors bound to this scope. We split into the built-ins
   *  (cli / git_remote / agent) and any third-party
   *  integrations. Empty array (frozen at the call site) when the
   *  DB trigger hasn't settled — row degrades to "no chips" without
   *  crashing. */
  readonly connectors: readonly AccessSurface[];
  readonly providerIcons: ProviderIconLookup;
  /** True iff this scope's path equals the folder the user is
   *  currently viewing in the file tree. */
  readonly isCurrent: boolean;
  readonly onClick: () => void;
}) {
  const [hovered, setHovered] = useState(false);

  // Pluck the built-ins by provider id. Scope creation guarantees
  // one of each per scope, but we read them
  // defensively in case the trigger hasn't settled yet on a fresh
  // insert.
  const cliConnector = connectors.find((c) => isCliProvider(c.kind));
  const gitRemoteConnector = connectors.find((c) => isGitRemoteProvider(c.kind));
  const agentConnector = connectors.find((c) => isAgentProvider(c.kind));

  // Active flags for the chip-render gate. We hide a built-in chip
  // when its connector is paused — the strip is a positive
  // statement of "what's on", not a state diagram of every method
  // that exists. (The detail view is where on/off lives.)
  const cliActive = cliConnector?.status === 'active';
  const gitRemoteActive = gitRemoteConnector?.status === 'active';
  const agentActive = agentConnector?.status === 'active';

  // Third-party integrations. Same hide-paused rule as the built-ins
  // — only `status !== 'paused'` integrations make it into the
  // strip. We keep `syncing` / `error` visible because the user
  // probably wants to see those at a glance (errors especially).
  const integrations = useMemo(
    () =>
      connectors.filter(
        (c) =>
          !isBuiltInAccessProvider(c.kind) &&
          c.status !== 'paused',
      ),
    [connectors],
  );
  const visibleIntegrations = integrations.slice(0, INTEGRATION_VISIBLE_CAP);
  const hiddenIntegrations = integrations.length - visibleIntegrations.length;

  const pathDisplay = scope.target.kind === 'project_root' ? '/' : `/${scope.path}`;
  const displayName = scope.target.kind === 'project_root'
    ? 'Root'
    : scope.name || scope.path.split('/').filter(Boolean).pop() || scope.path;

  const background = isCurrent ? ROW_BG_CURRENT : hovered ? ROW_BG_HOVER : ROW_BG;
  const borderColor = isCurrent ? ROW_BORDER_CURRENT : hovered ? COLOR_BORDER_HOVER : COLOR_BORDER;
  const active = connectors.some((c) => c.status === 'active' || c.status === 'syncing');
  const activeMethodCount =
    Number(cliActive) +
    Number(gitRemoteActive) +
    Number(AI_AGENT_ENABLED && agentActive) +
    visibleIntegrations.length +
    hiddenIntegrations;
  const methodSummary =
    activeMethodCount === 0
      ? 'No active connectors'
      : `${activeMethodCount} active ${activeMethodCount === 1 ? 'connector' : 'connectors'}`;

  return (
    <button
      type="button"
      onClick={onClick}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      title={`${displayName} · ${pathDisplay}`}
      style={{
        width: '100%',
        textAlign: 'left',
        display: 'flex',
        alignItems: 'center',
        gap: 11,
        minWidth: 0,
        minHeight: 66,
        padding: '9px 11px',
        borderRadius: 8,
        border: `1px solid ${borderColor}`,
        background,
        color: COLOR_FG,
        fontFamily: 'var(--po-font-sans)',
        cursor: 'pointer',
        transition: 'border-color 0.15s, background 0.15s',
        boxShadow: 'none',
        appearance: 'none',
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          flexShrink: 0,
          width: 34,
        }}
      >
        <ScopeGlyph active={active} selected={isCurrent} />
      </div>

      <div
        style={{
          display: 'flex',
          flexDirection: 'column',
          gap: 6,
          flex: '1 1 auto',
          minWidth: 0,
        }}
      >
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 10,
            minWidth: 0,
          }}
        >
          <span
            style={{
              minWidth: 0,
              flex: '1 1 auto',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
              ...ACCESS_PANEL_TYPOGRAPHY.title,
              color: COLOR_FG,
            }}
          >
            {displayName}
          </span>
          {/* Provider signals — built-ins first in fixed order, then
              active third-party integrations, then a `+N` overflow. */}
          <div
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              gap: 4,
              minWidth: 0,
              flexShrink: 0,
            }}
          >
            {cliActive && (
              <ProviderSignal
                provider="cli"
                label="CLI"
                selected={isCurrent}
                title="Puppyone CLI · active"
              />
            )}
            {gitRemoteActive && (
              <ProviderSignal
                provider="git_remote"
                label="Git"
                selected={isCurrent}
                title="Git Remote · active"
              />
            )}
            {/* AI Agent chip — gated on the AI_AGENT_ENABLED flag. The
                agent connector still exists per-scope (auto-INSERTed by
                the DB trigger) and `agentActive` still reads its status,
                but we don't surface a chip while the feature is hidden. */}
            {AI_AGENT_ENABLED && agentActive && (
              <ProviderSignal
                provider="agent"
                label="AI"
                selected={isCurrent}
                title="AI Agent · active"
              />
            )}
            {visibleIntegrations.map((c) => (
              <IntegrationChip
                key={c.id}
                connector={c}
                providerIcons={providerIcons}
              />
            ))}
            {hiddenIntegrations > 0 && (
              <span
                title={`+${hiddenIntegrations} more`}
                style={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  minWidth: CHIP_SIZE,
                  height: CHIP_SIZE,
                  padding: '0 5px',
                  ...ACCESS_PANEL_TYPOGRAPHY.meta,
                  borderRadius: 5,
                  color: COLOR_FG_MUTED,
                  background: 'transparent',
                  border: `1px dashed ${COLOR_BORDER_HOVER}`,
                  fontVariantNumeric: 'tabular-nums',
                  whiteSpace: 'nowrap',
                }}
              >
                +{hiddenIntegrations}
              </span>
            )}
          </div>
        </div>

        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            minWidth: 0,
          }}
        >
          <span
            style={{
              flex: '1 1 auto',
              minWidth: 0,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
              ...ACCESS_PANEL_TYPOGRAPHY.body,
              color: isCurrent ? COLOR_FG_MUTED : COLOR_FG_DIM,
              fontFamily: FONT_MONO,
            }}
          >
            {pathDisplay}
          </span>
          <span
            style={{
              flexShrink: 0,
              ...ACCESS_PANEL_TYPOGRAPHY.body,
              color: COLOR_FG_DIM,
              whiteSpace: 'nowrap',
            }}
          >
            {methodSummary}
          </span>
        </div>
      </div>

      <span
        aria-hidden
        style={{
          alignSelf: 'center',
          display: 'inline-flex',
          alignItems: 'center',
          justifyContent: 'center',
          width: 24,
          height: 24,
          borderRadius: 6,
          color: hovered || isCurrent ? COLOR_FG_MUTED : COLOR_FG_DIM,
          transition: 'color 0.15s, transform 0.15s',
          transform: hovered ? 'translateX(2px)' : 'translateX(0)',
          flexShrink: 0,
        }}
      >
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="9 18 15 12 9 6" />
        </svg>
      </span>
    </button>
  );
}

function ScopeGlyph({
  active,
  selected,
}: {
  readonly active: boolean;
  readonly selected: boolean;
}) {
  return (
    <span
      aria-hidden
      style={{
        position: 'relative',
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        width: 32,
        height: 32,
        borderRadius: 8,
        border: `1px solid ${selected ? ROW_BORDER_CURRENT : COLOR_BORDER}`,
        background: selected ? 'var(--po-access-active-bg)' : 'color-mix(in srgb, var(--po-control) 46%, transparent)',
        color: selected ? 'var(--po-access-active-text)' : COLOR_FG_MUTED,
        flexShrink: 0,
      }}
    >
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.1" strokeLinecap="round" strokeLinejoin="round">
        <path d="M9 17H7A5 5 0 0 1 7 7h2" />
        <path d="M15 7h2a5 5 0 1 1 0 10h-2" />
        <line x1="8" y1="12" x2="16" y2="12" />
      </svg>
      <StatusDot
        status={active ? 'active' : 'inactive'}
        style={{
          position: 'absolute',
          right: -1,
          bottom: -1,
          width: 6,
          height: 6,
          border: '1px solid var(--po-canvas)',
          boxSizing: 'border-box',
        }}
      />
    </span>
  );
}

/**
 * ProviderSignal — tiny built-in connector logo for the overview row.
 * Mirrors the dedicated Access sidebar's second-line connector signal:
 * mono/custom provider glyph, 18px square, quiet neutral chrome.
 */
function ProviderSignal({
  provider,
  label,
  selected,
  title,
}: {
  readonly provider: string;
  readonly label: string;
  readonly selected: boolean;
  readonly title: string;
}) {
  const isGit = isGitRemoteProvider(provider);

  return (
    <span
      title={title}
      aria-label={title}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        gap: 4,
        minWidth: CHIP_SIZE,
        height: CHIP_SIZE,
        padding: '0 6px',
        borderRadius: 5,
        background: selected
          ? 'var(--po-hover)'
          : 'color-mix(in srgb, var(--po-hover) 55%, transparent)',
        border: `1px solid ${selected ? 'var(--po-border-strong)' : COLOR_BORDER}`,
        color: selected ? COLOR_FG_MUTED : COLOR_FG_DIM,
        flexShrink: 0,
        opacity: selected ? 1 : 0.9,
        boxShadow: 'none',
      }}
    >
      <ProviderIcon
        provider={provider}
        size={isGit ? 13 : 10}
        variant="mono"
      />
      <span style={{ ...ACCESS_PANEL_TYPOGRAPHY.meta, lineHeight: 1 }}>
        {label}
      </span>
    </span>
  );
}

/**
 * IntegrationChip — icon-only chip for a third-party connector.
 *
 * Neutral surface (matches the chip-family visual language without
 * pulling brand colour into the row chrome itself) with the brand
 * glyph from `AccessPointProviderIcon` centered inside. Tooltip
 * carries the connector's display name.
 */
function IntegrationChip({
  connector,
  providerIcons,
}: {
  readonly connector: AccessSurface;
  readonly providerIcons: ProviderIconLookup;
}) {
  const ep = useMemo(() => accessSurfaceIcon(connector), [connector]);
  const label = connector.name || providerLabel(connector.kind);
  return (
    <span
      title={label}
      aria-label={label}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        width: CHIP_SIZE,
        height: CHIP_SIZE,
        borderRadius: 5,
        background: 'color-mix(in srgb, var(--po-control) 44%, transparent)',
        border: `1px solid ${COLOR_BORDER}`,
        color: COLOR_FG,
        flexShrink: 0,
      }}
    >
      <span
        aria-hidden
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          justifyContent: 'center',
          width: 16,
          height: 16,
          transform: 'scale(0.75)',
        }}
      >
        <AccessPointProviderIcon ep={ep} providerIcons={providerIcons} />
      </span>
    </span>
  );
}
