'use client';

import { PulseGrid } from '@/components/loading';
import { AiHandoffButton } from '@/components/ui/AiHandoffButton';
import { CountBadge } from '@/components/ui/CountBadge';
import { DialogBody, DialogHeader, DialogRoot, DialogSurface } from '@/components/ui/Dialog';
import { ModalPortal } from '@/components/ui/ModalPortal';
import { StatusIndicator } from '@/components/ui/StatusDot';
import { ProviderIcon } from '@/features/access/components/icons';
import { CommandBlock } from '@/features/access/components/ui-blocks';
import { STATUS_LABEL } from '@/features/access/lib/constants';
import { getApiBase, getTypeLine, timeAgo } from '@/features/access/lib/format';
import { T } from '@/features/access/lib/tokens';
import { CliCredentialIssuePanel } from '@/features/files/components/access-points/connect-methods/CliCredentialIssuePanel';
import { GitCredentialIssuePanel } from '@/features/files/components/access-points/connect-methods/GitCredentialIssuePanel';
import {
  accessPointProfileSlug,
  buildGitSyncPrompt,
  buildTerminalCliPrompt,
} from '@/lib/accessPointCliPrompt';
import {
  getAccessProviderCardTitle,
  getAccessProviderMethodMeta,
  getAccessProviderPromptKind,
  isCliProvider,
  isGitRemoteProvider,
} from '@/lib/accessProviderRegistry';
import { canonicalGitUrlForTarget } from '@/lib/gitRemote';
import {
  repositoryViewKey,
  sortAccessSurfacesBuiltinFirst,
  type AccessSurface,
  type RepositoryView,
} from '@/lib/repoApi';
import { APP_Z_INDEX } from '@/lib/zIndex';
import { ArrowLeft, BookOpen, ChevronRight, ExternalLink, Plus } from 'lucide-react';
import { useMemo, useRef, useState } from 'react';

const PROMPT_BOX_BG = 'color-mix(in srgb, var(--po-inset) 92%, var(--po-panel) 8%)';
const FONT_BODY = 13;
const FONT_META = 12;
const HOVER_CARD_WIDTH = 372;
const HOVER_CARD_HEIGHT = 196;

type DataAccessQuickModalProps = {
  readonly scope: RepositoryView;
  readonly connectors: readonly AccessSurface[];
  readonly onClose: () => void;
  readonly onCreateAccess: (path: string) => void;
  readonly onOpenFullSettings: (targetKey: string) => void;
};

export function DataAccessQuickModal({
  scope,
  connectors,
  onClose,
  onCreateAccess,
  onOpenFullSettings,
}: DataAccessQuickModalProps) {
  const [manualConnectorId, setManualConnectorId] = useState<string | null>(null);
  const methods = useMemo(() => {
    const sorted = sortAccessSurfacesBuiltinFirst(connectors);
    const cliMethods = sorted.filter((connector) => isCliProvider(connector.kind));
    const gitRemote = sorted.find((connector) => isGitRemoteProvider(connector.kind));

    return gitRemote ? [...cliMethods, gitRemote] : cliMethods;
  }, [connectors]);
  const manualConnector = useMemo(
    () => methods.find((connector) => connector.id === manualConnectorId) ?? null,
    [manualConnectorId, methods],
  );

  return (
    <DialogRoot open onClose={onClose} backdrop="strong">
      <DialogSurface width={760} ariaLabel={manualConnector ? 'Manual commands' : 'Share with AI'}>
        <DialogHeader
          title={manualConnector ? 'Manual commands' : 'Share with AI'}
          description={
            manualConnector
              ? `${accessMethodMeta(manualConnector).title} · ${formatScopeDescription(scope)}`
              : <HeaderAccessLine scope={scope} />
          }
          onClose={onClose}
        >
          <button
            type="button"
            onClick={() => onOpenFullSettings(repositoryViewKey(scope))}
            style={headerActionStyle}
          >
            <ExternalLink size={13} />
            Full settings
          </button>
        </DialogHeader>

        <DialogBody style={{ padding: '12px 20px 18px' }}>
          {manualConnector ? (
            <ManualCommandsPage
              connector={manualConnector}
              scope={scope}
              onBack={() => setManualConnectorId(null)}
            />
          ) : (
            <div
              style={{
                border: `1px solid ${T.cardBorder}`,
                borderRadius: 8,
                background: 'var(--po-panel)',
                overflow: 'hidden',
              }}
            >
              <AccessMethodsPanel
                connectors={methods}
                scope={scope}
                onOpenManualCommands={setManualConnectorId}
              />

              <button
                type="button"
                onClick={() => onCreateAccess(scope.path)}
                style={addMethodStyle}
              >
                <Plus size={15} />
                Add method
              </button>
            </div>
          )}
        </DialogBody>
      </DialogSurface>
    </DialogRoot>
  );
}

function HeaderAccessLine({ scope }: { readonly scope: RepositoryView }) {
  return (
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
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
        }}
      >
        {formatScopeDescription(scope)}
      </span>
    </div>
  );
}

function AccessMethodsPanel({
  connectors,
  scope,
  onOpenManualCommands,
}: {
  readonly connectors: readonly AccessSurface[];
  readonly scope: RepositoryView;
  readonly onOpenManualCommands: (connectorId: string) => void;
}) {
  const sorted = useMemo(
    () => sortAccessSurfacesBuiltinFirst(connectors),
    [connectors],
  );

  if (sorted.length === 0) {
    return (
      <div
        style={{
          minHeight: 172,
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          gap: 10,
          padding: '18px 14px',
          color: T.text3,
          fontSize: FONT_BODY,
          fontFamily: T.fontSans,
        }}
      >
        <PulseGrid size="sm" />
        <span>Preparing access methods</span>
      </div>
    );
  }

  return (
    <section style={{ padding: 14, display: 'flex', flexDirection: 'column', gap: 10 }}>
      {sorted.map((connector) => (
        <AccessMethodCard
          key={connector.id}
          connector={connector}
          scope={scope}
          onOpenManualCommands={onOpenManualCommands}
        />
      ))}
    </section>
  );
}

function AccessMethodCard({
  connector,
  scope,
  onOpenManualCommands,
}: {
  readonly connector: AccessSurface;
  readonly scope: RepositoryView;
  readonly onOpenManualCommands: (connectorId: string) => void;
}) {
  const setup = useMemo(() => buildSetupGuide(connector, scope), [connector, scope]);
  const [copied, setCopied] = useState(false);
  const meta = accessMethodMeta(connector);
  const showManualCommands = isGitRemoteProvider(connector.kind) || isCliProvider(connector.kind);

  const copyPrompt = async () => {
    if (!setup.prompt) return;
    try {
      await navigator.clipboard.writeText(setup.prompt);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      /* clipboard unavailable */
    }
  };

  return (
    <div
      style={{
        borderRadius: 8,
        border: `1px solid ${T.cardBorder}`,
        background: 'var(--po-panel)',
        overflow: 'hidden',
      }}
    >
      <div
        style={{
          minHeight: 116,
          display: 'grid',
          gridTemplateColumns: 'minmax(0, 1fr) 236px',
          gap: 12,
          alignItems: 'stretch',
          padding: 10,
        }}
      >
        <div style={{ minWidth: 0, display: 'flex', gap: 10 }}>
          <ProviderTile provider={connector.kind} />
          <div style={{ minWidth: 0, display: 'flex', flexDirection: 'column', gap: 8 }}>
            <div style={{ minWidth: 0, display: 'flex', alignItems: 'center', gap: 7 }}>
              <span
                style={{
                  minWidth: 0,
                  color: T.text1,
                  fontSize: FONT_BODY,
                  lineHeight: '18px',
                  fontWeight: 600,
                  fontFamily: T.fontSans,
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}
              >
                {meta.title}
              </span>
              {meta.badge ? <MethodBadge>{meta.badge}</MethodBadge> : null}
              {showManualCommands ? (
                <button
                  type="button"
                  onClick={() => onOpenManualCommands(connector.id)}
                  style={{
                    marginLeft: 'auto',
                    height: 24,
                    border: 'none',
                    background: 'transparent',
                    display: 'inline-flex',
                    alignItems: 'center',
                    gap: 5,
                    padding: '0 2px',
                    color: T.text3,
                    fontSize: FONT_META,
                    fontWeight: 500,
                    fontFamily: T.fontSans,
                    cursor: 'pointer',
                    whiteSpace: 'nowrap',
                    flexShrink: 0,
                  }}
                >
                  <ChevronRight
                    size={12}
                    style={{
                      color: T.text4,
                    }}
                  />
                  {isCliProvider(connector.kind) ? 'Generate key' : 'Manual commands'}
                </button>
              ) : null}
            </div>
            <div
              style={{
                color: T.text3,
                fontSize: FONT_META,
                lineHeight: '18px',
                fontFamily: T.fontSans,
              }}
            >
              {meta.description}
            </div>
            <ScopeAssuranceRow
              scope={scope}
              status={connector.status}
              lastUsed={connector.last_activity_at}
            />
          </div>
        </div>

        <MethodPromptPreview
          prompt={setup.prompt}
          copied={copied}
          onCopy={copyPrompt}
        />
      </div>
    </div>
  );
}

function ManualCommandsPage({
  connector,
  scope,
  onBack,
}: {
  readonly connector: AccessSurface;
  readonly scope: RepositoryView;
  readonly onBack: () => void;
}) {
  const setup = useMemo(() => buildSetupGuide(connector, scope), [connector, scope]);
  const meta = accessMethodMeta(connector);

  return (
    <div
      style={{
        border: `1px solid ${T.cardBorder}`,
        borderRadius: 8,
        background: 'var(--po-panel)',
        overflow: 'hidden',
      }}
    >
      <div
        style={{
          padding: '12px 14px',
          borderBottom: `1px solid ${T.cardBorder}`,
          display: 'flex',
          alignItems: 'center',
          gap: 12,
        }}
      >
        <button
          type="button"
          onClick={onBack}
          style={{
            height: 30,
            borderRadius: 6,
            border: `1px solid ${T.cardBorder}`,
            background: 'transparent',
            color: T.text2,
            display: 'inline-flex',
            alignItems: 'center',
            gap: 6,
            padding: '0 10px',
            fontSize: FONT_META,
            fontWeight: 500,
            fontFamily: T.fontSans,
            cursor: 'pointer',
            flexShrink: 0,
          }}
        >
          <ArrowLeft size={13} />
          Access methods
        </button>
        <ProviderTile provider={connector.kind} />
        <div style={{ minWidth: 0 }}>
          <div
            style={{
              color: T.text1,
              fontSize: FONT_BODY,
              lineHeight: '18px',
              fontWeight: 600,
              fontFamily: T.fontSans,
            }}
          >
            {meta.title}
          </div>
          <div
            style={{
              color: T.text4,
              fontSize: FONT_META,
              lineHeight: '16px',
              fontFamily: T.fontSans,
            }}
          >
            {setup.manualTitle}
          </div>
        </div>
      </div>
      <div style={{ padding: '14px 16px 18px' }}>
        {isGitRemoteProvider(connector.kind) ? (
          <div style={{ marginBottom: 14 }}>
            <GitCredentialIssuePanel
              connectorId={connector.id}
              gitUrl={canonicalGitUrlForTarget(getApiBase(), scope.target)}
              scopeMode={scope.max_mode}
              target={scope.target}
            />
          </div>
        ) : null}
        {isCliProvider(connector.kind) ? (
          <CliCredentialIssuePanel
            connectorId={connector.id}
            target={scope.target}
          >
            {(credential) => (
              <ConnectionStepsList steps={buildSetupGuide(connector, scope, credential).steps} />
            )}
          </CliCredentialIssuePanel>
        ) : (
          <ConnectionStepsList steps={setup.steps} />
        )}
      </div>
    </div>
  );
}

function MethodPromptPreview({
  prompt,
  copied,
  onCopy,
}: {
  readonly prompt: string;
  readonly copied: boolean;
  readonly onCopy: () => void;
}) {
  return (
    <div
      style={{
        position: 'relative',
        minWidth: 0,
        minHeight: 96,
        borderRadius: 7,
        border: `1px solid ${T.cardBorder}`,
        background: PROMPT_BOX_BG,
        overflow: 'hidden',
      }}
    >
      <pre
        aria-hidden
        style={{
          margin: 0,
          padding: '9px 10px 30px',
          color: 'color-mix(in srgb, var(--po-text) 58%, var(--po-text-muted) 42%)',
          fontFamily: T.fontMono,
          fontSize: FONT_META,
          lineHeight: 1.55,
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
          maxHeight: 96,
          overflow: 'hidden',
        }}
      >
        {prompt || 'Generate a one-time credential to build this setup prompt.'}
      </pre>
      <div
        aria-hidden
        style={{
          position: 'absolute',
          inset: 0,
          background: `linear-gradient(180deg, color-mix(in srgb, ${PROMPT_BOX_BG} 18%, transparent) 0%, ${PROMPT_BOX_BG} 82%)`,
          pointerEvents: 'none',
        }}
      />
      <AiHandoffButton
        copied={copied}
        onClick={onCopy}
        disabled={!prompt}
        style={{
          position: 'absolute',
          left: '50%',
          top: '50%',
          transform: 'translate(-50%, -50%)',
        }}
      />
    </div>
  );
}

function MethodBadge({ children }: { readonly children: string }) {
  return (
    <span
      style={{
        height: 20,
        display: 'inline-flex',
        alignItems: 'center',
        padding: '0 7px',
        borderRadius: 999,
        border: '1px solid color-mix(in srgb, var(--po-success) 34%, transparent)',
        color: 'var(--po-success)',
        background: 'color-mix(in srgb, var(--po-success) 10%, transparent)',
        fontSize: FONT_META,
        lineHeight: '16px',
        fontWeight: 600,
        fontFamily: T.fontSans,
        whiteSpace: 'nowrap',
      }}
    >
      {children}
    </span>
  );
}

function ScopeAssuranceRow({
  scope,
  status,
  lastUsed,
}: {
  readonly scope: RepositoryView;
  readonly status: AccessSurface['status'] | null;
  readonly lastUsed: string | null;
}) {
  const chips = [
    scope.max_mode === 'rw' ? 'Read & write' : 'Read only',
    'Scope-bound',
    status ? (STATUS_LABEL[status] ?? status) : 'Preparing',
    lastUsed ? `Last used ${timeAgo(lastUsed)}` : 'Not used yet',
  ];

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
      {chips.map((chip) => (
        <span
          key={chip}
          style={{
            minHeight: 24,
            display: 'inline-flex',
            alignItems: 'center',
            padding: '0 8px',
            borderRadius: 999,
            border: `1px solid ${T.cardBorder}`,
            color: T.text2,
            background: 'var(--po-panel)',
            fontSize: FONT_META,
            lineHeight: '16px',
            fontFamily: T.fontSans,
            whiteSpace: 'nowrap',
          }}
        >
          {chip}
        </span>
      ))}
    </div>
  );
}

function ManualWaysPanel({
  connectors,
  scope,
}: {
  readonly connectors: readonly AccessSurface[];
  readonly scope: RepositoryView;
}) {
  if (connectors.length === 0) return null;

  return (
    <section style={{ padding: '12px 14px 10px', borderBottom: `1px solid ${T.cardBorder}` }}>
      <div
        style={{
          marginBottom: 8,
          color: T.text4,
          fontSize: FONT_META,
          lineHeight: '16px',
          fontWeight: 600,
          fontFamily: T.fontSans,
          textTransform: 'uppercase',
          letterSpacing: 0,
        }}
      >
        Manual ways in
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {connectors.map((connector) => (
          <ManualWayRow
            key={connector.id}
            connector={connector}
            scope={scope}
          />
        ))}
      </div>
    </section>
  );
}

function ManualWayRow({
  connector,
  scope,
}: {
  readonly connector: AccessSurface;
  readonly scope: RepositoryView;
}) {
  const setup = useMemo(() => buildSetupGuide(connector, scope), [connector, scope]);
  const [copied, setCopied] = useState(false);

  const copyPrompt = async () => {
    try {
      await navigator.clipboard.writeText(setup.prompt);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      /* clipboard unavailable */
    }
  };

  return (
    <div
      style={{
        minHeight: 48,
        display: 'grid',
        gridTemplateColumns: 'minmax(0, 1fr) max-content',
        alignItems: 'center',
        gap: 12,
        padding: '9px 10px',
        borderRadius: 7,
        border: `1px solid ${T.cardBorder}`,
        background: 'var(--po-panel)',
      }}
    >
      <div style={{ minWidth: 0, display: 'flex', alignItems: 'center', gap: 10 }}>
        <ProviderTile provider={connector.kind} />
        <div style={{ minWidth: 0, display: 'flex', flexDirection: 'column', gap: 3 }}>
          <span
            style={{
              minWidth: 0,
              color: T.text1,
              fontSize: FONT_BODY,
              fontWeight: 600,
              fontFamily: T.fontSans,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
          >
            {connectorName(connector)}
          </span>
          <span
            style={{
              minWidth: 0,
              color: T.text3,
              fontSize: FONT_META,
              lineHeight: '16px',
              fontFamily: T.fontSans,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
          >
            {isGitRemoteProvider(connector.kind)
              ? 'For clone, pull, commit, and push workflows.'
              : getTypeLine(connector)}
          </span>
        </div>
      </div>
      <AiHandoffButton
        onClick={copyPrompt}
        copied={copied}
      />
    </div>
  );
}

function MethodRow({
  connector,
  scope,
  isFirst,
}: {
  readonly connector: AccessSurface;
  readonly scope: RepositoryView;
  readonly isFirst: boolean;
}) {
  const name = connectorName(connector);

  return (
    <div
      style={{
        minHeight: 68,
        display: 'grid',
        gridTemplateColumns: 'minmax(190px, 1fr) minmax(76px, 0.36fr) minmax(74px, max-content) max-content',
        alignItems: 'center',
        gap: 12,
        padding: '12px 14px',
        borderTop: isFirst ? 'none' : `1px solid ${T.cardBorder}`,
        background: 'var(--po-panel)',
        boxSizing: 'border-box',
        position: 'relative',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, minWidth: 0 }}>
        <ProviderTile provider={connector.kind} />
        <div style={{ minWidth: 0, display: 'flex', flexDirection: 'column', gap: 4 }}>
          <span
            title={name}
            style={{
              minWidth: 0,
              color: T.text1,
              fontSize: FONT_BODY,
              fontWeight: 600,
              fontFamily: T.fontSans,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
          >
            {name}
          </span>
          <span
            title={getTypeLine(connector)}
            style={{
              minWidth: 0,
              color: T.text3,
              fontSize: FONT_META,
              lineHeight: '16px',
              fontFamily: T.fontSans,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
          >
            {getTypeLine(connector)}
          </span>
        </div>
      </div>

      <div style={{ minWidth: 0, display: 'flex', flexDirection: 'column', gap: 3 }}>
        <span style={{ color: T.text4, fontSize: FONT_META, lineHeight: '16px', fontFamily: T.fontSans }}>
          Last used
        </span>
        <span
          title={timeAgo(connector.last_activity_at)}
          style={{
            minWidth: 0,
            color: T.text2,
            fontSize: FONT_META,
            lineHeight: '16px',
            fontWeight: 500,
            fontFamily: T.fontSans,
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}
        >
          {timeAgo(connector.last_activity_at)}
        </span>
      </div>

      <StatusCell status={connector.status} />
      <SetupGuideHover connector={connector} scope={scope} />
    </div>
  );
}

function SetupGuideHover({
  connector,
  scope,
}: {
  readonly connector: AccessSurface;
  readonly scope: RepositoryView;
}) {
  const setup = useMemo(() => buildSetupGuide(connector, scope), [connector, scope]);
  const [open, setOpen] = useState(false);
  const [cardPosition, setCardPosition] = useState<HoverCardPosition | null>(null);
  const closeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);

  const openCard = () => {
    if (closeTimerRef.current) {
      clearTimeout(closeTimerRef.current);
      closeTimerRef.current = null;
    }
    const rect = buttonRef.current?.getBoundingClientRect();
    if (rect) {
      const dialogRect = buttonRef.current
        ?.closest('[role="dialog"]')
        ?.getBoundingClientRect() ?? null;
      setCardPosition(computeHoverCardPosition(rect, dialogRect));
    }
    setOpen(true);
  };

  const closeCardSoon = () => {
    if (closeTimerRef.current) clearTimeout(closeTimerRef.current);
    closeTimerRef.current = setTimeout(() => setOpen(false), 120);
  };

  return (
    <div
      onMouseEnter={openCard}
      onMouseLeave={closeCardSoon}
      onFocus={openCard}
      onBlur={closeCardSoon}
      style={{
        justifySelf: 'end',
        position: 'relative',
        display: 'inline-flex',
        zIndex: open ? 20 : 1,
      }}
    >
      <button
        ref={buttonRef}
        type="button"
        aria-expanded={open}
        onClick={openCard}
        style={{
          ...setupButtonStyle,
          borderColor: open ? 'var(--po-border-strong)' : T.cardBorder,
          color: open ? T.text1 : T.text2,
        }}
      >
        <BookOpen size={12} />
        Setup guide
      </button>
      {open && cardPosition ? (
        <PromptHoverCard
          setup={setup}
          position={cardPosition}
          onMouseEnter={openCard}
          onMouseLeave={closeCardSoon}
        />
      ) : null}
    </div>
  );
}

function PromptHoverCard({
  setup,
  position,
  onMouseEnter,
  onMouseLeave,
}: {
  readonly setup: ReturnType<typeof buildSetupGuide>;
  readonly position: HoverCardPosition;
  readonly onMouseEnter: () => void;
  readonly onMouseLeave: () => void;
}) {
  return (
    <ModalPortal>
      <div
        onMouseEnter={onMouseEnter}
        onMouseLeave={onMouseLeave}
        onFocus={onMouseEnter}
        onBlur={onMouseLeave}
        style={{
          position: 'fixed',
          top: position.top,
          left: position.left,
          width: HOVER_CARD_WIDTH,
          borderRadius: 8,
          border: `1px solid ${T.cardBorder}`,
          background: 'var(--po-overlay)',
          boxShadow: '0 18px 36px color-mix(in srgb, var(--po-shadow) 72%, transparent)',
          padding: 8,
          zIndex: APP_Z_INDEX.modalNested,
          boxSizing: 'border-box',
        }}
      >
        <div
          aria-hidden
          style={{
            position: 'absolute',
            left: position.arrowLeft,
            top: position.placement === 'below' ? -5 : undefined,
            bottom: position.placement === 'above' ? -5 : undefined,
            width: 10,
            height: 10,
            transform: 'rotate(45deg)',
            background: 'var(--po-overlay)',
            borderLeft: position.placement === 'below' ? `1px solid ${T.cardBorder}` : undefined,
            borderTop: position.placement === 'below' ? `1px solid ${T.cardBorder}` : undefined,
            borderRight: position.placement === 'above' ? `1px solid ${T.cardBorder}` : undefined,
            borderBottom: position.placement === 'above' ? `1px solid ${T.cardBorder}` : undefined,
          }}
        />
        <PromptPreview prompt={setup.prompt} />
        <ManualSetup setup={setup} />
      </div>
    </ModalPortal>
  );
}

type HoverCardPosition = {
  readonly top: number;
  readonly left: number;
  readonly arrowLeft: number;
  readonly placement: 'below' | 'above';
};

function computeHoverCardPosition(rect: DOMRect, dialogRect: DOMRect | null): HoverCardPosition {
  const margin = 16;
  const boundsLeft = dialogRect ? dialogRect.left + 20 : margin;
  const boundsRight = dialogRect ? dialogRect.right - 20 : window.innerWidth - margin;
  const boundsBottom = dialogRect ? dialogRect.bottom - 16 : window.innerHeight - margin;
  const centeredLeft = rect.left + rect.width / 2 - HOVER_CARD_WIDTH / 2;
  const left = Math.min(
    boundsRight - HOVER_CARD_WIDTH,
    Math.max(boundsLeft, centeredLeft),
  );
  const belowTop = rect.bottom + 8;
  const aboveTop = rect.top - HOVER_CARD_HEIGHT - 8;
  const placement =
    belowTop + HOVER_CARD_HEIGHT <= boundsBottom || aboveTop < margin ? 'below' : 'above';
  const top = placement === 'below' ? belowTop : Math.max(margin, aboveTop);
  const arrowLeft = Math.min(
    HOVER_CARD_WIDTH - 24,
    Math.max(18, rect.left + rect.width / 2 - left - 5),
  );

  return { top, left, arrowLeft, placement };
}

function ManualSetup({
  setup,
}: {
  readonly setup: ReturnType<typeof buildSetupGuide>;
}) {
  const [open, setOpen] = useState(false);

  return (
    <div style={{ marginTop: 8 }}>
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        style={{
          all: 'unset',
          cursor: 'pointer',
          minHeight: 30,
          width: '100%',
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          color: T.text2,
          fontFamily: T.fontSans,
        }}
      >
        <span
          aria-hidden
          style={{
            width: 12,
            display: 'inline-flex',
            alignItems: 'center',
            justifyContent: 'center',
            color: T.text3,
            transform: open ? 'rotate(90deg)' : 'rotate(0deg)',
            transition: `transform 0.15s ${T.ease}`,
            flexShrink: 0,
          }}
        >
          <svg width={10} height={10} viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round">
            <path d="M4 2.5 7.5 6 4 9.5" />
          </svg>
        </span>
        <span
          style={{
            flex: 1,
            minWidth: 0,
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
            fontSize: FONT_META,
            lineHeight: '18px',
          }}
        >
          <strong style={{ color: T.text2, fontWeight: 600 }}>Terminal setup</strong>
          <span> · {setup.manualTitle} · {setup.manualDescription}</span>
        </span>
      </button>
      {open ? (
        <div style={{ padding: '8px 0 2px 20px' }}>
          <ConnectionStepsList steps={setup.steps} />
        </div>
      ) : null}
    </div>
  );
}

function ConnectionStepsList({
  steps,
}: {
  readonly steps: ReadonlyArray<{ title: string; lines: readonly string[] }>;
}) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      {steps.map((step, index) => (
        <div key={step.title} style={{ display: 'flex', gap: 10 }}>
          <span style={{ marginTop: 1 }}>
            <CountBadge value={index + 1} size="sm" tone="neutral" />
          </span>
          <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 6 }}>
            <span style={{ color: T.text1, fontSize: FONT_META, fontWeight: 600, fontFamily: T.fontSans }}>
              {step.title}
            </span>
            <CommandBlock lines={step.lines} />
          </div>
        </div>
      ))}
    </div>
  );
}

function PromptPreview({ prompt }: { readonly prompt: string }) {
  const [copied, setCopied] = useState(false);

  const copyPrompt = async () => {
    if (!prompt) return;
    try {
      await navigator.clipboard.writeText(prompt);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      /* clipboard unavailable */
    }
  };

  return (
    <div
      style={{
        position: 'relative',
        height: 132,
        borderRadius: 8,
        border: `1px solid ${T.cardBorder}`,
        background: PROMPT_BOX_BG,
        overflow: 'hidden',
      }}
    >
      <pre
        aria-hidden
        style={{
          margin: 0,
          padding: '34px 12px 50px',
          color: 'color-mix(in srgb, var(--po-text) 68%, var(--po-text-muted) 32%)',
          fontFamily: T.fontMono,
          fontSize: FONT_META,
          lineHeight: 1.6,
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
        }}
      >
        {prompt || 'Generate a one-time credential from this access method before copying setup.'}
      </pre>
      <div
        aria-hidden
        style={{
          position: 'absolute',
          top: 11,
          left: 12,
          right: 12,
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          color: T.text2,
          fontFamily: T.fontMono,
          fontSize: FONT_META,
          lineHeight: '18px',
          pointerEvents: 'none',
        }}
      >
        <span style={{ color: T.text1 }}># setup prompt</span>
        <span
          style={{
            flex: 1,
            height: 1,
            background: 'color-mix(in srgb, var(--po-border) 55%, transparent)',
          }}
        />
      </div>
      <div
        aria-hidden
        style={{
          position: 'absolute',
          inset: 'auto 0 0 0',
          height: 60,
          background: `linear-gradient(180deg, transparent 0%, ${PROMPT_BOX_BG} 92%)`,
          pointerEvents: 'none',
        }}
      />
      <AiHandoffButton
        onClick={copyPrompt}
        copied={copied}
        disabled={!prompt}
        label="Copy setup prompt"
        style={{
          position: 'absolute',
          left: '50%',
          bottom: 9,
          transform: 'translateX(-50%)',
        }}
      />
    </div>
  );
}

function ProviderTile({ provider }: { readonly provider: string }) {
  const tile = getProviderTileStyle(provider);
  const isGitRemote = isGitRemoteProvider(provider);
  const tileSize = isGitRemote ? 34 : 30;
  const iconSize = isGitRemote ? 34 : 17;

  return (
    <div
      style={{
        width: tileSize,
        height: tileSize,
        borderRadius: isGitRemote ? 7 : 6,
        background: tile.background,
        border: `1px solid ${tile.border}`,
        color: tile.color,
        boxShadow: tile.shadow,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        flexShrink: 0,
        overflow: isGitRemote ? 'hidden' : undefined,
      }}
    >
      <ProviderIcon provider={provider} size={iconSize} />
    </div>
  );
}

function buildSetupGuide(connector: AccessSurface, scope: RepositoryView, cliCredential?: string) {
  const scopeName = scope.name || scope.path || 'Root';
  const promptKind = getAccessProviderPromptKind(connector.kind);
  if (promptKind === 'git_remote') {
    const gitUrl = canonicalGitUrlForTarget(getApiBase(), scope.target);
    const guide = buildGitSyncPrompt({
      gitUrl,
      scopeName,
      directoryName: scopeName,
    });
    return {
      title: 'Set up Git Remote',
      description: 'Copy this prompt when an agent should clone or push this folder.',
      prompt: guide.prompt,
      manualTitle: 'Git commands',
      manualDescription: 'Run these commands yourself when you want direct Git access.',
      steps: [
        { title: 'Clone into a folder', lines: guide.cloneLines },
        { title: 'Connect an existing folder', lines: guide.existingFolderLines },
        { title: 'Daily workflow', lines: guide.workflowLines },
      ],
    };
  }

  const accessKey = cliCredential ?? '';
  if (!accessKey) {
    return {
      title: 'Access this folder by an AI agent',
      description: 'Generate a one-time CLI key before copying setup.',
      prompt: '',
      manualTitle: 'Puppyone CLI',
      manualDescription: 'Generate a key to reveal runnable commands.',
      steps: [],
    };
  }
  const guide = buildTerminalCliPrompt({
    apiBase: getApiBase(),
    accessKey,
    profileName: accessPointProfileSlug(scope.name || scope.path || 'root'),
    scopeName,
  });
  return {
    title: 'Access this folder by an AI agent',
    description: 'Copy this prompt into Codex, Cursor, or Claude.',
    prompt: guide.prompt,
    manualTitle: 'Puppyone CLI',
    manualDescription: 'Run these commands yourself in terminal.',
    steps: [
      { title: 'Install once', lines: [guide.installLine] },
      { title: 'Sign in to this scope', lines: [guide.loginLine] },
      { title: 'Explore safely', lines: guide.exploreLines },
      { title: 'Read & write files', lines: guide.fileLines },
    ],
  };
}

function getProviderTileStyle(provider: string) {
  if (isCliProvider(provider)) {
    return {
      background: 'var(--po-accent)',
      border: 'var(--po-accent)',
      color: 'var(--po-text-inverse)',
      shadow: '0 1px 2px var(--po-shadow)',
    };
  }
  return {
    background: 'var(--po-text-inverse)',
    border: T.border,
    color: T.text2,
    shadow: 'none',
  };
}

function StatusCell({ status }: { readonly status: AccessSurface['status'] }) {
  return (
    <StatusIndicator
      status={status}
      label={STATUS_LABEL[status] ?? status}
      style={{
        justifySelf: 'end',
      }}
    />
  );
}

function connectorName(connector: AccessSurface): string {
  return getAccessProviderCardTitle(connector.kind, connector.name);
}

function accessMethodMeta(connector: AccessSurface): {
  readonly title: string;
  readonly description: string;
  readonly badge?: string;
} {
  const meta = getAccessProviderMethodMeta(connector.kind, connector.name);
  return {
    ...meta,
    badge: isCliProvider(connector.kind) ? 'Official' : undefined,
  };
}

function formatScopePath(path: string): string {
  const normalized = path.trim().replace(/^\/+|\/+$/g, '').replace(/\/+/g, '/');
  return normalized ? `/${normalized}` : '/';
}

function formatScopeDescription(scope: RepositoryView): string {
  const path = formatScopePath(scope.path);
  const folder = path === '/' ? 'Project root' : path;
  const mode = scope.max_mode === 'rw' ? 'read & write' : 'read only';
  return `${folder} · ${mode}`;
}

const headerActionStyle = {
  height: 28,
  border: 'none',
  background: 'transparent',
  color: 'var(--po-text-muted)',
  display: 'inline-flex',
  alignItems: 'center',
  justifyContent: 'center',
  gap: 6,
  padding: '0 6px',
  fontSize: FONT_META,
  fontWeight: 500,
  fontFamily: 'var(--po-font-sans)',
  cursor: 'pointer',
} as const;

const setupButtonStyle = {
  height: 30,
  borderRadius: 6,
  border: `1px solid ${T.cardBorder}`,
  background: 'transparent',
  display: 'inline-flex',
  alignItems: 'center',
  justifyContent: 'center',
  gap: 6,
  padding: '0 10px',
  fontSize: FONT_META,
  fontWeight: 500,
  fontFamily: T.fontSans,
  cursor: 'pointer',
  whiteSpace: 'nowrap',
} as const;

const addMethodStyle = {
  width: '100%',
  minHeight: 50,
  border: 'none',
  borderTop: `1px dashed ${T.cardBorder}`,
  background: 'transparent',
  color: T.text2,
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  gap: 8,
  fontSize: FONT_BODY,
  fontWeight: 500,
  cursor: 'pointer',
  fontFamily: T.fontSans,
} as const;
