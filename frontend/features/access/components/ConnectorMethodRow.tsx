'use client';

import { useState, type ReactNode } from 'react';
import { StatusIndicator } from '@/components/ui/StatusDot';
import type { AccessSurface, RepositoryView } from '@/lib/repoApi';
import { getAccessProviderMethodMeta, isCliProvider, isMcpProvider, normalizeConnectorProvider } from '@/lib/accessProviderRegistry';
import { T } from '@/features/access/lib/tokens';
import { STATUS_COLORS, STATUS_LABEL } from '@/features/access/lib/constants';
import { isGitBuiltinProvider } from '@/features/access/lib/format';
import { ProviderIcon, RetryIcon } from '@/features/access/components/icons';
import { ConnectorDetailBody } from '@/features/access/components/ConnectorCard';
import type { ConnectorEditPatch } from '@/features/access/hooks/useAccessData';
import { GearIcon, formatScopePath } from '@/features/access/components/ScopeHeader';
import { ConnectorMethodCopyButton, ConnectorMethodPrompt } from '@/features/access/components/ConnectorMethodPrompt';
import { getProviderIconSize, getProviderTileSize, getProviderTileStyle } from '@/features/access/components/connectorVisuals';

export function ConnectorListRow({
  scope,
  connector,
  selected,
  showPromptPreview = false,
  onSelect,
  onConnect,
  onPauseResume,
  pending,
  showScopeLabel = false,
  showSettings = false,
  settingsOpen = false,
  onSettings,
  canManage = false,
}: {
  readonly scope: RepositoryView | undefined;
  readonly connector: AccessSurface;
  readonly selected: boolean;
  readonly showPromptPreview?: boolean;
  readonly onSelect: () => void;
  readonly onConnect: () => void;
  readonly onPauseResume: () => Promise<void> | void;
  readonly pending: boolean;
  readonly showScopeLabel?: boolean;
  readonly showSettings?: boolean;
  readonly settingsOpen?: boolean;
  readonly onSettings?: () => void;
  readonly canManage?: boolean;
}) {
  const [hovered, setHovered] = useState(false);
  const meta = getConnectorMethodMeta(connector);
  const paused = connector.status === 'paused';
  const tile = getProviderTileStyle(connector.kind, selected);
  const tileSize = getProviderTileSize(connector.kind);
  const iconSize = getProviderIconSize(connector.kind);
  const provider = normalizeConnectorProvider(connector.kind);
  const canConfigure = isCliProvider(provider) || connector.status === 'error' || !!connector.error_message;
  const canOpen = true;
  const scopeLabel = showScopeLabel && scope ? getScopeChipLabel(scope) : null;
  const scopeTitle = scope ? formatScopePath(scope) : undefined;
  const compactDescription = getCompactConnectorDescription(meta.description, connector.kind);
  const previewOpen = selected || showPromptPreview;

  if (paused && showPromptPreview) {
    return (
      <PausedConnectorPreview
        connector={connector}
        meta={meta}
        compactDescription={compactDescription}
        scopeLabel={scopeLabel}
        scopeTitle={scopeTitle}
        tile={tile}
        tileSize={tileSize}
        iconSize={iconSize}
        pending={pending}
        onTurnOn={onPauseResume}
        canManage={canManage}
      />
    );
  }

  return (
    <div
      onClick={canOpen ? onSelect : undefined}
      onKeyDown={(e) => {
        if (!canOpen) return;
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          onSelect();
        }
      }}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      role={canOpen ? 'button' : undefined}
      tabIndex={canOpen ? 0 : undefined}
      aria-pressed={canOpen ? selected : undefined}
      style={{
        minHeight: previewOpen ? 132 : 84,
        minWidth: 0,
        display: 'grid',
        gridTemplateColumns: previewOpen ? 'minmax(0, 1fr) minmax(220px, 240px)' : 'minmax(0, 1fr) max-content',
        alignItems: previewOpen ? 'stretch' : 'center',
        gap: previewOpen ? 16 : 14,
        padding: previewOpen ? '16px 18px' : '14px 16px',
        boxSizing: 'border-box',
        cursor: canOpen ? 'pointer' : 'default',
        background: selected
          ? 'color-mix(in srgb, var(--po-panel) 68%, var(--po-control) 32%)'
          : canOpen && hovered
            ? 'color-mix(in srgb, var(--po-panel) 78%, var(--po-control) 22%)'
            : 'transparent',
        transition: `background 0.15s ${T.ease}`,
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: previewOpen ? 'flex-start' : 'center',
          gap: previewOpen ? 14 : 12,
          minWidth: 0,
          alignSelf: previewOpen ? 'stretch' : undefined,
        }}
      >
        <div
          style={{
            height: tileSize,
            width: tileSize,
            borderRadius: isGitBuiltinProvider(connector.kind) ? 7 : 6,
            background: tile.background,
            border: `1px solid ${tile.border}`,
            color: tile.color,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            flexShrink: 0,
            boxShadow: tile.shadow,
            overflow: isGitBuiltinProvider(connector.kind) ? 'hidden' : undefined,
          }}
        >
          <ProviderIcon provider={connector.kind} size={iconSize} />
        </div>
        <div
          style={{
            minWidth: 0,
            display: 'flex',
            flexDirection: 'column',
            gap: previewOpen ? 9 : 5,
            flex: 1,
            minHeight: previewOpen ? 96 : undefined,
          }}
        >
          <div
            style={{
              minWidth: 0,
              display: 'flex',
              alignItems: 'center',
              gap: 7,
            }}
          >
            <div style={{ minWidth: 0, display: 'flex', alignItems: 'center', gap: 7 }}>
              <span
                style={{
                  minWidth: 0,
                  display: 'inline-flex',
                  alignItems: 'center',
                  fontSize: 14,
                  lineHeight: '18px',
                  fontWeight: 500,
                  color: T.text1,
                  fontFamily: T.fontSans,
                  whiteSpace: 'nowrap',
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                }}
                title={meta.title}
              >
                {meta.title}
              </span>
              {previewOpen ? <ConnectorInlineStatus status={connector.status} /> : null}
              {scopeLabel ? (
                <>
                  <ConnectorScopeBadge label={scopeLabel} title={scopeTitle} />
                  {scopeTitle === '/' ? (
                    <span
                      style={{
                        color: T.text3,
                        fontSize: 12,
                        lineHeight: '16px',
                        fontFamily: T.fontMono,
                        whiteSpace: 'nowrap',
                      }}
                    >
                      /
                    </span>
                  ) : null}
                </>
              ) : null}
            </div>
          </div>
          <div
            style={{
              fontSize: 12,
              color: T.text2,
              fontFamily: T.fontSans,
              lineHeight: '18px',
              fontWeight: 400,
              whiteSpace: previewOpen ? 'normal' : 'nowrap',
              overflow: previewOpen ? 'visible' : 'hidden',
              textOverflow: previewOpen ? undefined : 'ellipsis',
            }}
            title={meta.description}
          >
            {previewOpen ? meta.description : compactDescription}
          </div>
          {previewOpen ? (
            <div style={{ marginTop: 'auto', paddingTop: 10 }}>
              <ConnectorPreviewActions
                connector={connector}
                status={connector.status}
                pending={pending}
                onPauseResume={onPauseResume}
                onConnect={onConnect}
                onConfigure={onSelect}
                canConfigure={canConfigure}
                selected={selected}
                canManage={canManage}
              />
            </div>
          ) : null}
        </div>
      </div>
      {previewOpen ? (
        <ConnectorMethodPrompt connector={connector} scope={scope} onConnect={onConnect} />
      ) : (
        <ConnectorCollapsedActions
          connector={connector}
          scope={scope}
          status={connector.status}
          showSettings={showSettings}
          settingsOpen={settingsOpen}
          onSettings={onSettings}
          onOpen={onSelect}
          onConnect={onConnect}
        />
      )}
    </div>
  );
}

function PausedConnectorPreview({
  connector,
  meta,
  compactDescription,
  scopeLabel,
  scopeTitle,
  tile,
  tileSize,
  iconSize,
  pending,
  onTurnOn,
  canManage,
}: {
  readonly connector: AccessSurface;
  readonly meta: {
    readonly title: string;
    readonly description: string;
  };
  readonly compactDescription: string;
  readonly scopeLabel: string | null;
  readonly scopeTitle: string | undefined;
  readonly tile: ReturnType<typeof getProviderTileStyle>;
  readonly tileSize: number;
  readonly iconSize: number;
  readonly pending: boolean;
  readonly onTurnOn: () => Promise<void> | void;
  readonly canManage: boolean;
}) {
  return (
    <div
      style={{
        minHeight: 72,
        minWidth: 0,
        display: 'grid',
        gridTemplateColumns: 'minmax(0, 1fr) max-content',
        alignItems: 'center',
        gap: 16,
        padding: '14px 18px',
        boxSizing: 'border-box',
        background: 'transparent',
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 14,
          minWidth: 0,
          opacity: 0.72,
        }}
      >
        <div
          style={{
            height: tileSize,
            width: tileSize,
            borderRadius: isGitBuiltinProvider(connector.kind) ? 7 : 6,
            background: tile.background,
            border: `1px solid ${tile.border}`,
            color: tile.color,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            flexShrink: 0,
            boxShadow: tile.shadow,
            overflow: isGitBuiltinProvider(connector.kind) ? 'hidden' : undefined,
          }}
        >
          <ProviderIcon provider={connector.kind} size={iconSize} />
        </div>
        <div style={{ minWidth: 0, flex: 1, display: 'flex', flexDirection: 'column', gap: 5 }}>
          <div style={{ minWidth: 0, display: 'flex', alignItems: 'center', gap: 7 }}>
            <span
              title={meta.title}
              style={{
                minWidth: 0,
                color: T.text1,
                fontFamily: T.fontSans,
                fontSize: 14,
                lineHeight: '18px',
                fontWeight: 500,
                whiteSpace: 'nowrap',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
              }}
            >
              {meta.title}
            </span>
            {scopeLabel ? (
              <ConnectorScopeBadge label={scopeLabel} title={scopeTitle} />
            ) : null}
          </div>
          <div
            title={meta.description}
            style={{
              color: T.text2,
              fontFamily: T.fontSans,
              fontSize: 12,
              lineHeight: '18px',
              whiteSpace: 'nowrap',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
            }}
          >
            {compactDescription}
          </div>
        </div>
      </div>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'flex-end',
          gap: 10,
        }}
      >
        <span
          style={{
            color: T.text3,
            fontFamily: T.fontSans,
            fontSize: 12,
            lineHeight: '16px',
            whiteSpace: 'nowrap',
          }}
        >
          Off
        </span>
        {canManage ? <MethodOutlineButton
          label={pending ? 'Turning on' : 'Turn On'}
          tone='soft'
          disabled={pending}
          onClick={onTurnOn}
        /> : null}
      </div>
    </div>
  );
}

function ConnectorScopeBadge({
  label,
  title,
}: {
  readonly label: string;
  readonly title?: string;
}) {
  return (
    <span
      title={title ?? label}
      style={{
        maxWidth: 190,
        height: 24,
        padding: '0 10px',
        borderRadius: 999,
        border: `1px solid ${T.cardBorder}`,
        background: 'color-mix(in srgb, var(--po-control) 68%, var(--po-panel) 32%)',
        color: T.text2,
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        fontSize: 12,
        lineHeight: '16px',
        fontWeight: 600,
        fontFamily: label.startsWith('/') ? T.fontMono : T.fontSans,
        whiteSpace: 'nowrap',
        overflow: 'hidden',
        textOverflow: 'ellipsis',
        flexShrink: 1,
      }}
    >
      {label}
    </span>
  );
}

function ConnectorCollapsedActions({
  connector,
  scope,
  status,
  showSettings,
  settingsOpen,
  onSettings,
  onOpen,
  onConnect,
}: {
  readonly connector: AccessSurface;
  readonly scope: RepositoryView | undefined;
  readonly status: string;
  readonly showSettings: boolean;
  readonly settingsOpen: boolean;
  readonly onSettings?: () => void;
  readonly onOpen: () => void;
  readonly onConnect: () => void;
}) {
  const isMcp = isMcpProvider(connector.kind);
  return (
    <div
      onClick={(event) => event.stopPropagation()}
      style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'flex-end',
        gap: 10,
        minWidth: 0,
      }}
    >
      <ConnectorCompactStatus status={status} />
      {isMcp ? (
        <CollapsedPillButton
          label='Connect'
          icon={<ConnectionGlyph size={11} />}
          active={false}
          onClick={onConnect}
        />
      ) : (
        <ConnectorMethodCopyButton
          connector={connector}
          scope={scope}
          style={{
            height: 32,
            minWidth: 124,
            boxShadow: 'none',
          }}
        />
      )}
      {showSettings && onSettings ? (
        <CollapsedPillButton
          label='Settings'
          icon={<GearIcon size={11} />}
          active={settingsOpen}
          onClick={onSettings}
        />
      ) : null}
      <button
        type='button'
        aria-label='Expand connector'
        onClick={onOpen}
        style={{
          width: 24,
          height: 32,
          display: 'inline-flex',
          alignItems: 'center',
          justifyContent: 'center',
          border: 'none',
          borderRadius: 6,
          background: 'transparent',
          color: T.text2,
          cursor: 'pointer',
          fontFamily: T.fontSans,
          fontSize: 18,
          lineHeight: 1,
        }}
      >
        ›
      </button>
    </div>
  );
}

function CollapsedPillButton({
  label,
  icon,
  active,
  onClick,
}: {
  readonly label: string;
  readonly icon: ReactNode;
  readonly active: boolean;
  readonly onClick: () => void;
}) {
  const [hovered, setHovered] = useState(false);
  return (
    <button
      type='button'
      onClick={onClick}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        height: 32,
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        gap: 6,
        padding: '0 12px',
        borderRadius: 999,
        border: `1px solid ${active || hovered ? 'var(--po-border-strong)' : T.cardBorder}`,
        background: active || hovered
          ? 'color-mix(in srgb, var(--po-control) 76%, var(--po-panel) 24%)'
          : 'color-mix(in srgb, var(--po-control) 54%, var(--po-panel) 46%)',
        color: active ? T.text1 : T.text2,
        fontSize: 12,
        lineHeight: '16px',
        fontWeight: 500,
        fontFamily: T.fontSans,
        whiteSpace: 'nowrap',
        cursor: 'pointer',
        transition: `background 0.15s ${T.ease}, border-color 0.15s ${T.ease}, color 0.15s ${T.ease}`,
      }}
    >
      {icon}
      {label}
    </button>
  );
}

const ConnectionGlyph = ({ size = 12 }: { readonly size?: number }) => (
  <svg
    width={size}
    height={size}
    viewBox='0 0 16 16'
    fill='none'
    stroke='currentColor'
    strokeWidth='1.7'
    strokeLinecap='round'
    strokeLinejoin='round'
    aria-hidden
  >
    <path d='M6.7 4.4l1-1a3 3 0 0 1 4.2 4.2l-1 1' />
    <path d='M9.3 11.6l-1 1a3 3 0 0 1-4.2-4.2l1-1' />
    <path d='M6.4 9.6l3.2-3.2' />
  </svg>
);

export function getConnectorMethodMeta(connector: AccessSurface): {
  readonly title: string;
  readonly description: string;
} {
  return getAccessProviderMethodMeta(connector.kind, connector.name);
}

function getCompactConnectorDescription(description: string, providerName: string): string {
  if (isGitBuiltinProvider(providerName)) return 'Native Git clone and push.';
  if (isCliProvider(normalizeConnectorProvider(providerName))) return 'FS CLI · two-way access.';
  return description;
}

function ConnectorPreviewActions({
  connector,
  status,
  pending,
  onPauseResume,
  onConnect,
  onConfigure,
  canConfigure,
  selected,
  canManage,
}: {
  readonly connector: AccessSurface;
  readonly status: string;
  readonly pending: boolean;
  readonly onPauseResume: () => Promise<void> | void;
  readonly onConnect: () => void;
  readonly onConfigure: () => void;
  readonly canConfigure: boolean;
  readonly selected: boolean;
  readonly canManage: boolean;
}) {
  const isGitRemote = isGitBuiltinProvider(connector.kind);
  const isCli = isCliProvider(normalizeConnectorProvider(connector.kind));
  const isMcp = isMcpProvider(connector.kind);
  const action = status === 'error'
    ? null
    : status === 'paused'
      ? canManage ? { label: pending ? 'Turning on' : 'Turn On', icon: undefined, onClick: onPauseResume, active: false, tone: 'soft' as const } : null
      : isGitRemote
      ? { label: 'View Git remote', icon: <ExternalLinkGlyph size={12} />, onClick: onConnect, active: false, tone: 'outline' as const }
      : !canManage
        ? null
      : isMcp
        ? { label: selected ? 'Hide config' : 'Show config', icon: <ChevronDownGlyph size={12} rotated={selected} />, onClick: onConfigure, active: selected, tone: 'outline' as const }
      : isCli || canConfigure
        ? { label: selected ? 'Hide config' : 'Configure CLI', icon: <GearIcon size={12} />, onClick: onConfigure, active: selected, tone: 'outline' as const }
        : null;
  const paused = status === 'paused';

  return (
    <div
      onClick={(event) => event.stopPropagation()}
      style={{
        display: 'flex',
        minWidth: 0,
        alignItems: 'flex-start',
        justifyContent: 'flex-start',
        flexDirection: 'column',
        gap: 8,
      }}
    >
      {status === 'error' && canManage ? (
        <RowActionButton
          label='Retry'
          icon={<RetryIcon size={10} />}
          disabled={pending}
          onClick={onPauseResume}
        />
      ) : action ? (
        <MethodOutlineButton
          label={action.label}
          icon={action.icon}
          active={action.active}
          tone={action.tone}
          disabled={paused && pending}
          onClick={action.onClick}
        />
      ) : null}
    </div>
  );
}

function ConnectorInlineStatus({
  status,
}: {
  readonly status: string;
}) {
  const label = status === 'active' ? 'Active' : STATUS_LABEL[status] ?? status;
  return (
    <>
      <span aria-hidden style={{ color: T.text4, fontSize: 12, lineHeight: '16px' }}>·</span>
      <StatusIndicator status={status} label={label} style={{ flexShrink: 0 }} />
    </>
  );
}

function ConnectorCompactStatus({
  status,
}: {
  readonly status: string;
}) {
  const isOn = status === 'active' || status === 'syncing';
  const label = isOn ? 'Active' : status === 'paused' ? 'Paused' : STATUS_LABEL[status] ?? status;
  return <StatusIndicator status={status} label={label} />;
}

function MethodOutlineButton({
  label,
  icon,
  active = false,
  tone = 'outline',
  disabled = false,
  onClick,
}: {
  readonly label: string;
  readonly icon?: ReactNode;
  readonly active?: boolean;
  readonly tone?: 'outline' | 'primary' | 'soft';
  readonly disabled?: boolean;
  readonly onClick: () => void;
}) {
  const [hovered, setHovered] = useState(false);
  const primary = tone === 'primary';
  const soft = tone === 'soft';
  return (
    <button
      type='button'
      disabled={disabled}
      onClick={(event) => {
        event.stopPropagation();
        if (disabled) return;
        onClick();
      }}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        height: 30,
        minWidth: soft ? 96 : 132,
        borderRadius: 7,
        border: `1px solid ${primary ? T.text1 : active || hovered ? 'var(--po-border-strong)' : soft ? T.cardBorder : T.border}`,
        background: primary
          ? hovered && !disabled
            ? 'color-mix(in srgb, var(--po-text) 90%, var(--po-canvas) 10%)'
            : T.text1
          : soft
            ? hovered && !disabled
              ? 'color-mix(in srgb, var(--po-control) 72%, var(--po-panel) 28%)'
              : 'color-mix(in srgb, var(--po-control) 48%, var(--po-panel) 52%)'
            : active || hovered
              ? 'color-mix(in srgb, var(--po-panel) 72%, var(--po-control) 28%)'
              : 'transparent',
        color: primary ? T.bg : active || (soft && hovered && !disabled) ? T.text1 : T.text2,
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        gap: 8,
        padding: '0 11px',
        fontSize: 12,
        lineHeight: '16px',
        fontWeight: 500,
        fontFamily: T.fontSans,
        cursor: disabled ? 'wait' : 'pointer',
        opacity: disabled ? 0.58 : 1,
        whiteSpace: 'nowrap',
        transition: `background 0.15s ${T.ease}, border-color 0.15s ${T.ease}, color 0.15s ${T.ease}, opacity 0.15s ${T.ease}`,
      }}
    >
      <span>{label}</span>
      {icon ?? null}
    </button>
  );
}

function RowActionButton({
  icon,
  label,
  tone = 'neutral',
  disabled,
  onClick,
}: {
  readonly icon?: ReactNode;
  readonly label: string;
  readonly tone?: 'neutral' | 'success';
  readonly disabled: boolean;
  readonly onClick: () => Promise<void> | void;
}) {
  const [hovered, setHovered] = useState(false);
  const successTone = tone === 'success';
  const border = successTone
    ? 'color-mix(in srgb, var(--po-success) 38%, transparent)'
    : hovered
      ? 'var(--po-border-strong)'
      : T.border;
  const background = successTone
    ? hovered
      ? 'color-mix(in srgb, var(--po-success) 20%, var(--po-panel) 80%)'
      : 'color-mix(in srgb, var(--po-success) 14%, var(--po-panel) 86%)'
    : hovered
      ? 'var(--po-hover)'
      : 'transparent';
  const color = successTone ? 'var(--po-success)' : T.text2;

  return (
    <button
      type='button'
      disabled={disabled}
      onClick={(e) => {
        e.stopPropagation();
        void onClick();
      }}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        justifySelf: 'end',
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        gap: 6,
        height: successTone ? 30 : 28,
        padding: successTone ? '0 12px' : '0 10px',
        borderRadius: 6,
        border: `1px solid ${border}`,
        background,
        color,
        fontSize: 12,
        fontWeight: 500,
        fontFamily: T.fontSans,
        cursor: disabled ? 'not-allowed' : 'pointer',
        opacity: disabled ? 0.5 : 1,
        transition: `background 0.15s ${T.ease}, border-color 0.15s ${T.ease}, color 0.15s ${T.ease}`,
      }}
    >
      {icon ?? null}
      {label}
    </button>
  );
}

export function ConnectorExpandedDetail({
  connector,
  scope,
  pending,
  onPauseResume,
  onUpdate,
}: {
  readonly connector: AccessSurface;
  readonly scope: RepositoryView | undefined;
  readonly pending: boolean;
  readonly onPauseResume: () => Promise<void> | void;
  readonly onUpdate: (patch: ConnectorEditPatch) => Promise<void>;
}) {
  const provider = normalizeConnectorProvider(connector.kind);
  const showError = connector.status === 'error' || !!connector.error_message;
  const showInlineDetail = isCliProvider(provider) || isMcpProvider(provider);
  if (!showError && !showInlineDetail) return null;

  return (
    <div
      style={{
        borderTop: `1px solid ${T.cardBorder}`,
        background: 'color-mix(in srgb, var(--po-control) 76%, var(--po-panel) 24%)',
      }}
    >
      {showError ? (
        <ConnectorManagementStrip
          connector={connector}
          pending={pending}
          onPauseResume={onPauseResume}
          withDivider={showInlineDetail}
        />
      ) : null}
      {showInlineDetail ? (
        <ConnectorDetailBody
          connector={connector}
          scope={scope}
          onPauseResume={onPauseResume}
          onUpdate={onUpdate}
          pending={pending}
          variant='inline'
        />
      ) : null}
    </div>
  );
}

const ChevronDownGlyph = ({
  size = 12,
  rotated = false,
}: {
  readonly size?: number;
  readonly rotated?: boolean;
}) => (
  <svg
    width={size}
    height={size}
    viewBox='0 0 16 16'
    fill='none'
    stroke='currentColor'
    strokeWidth='1.8'
    strokeLinecap='round'
    strokeLinejoin='round'
    aria-hidden
    style={{
      transform: rotated ? 'rotate(180deg)' : 'rotate(0deg)',
      transition: `transform 0.15s ${T.ease}`,
    }}
  >
    <path d='M4 6l4 4 4-4' />
  </svg>
);

function ConnectorManagementStrip({
  connector,
  pending,
  onPauseResume,
  withDivider,
}: {
  readonly connector: AccessSurface;
  readonly pending: boolean;
  readonly onPauseResume: () => Promise<void> | void;
  readonly withDivider?: boolean;
}) {
  const statusColor = STATUS_COLORS[connector.status] ?? T.text2;
  const statusLabel = STATUS_LABEL[connector.status] ?? connector.status;
  const description =
    connector.error_message || 'This method needs attention before it can be used.';

  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: 'minmax(0, 1fr) max-content',
        alignItems: 'center',
        gap: 14,
        padding: '14px 16px',
        borderBottom: withDivider ? `1px solid ${T.cardBorder}` : 'none',
      }}
    >
      <div style={{ minWidth: 0, display: 'flex', flexDirection: 'column', gap: 3 }}>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            minWidth: 0,
            fontFamily: T.fontSans,
            fontSize: 12,
            lineHeight: '16px',
          }}
        >
          <span style={{ color: T.text1, fontWeight: 500 }}>Connector status</span>
          <span aria-hidden style={{ color: T.text4 }}>·</span>
          <span style={{ color: statusColor, fontWeight: 400 }}>{statusLabel}</span>
        </div>
        <div
          style={{
            color: T.text2,
            fontFamily: T.fontSans,
            fontSize: 12,
            lineHeight: '16px',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}
          title={description}
        >
          {description}
        </div>
      </div>
      <RowActionButton
        label='Retry'
        icon={<RetryIcon size={10} />}
        disabled={pending}
        onClick={onPauseResume}
      />
    </div>
  );
}

const ExternalLinkGlyph = ({ size = 12 }: { readonly size?: number }) => (
  <svg width={size} height={size} viewBox='0 0 24 24' fill='none' stroke='currentColor' strokeWidth='2.1' strokeLinecap='round' strokeLinejoin='round' aria-hidden>
    <path d='M14 3h7v7' />
    <path d='M10 14 21 3' />
    <path d='M21 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5' />
  </svg>
);

function getScopeChipLabel(scope: RepositoryView): string {
  const path = formatScopePath(scope);
  return path === '/' ? 'Root' : path;
}
