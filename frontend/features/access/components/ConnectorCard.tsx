'use client';

/**
 * ConnectorCard — the selected access point's full detail view.
 *
 * Responsibilities:
 *
 *   - Header: AP name (page-level title, *inline-editable* via hover →
 *     pencil → input), single attribute line, primary action button
 *     (Pause / Resume / Retry), and a real action-menu (Rename, Copy
 *     ID, Disconnect for third-party).
 *   - Body (always visible — no expand/collapse): paused-banner, the
 *     provider-specific Quick-Connect block, the Configuration panel,
 *     and a Recent-activity placeholder.
 *
 * The card is presentational; mutation lives in `useAccessData`. We
 * receive everything needed via props (`onPauseResume`, `onUpdate`,
 * `onDelete`, `pending`) so the card itself stays declarative and
 * testable in isolation.
 */

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { createPortal } from 'react-dom';
import { StatusIndicator } from '@/components/ui/StatusDot';
import { ToggleSwitch } from '@/components/ui/ToggleSwitch';
import type { AccessSurface, RepositoryView } from '@/lib/repoApi';
import { APP_Z_INDEX } from '@/lib/zIndex';
import {
  getAccessProviderCardTitle,
  isBuiltInAccessProvider,
  isCliProvider,
  isMcpProvider,
  normalizeConnectorProvider,
} from '@/lib/accessProviderRegistry';
import { T } from '@/features/access/lib/tokens';
import {
  STATUS_LABEL,
} from '@/features/access/lib/constants';
import {
  getPrimaryAction,
  getTypeLine,
  timeAgo,
} from '@/features/access/lib/format';
import {
  ChevronRightIcon,
  CopyIcon,
  EditIcon,
  MoreVerticalIcon,
  PauseIcon,
  PlayIcon,
  RetryIcon,
} from '@/features/access/components/icons';
import { GhostButton, SubSectionLabel } from '@/features/access/components/ui-blocks';
import { ConnectorAccessPanel } from '@/features/access/components/quick-connect';
import type { ConnectorEditPatch } from '@/features/access/hooks/useAccessData';

// ─── Connector card (one access point, expanded view) ────────────────

export function ConnectorCard({
  connector,
  scope,
  onPauseResume,
  onUpdate,
  onDelete,
  pending,
}: {
  readonly connector: AccessSurface;
  readonly scope: RepositoryView | undefined;
  readonly onPauseResume: () => void;
  readonly onUpdate: (patch: ConnectorEditPatch) => Promise<void>;
  readonly onDelete: () => Promise<void>;
  readonly pending: boolean;
}) {
  const action = getPrimaryAction(connector.status);
  const provider = normalizeConnectorProvider(connector.kind);
  const name = getAccessProviderCardTitle(provider, connector.name);
  const isBuiltin = isBuiltInAccessProvider(provider);

  // Shared name-editing state, controlled from two surfaces:
  //   - Hover-pencil on the header name (direct entry).
  //   - "Rename" item in the action menu (entry from the menu).
  // Both flip this flag; the input then lives inside <NameField>.
  const [editingName, setEditingName] = useState(false);

  return (
    <div
      style={{
        background: T.cardBg,
        border: `1px solid ${T.cardBorder}`,
        borderRadius: 10,
        overflow: 'hidden',
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'flex-start',
          gap: 12,
          padding: '16px 16px 14px',
          boxSizing: 'border-box',
        }}
      >
        <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 6 }}>
          <NameField
            initial={name}
            editing={editingName}
            onStartEdit={() => setEditingName(true)}
            onCancel={() => setEditingName(false)}
            onSubmit={async (newName) => {
              setEditingName(false);
              if (newName !== name) {
                await onUpdate({ name: newName });
              }
            }}
          />
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              fontSize: 12,
              color: T.text3,
              fontFamily: T.fontSans,
              minWidth: 0,
            }}
          >
            <span
              style={{
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
                minWidth: 0,
              }}
            >
              {getTypeLine(connector)}
            </span>
            <span style={{ color: T.text4, flexShrink: 0 }}>·</span>
            <StatusIndicator
              status={connector.status}
              label={STATUS_LABEL[connector.status] ?? connector.status}
              style={{ flexShrink: 0 }}
            />
            <span style={{ color: T.text4, flexShrink: 0 }}>·</span>
            <span style={{ color: T.text3, flexShrink: 0 }}>
              {timeAgo(connector.last_activity_at)}
            </span>
          </div>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0 }}>
          <GhostButton
            onClick={onPauseResume}
            disabled={pending}
            icon={
              action.icon === 'pause'
                ? <PauseIcon size={10} />
                : action.icon === 'play'
                  ? <PlayIcon size={10} />
                  : <RetryIcon size={10} />
            }
          >
            {action.label}
          </GhostButton>
          <ConnectorActionMenu
            connector={connector}
            isBuiltin={isBuiltin}
            onRename={() => setEditingName(true)}
            onDelete={onDelete}
          />
        </div>
      </div>

      <div style={{ height: 1, background: T.cardBorder, margin: '0 16px' }} />

      <ConnectorDetailBody
        connector={connector}
        scope={scope}
        onPauseResume={onPauseResume}
        onUpdate={onUpdate}
        pending={pending}
      />
    </div>
  );
}

export function ConnectorDetailBody({
  connector,
  scope,
  onPauseResume,
  onUpdate,
  pending,
  variant = 'full',
}: {
  readonly connector: AccessSurface;
  readonly scope: RepositoryView | undefined;
  readonly onPauseResume: () => void;
  readonly onUpdate: (patch: ConnectorEditPatch) => Promise<void>;
  readonly pending: boolean;
  readonly variant?: 'full' | 'inline';
}) {
  const isBuiltin = isBuiltInAccessProvider(connector.kind);
  const inline = variant === 'inline';
  const provider = normalizeConnectorProvider(connector.kind);

  return (
    <div style={{ padding: inline ? '12px 16px 14px' : '16px' }}>
      {!inline && connector.status === 'paused' && (
        <PausedBanner
          provider={connector.kind}
          onResume={onPauseResume}
          pending={pending}
        />
      )}
      {inline ? (
        <ConnectorConfigPanel
          connector={connector}
          scope={scope}
          isBuiltin={isBuiltin}
          onUpdate={onUpdate}
          pending={pending}
          showLabel={false}
          variant='inline'
        />
      ) : (
        <>
          <ConnectorAccessPanel
            connector={connector}
            scope={scope}
          />
          <ConnectorConfigPanel
            connector={connector}
            scope={scope}
            isBuiltin={isBuiltin}
            onUpdate={onUpdate}
            pending={pending}
          />
          <ConnectorActivityPanel />
        </>
      )}
    </div>
  );
}

// ─── Inline-editable name (header) ───────────────────────────────────
//
// Two visual states:
//   - Read mode: name span; on hover the pencil icon fades in to hint
//     edit affordance. Click anywhere on the row → enter edit mode.
//   - Edit mode: <input> autofocus + select-all; Enter or blur commits;
//     Escape cancels. While the parent's onSubmit is in flight we
//     keep the input mounted (disabled + dim) so users see why the UI
//     hasn't updated yet — never flash back to read mode mid-flight.
//
// Errors revert the draft to `initial` and surface a tiny red helper
// below the input. The parent decides what counts as success/error
// (typically: SWR revalidation either updates `initial` or throws).
function NameField({
  initial,
  editing,
  onStartEdit,
  onCancel,
  onSubmit,
}: {
  readonly initial: string;
  readonly editing: boolean;
  readonly onStartEdit: () => void;
  readonly onCancel: () => void;
  readonly onSubmit: (newName: string) => Promise<void>;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [draft, setDraft] = useState(initial);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hovered, setHovered] = useState(false);
  // We need to suppress the implicit blur-submit when the user
  // explicitly cancels via Escape — otherwise the blur fires after the
  // ESC keydown clears `editing`, sending an empty/old draft to the API.
  const cancelledRef = useRef(false);

  useEffect(() => {
    if (!editing) {
      setDraft(initial);
      setError(null);
      cancelledRef.current = false;
    }
  }, [initial, editing]);

  useEffect(() => {
    if (editing && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [editing]);

  const submit = useCallback(async () => {
    if (cancelledRef.current) return;
    const trimmed = draft.trim();
    if (!trimmed) {
      setError('Name cannot be empty');
      return;
    }
    if (trimmed === initial) {
      onCancel();
      return;
    }
    setPending(true);
    setError(null);
    try {
      await onSubmit(trimmed);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to rename');
      setDraft(initial);
    } finally {
      setPending(false);
    }
  }, [draft, initial, onCancel, onSubmit]);

  if (!editing) {
    return (
      <button
        type='button'
        onClick={onStartEdit}
        onMouseEnter={() => setHovered(true)}
        onMouseLeave={() => setHovered(false)}
        title='Click to rename'
        style={{
          all: 'unset',
          cursor: 'text',
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          maxWidth: '100%',
          height: 30,
          padding: '0 6px',
          marginLeft: -6,
          borderRadius: 4,
          background: hovered ? 'var(--po-hover)' : 'transparent',
          transition: 'background 0.12s ease',
        }}
      >
        <span
          style={{
            fontSize: 16,
            fontWeight: 600,
            color: T.text1,
            fontFamily: T.fontSans,
            letterSpacing: '-0.01em',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
            minWidth: 0,
          }}
        >
          {initial}
        </span>
        <span
          aria-hidden
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            justifyContent: 'center',
            color: T.text3,
            opacity: hovered ? 1 : 0,
            transition: 'opacity 0.12s ease',
            flexShrink: 0,
          }}
        >
          <EditIcon size={11} />
        </span>
      </button>
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 2, maxWidth: '100%' }}>
      <input
        ref={inputRef}
        value={draft}
        disabled={pending}
        spellCheck={false}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => { void submit(); }}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault();
            void submit();
          } else if (e.key === 'Escape') {
            e.preventDefault();
            cancelledRef.current = true;
            setDraft(initial);
            onCancel();
          }
        }}
        style={{
          fontSize: 16,
          fontWeight: 600,
          color: T.text1,
          fontFamily: T.fontSans,
          letterSpacing: '-0.01em',
          background: 'var(--po-canvas)',
          border: `1px solid ${error ? 'var(--po-danger)' : 'var(--po-border-strong)'}`,
          borderRadius: 6,
          padding: '4px 8px',
          marginLeft: -8,
          outline: 'none',
          minWidth: 0,
          opacity: pending ? 0.6 : 1,
        }}
      />
      {error ? (
        <span
          style={{
            fontSize: 10,
            color: 'var(--po-danger)',
            fontFamily: T.fontSans,
            paddingLeft: 1,
          }}
        >
          {error}
        </span>
      ) : (
        <span
          style={{
            fontSize: 10,
            color: T.text4,
            fontFamily: T.fontSans,
            paddingLeft: 1,
          }}
        >
          Press Enter to save · Esc to cancel
        </span>
      )}
    </div>
  );
}

// ─── Action menu (3-dot dropdown) ────────────────────────────────────
//
// Portal-based dropdown identical in spirit to `ItemActionMenu` used
// across the data view, but typed to the connector's action set so the
// component is self-contained and reusable on this page only.
//
// Items:
//   • Rename       (always)        → defers to the parent's editingName flag
//   • Copy ID      (always)        → navigator.clipboard.writeText(connector.id)
//   • ──────────                   (only when both groups present)
//   • Disconnect   (third-party)   → confirm + onDelete()
function ConnectorActionMenu({
  connector,
  isBuiltin,
  onRename,
  onDelete,
}: {
  readonly connector: AccessSurface;
  readonly isBuiltin: boolean;
  readonly onRename: () => void;
  readonly onDelete: () => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);
  const [copied, setCopied] = useState(false);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  const close = useCallback(() => {
    setOpen(false);
    setPos(null);
  }, []);

  const computePosition = useCallback(() => {
    const btn = buttonRef.current;
    if (!btn) return null;
    const r = btn.getBoundingClientRect();
    const menuWidth = 188;
    let left = r.right - menuWidth;
    if (left < 8) left = 8;
    if (left + menuWidth > globalThis.innerWidth - 8) {
      left = globalThis.innerWidth - menuWidth - 8;
    }
    return { top: r.bottom + 6, left };
  }, []);

  const toggle = () => {
    if (open) {
      close();
    } else {
      const p = computePosition();
      if (p) {
        setPos(p);
        setOpen(true);
      }
    }
  };

  useEffect(() => {
    if (!open) return;
    const onClickOutside = (e: MouseEvent) => {
      const target = e.target as Node;
      if (menuRef.current?.contains(target)) return;
      if (buttonRef.current?.contains(target)) return;
      close();
    };
    const onEscape = (e: KeyboardEvent) => {
      if (e.key === 'Escape') close();
    };
    const onScroll = () => close();
    document.addEventListener('mousedown', onClickOutside);
    document.addEventListener('keydown', onEscape);
    globalThis.addEventListener('scroll', onScroll, true);
    return () => {
      document.removeEventListener('mousedown', onClickOutside);
      document.removeEventListener('keydown', onEscape);
      globalThis.removeEventListener('scroll', onScroll, true);
    };
  }, [open, close]);

  const handleCopyId = useCallback(async () => {
    close();
    try {
      await navigator.clipboard.writeText(connector.id);
      setCopied(true);
      globalThis.setTimeout(() => setCopied(false), 1400);
    } catch {
      // Clipboard API failures are environmental (insecure context, etc.) —
      // we don't surface them as errors because the user explicitly chose
      // a "copy" action; either the system honours it or the platform UI
      // surfaces its own message.
    }
  }, [connector.id, close]);

  const handleDisconnect = useCallback(async () => {
    close();
    const ok = globalThis.confirm(
      `Disconnect "${connector.name}"? This removes the access point and is undoable only by re-creating it.`,
    );
    if (!ok) return;
    try {
      await onDelete();
    } catch {
      // Parent already logged it; user-visible failure surface comes
      // from SWR not flipping (the row stays put). A toast system on
      // this page would let us announce it explicitly — out of scope.
    }
  }, [connector.name, close, onDelete]);

  // Tiny visual confirmation for "Copy ID" — sits on the trigger
  // button as a 1.4-second floating pill so the user sees the result
  // even though the menu has already closed by then.
  const copiedToast = copied ? (
    <span
      style={{
        position: 'absolute',
        top: '100%',
        right: 0,
        marginTop: 6,
        fontSize: 10,
        fontWeight: 500,
        color: T.text1,
        fontFamily: T.fontSans,
        background: 'var(--po-overlay)',
        border: `1px solid ${T.cardBorder}`,
        borderRadius: 6,
        padding: '4px 8px',
        boxShadow: '0 6px 20px var(--po-shadow)',
        whiteSpace: 'nowrap',
        pointerEvents: 'none',
        zIndex: 50,
      }}
    >
      Copied connector ID
    </span>
  ) : null;

  return (
    <span style={{ position: 'relative', display: 'inline-flex' }}>
      <button
        ref={buttonRef}
        type='button'
        onClick={toggle}
        aria-label='More actions'
        aria-haspopup='menu'
        aria-expanded={open}
        style={{
          all: 'unset',
          cursor: 'pointer',
          width: 30,
          height: 30,
          display: 'inline-flex',
          alignItems: 'center',
          justifyContent: 'center',
          borderRadius: 6,
          color: open ? T.text1 : T.text3,
          background: open ? 'var(--po-border-subtle)' : 'transparent',
          transition: 'background 0.12s ease, color 0.12s ease',
        }}
        onMouseEnter={(e) => {
          if (!open) {
            (e.currentTarget as HTMLButtonElement).style.background = 'var(--po-hover)';
            (e.currentTarget as HTMLButtonElement).style.color = T.text2;
          }
        }}
        onMouseLeave={(e) => {
          if (!open) {
            (e.currentTarget as HTMLButtonElement).style.background = 'transparent';
            (e.currentTarget as HTMLButtonElement).style.color = T.text3;
          }
        }}
      >
        <MoreVerticalIcon size={12} />
      </button>
      {copiedToast}
      {open && pos && typeof document !== 'undefined'
        ? createPortal(
            <div
              ref={menuRef}
              role='menu'
              style={{
                position: 'fixed',
                top: pos.top,
                left: pos.left,
                minWidth: 188,
                background: 'var(--po-overlay)',
                border: `1px solid ${T.cardBorder}`,
                borderRadius: 10,
                padding: 4,
                boxShadow: '0 12px 28px var(--po-shadow), 0 2px 4px var(--po-shadow)',
                zIndex: APP_Z_INDEX.popover,
                display: 'flex',
                flexDirection: 'column',
                gap: 1,
                fontFamily: T.fontSans,
              }}
            >
              <MenuItem
                icon={<EditIcon size={12} />}
                label='Rename'
                onClick={() => {
                  close();
                  onRename();
                }}
              />
              <MenuItem
                icon={<CopyIcon size={12} />}
                label='Copy connector ID'
                onClick={handleCopyId}
              />
              {!isBuiltin && (
                <>
                  <div
                    aria-hidden
                    style={{ height: 1, background: T.cardBorder, margin: '4px 6px' }}
                  />
                  <MenuItem
                    icon={<TrashIcon size={12} />}
                    label='Disconnect'
                    danger
                    onClick={handleDisconnect}
                  />
                </>
              )}
            </div>,
            document.body,
          )
        : null}
    </span>
  );
}

function MenuItem({
  icon,
  label,
  onClick,
  danger,
}: {
  readonly icon: ReactNode;
  readonly label: string;
  readonly onClick: () => void;
  readonly danger?: boolean;
}) {
  const [hovered, setHovered] = useState(false);
  return (
    <button
      type='button'
      role='menuitem'
      onClick={onClick}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        all: 'unset',
        cursor: 'pointer',
        display: 'flex',
        alignItems: 'center',
        gap: 10,
        height: 30,
        padding: '0 9px',
        borderRadius: 6,
        fontSize: 12,
        color: danger ? 'var(--po-danger)' : T.text1,
        background: hovered
          ? danger
            ? 'color-mix(in srgb, var(--po-danger) 10%, transparent)'
            : 'var(--po-border-subtle)'
          : 'transparent',
        transition: 'background 0.1s ease',
      }}
    >
      <span
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          justifyContent: 'center',
          color: danger ? 'var(--po-danger)' : T.text3,
        }}
      >
        {icon}
      </span>
      <span>{label}</span>
    </button>
  );
}

const TrashIcon = ({ size = 12 }: { readonly size?: number }) => (
  <svg width={size} height={size} viewBox='0 0 24 24' fill='none' stroke='currentColor' strokeWidth='2' strokeLinecap='round' strokeLinejoin='round'>
    <polyline points='3 6 5 6 21 6' />
    <path d='M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6' />
    <path d='M10 11v6' />
    <path d='M14 11v6' />
    <path d='M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2' />
  </svg>
);

// ─── Paused-state banner ─────────────────────────────────────────────

function PausedBanner({
  provider,
  onResume,
  pending,
}: {
  readonly provider: string;
  readonly onResume: () => void;
  readonly pending: boolean;
}) {
  const label = getAccessProviderCardTitle(provider);
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 12,
        padding: '10px 12px',
        marginBottom: 14,
        background: 'color-mix(in srgb, var(--po-warning) 8%, transparent)',
        border: '1px solid color-mix(in srgb, var(--po-warning) 25%, transparent)',
        borderRadius: 8,
        fontFamily: T.fontSans,
      }}
    >
      <PauseIcon size={11} />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 12, fontWeight: 500, color: T.text1, lineHeight: 1.4 }}>
          {label} is disabled
        </div>
        <div style={{ fontSize: 12, color: T.text2, lineHeight: 1.5, marginTop: 2 }}>
          New requests through this channel are rejected. Click Resume to re-enable.
        </div>
      </div>
      <GhostButton
        onClick={onResume}
        disabled={pending}
        icon={<PlayIcon size={10} />}
      >
        Resume
      </GhostButton>
    </div>
  );
}

// ─── CLI command permissions ────────────────────────────────────────
//
// Keep this deliberately plain: the user is choosing which concrete
// `puppyone fs` commands this connector may expose. Scope mode remains
// the upper bound; a read-only scope locks mutating commands off here.

type PermissionCommandKind = 'read' | 'write';
type PermissionCommandSpec = {
  readonly key: string;
  readonly kind: PermissionCommandKind;
  readonly defaultAllowed: boolean;
};
type CliCommandSpec = PermissionCommandSpec;
type McpToolSpec = PermissionCommandSpec;

const CLI_PERMISSION_CONFIG_KEY = 'command_permissions';
const CLI_COMMAND_SPECS: readonly CliCommandSpec[] = [
  { key: 'ls', kind: 'read', defaultAllowed: true },
  { key: 'tree', kind: 'read', defaultAllowed: true },
  { key: 'find', kind: 'read', defaultAllowed: true },
  { key: 'grep', kind: 'read', defaultAllowed: true },
  { key: 'stat', kind: 'read', defaultAllowed: true },
  { key: 'cat', kind: 'read', defaultAllowed: true },
  { key: 'head', kind: 'read', defaultAllowed: true },
  { key: 'tail', kind: 'read', defaultAllowed: true },
  { key: 'download', kind: 'read', defaultAllowed: true },
  { key: 'write', kind: 'write', defaultAllowed: true },
  { key: 'mkdir', kind: 'write', defaultAllowed: true },
  { key: 'touch', kind: 'write', defaultAllowed: true },
  { key: 'upload', kind: 'write', defaultAllowed: true },
  { key: 'cp', kind: 'write', defaultAllowed: true },
  { key: 'mv', kind: 'write', defaultAllowed: true },
  { key: 'rm', kind: 'write', defaultAllowed: false },
  { key: 'rmdir', kind: 'write', defaultAllowed: false },
];

const CLI_COMMAND_ORDER = new Map(CLI_COMMAND_SPECS.map((command, index) => [command.key, index]));
const CLI_VALID_COMMANDS = new Set(CLI_COMMAND_SPECS.map((command) => command.key));
const CLI_DEFAULT_ALLOWED = CLI_COMMAND_SPECS
  .filter((command) => command.defaultAllowed)
  .map((command) => command.key);
const MCP_TOOLS_CONFIG_KEY = 'tools_config';
const MCP_TOOL_SPECS: readonly McpToolSpec[] = [
  { key: 'fs_semantics', kind: 'read', defaultAllowed: true },
  { key: 'fs_ls', kind: 'read', defaultAllowed: true },
  { key: 'fs_tree', kind: 'read', defaultAllowed: true },
  { key: 'fs_find', kind: 'read', defaultAllowed: true },
  { key: 'fs_grep', kind: 'read', defaultAllowed: true },
  { key: 'fs_cat', kind: 'read', defaultAllowed: true },
  { key: 'fs_head', kind: 'read', defaultAllowed: true },
  { key: 'fs_tail', kind: 'read', defaultAllowed: true },
  { key: 'fs_stat', kind: 'read', defaultAllowed: true },
  { key: 'fs_write', kind: 'write', defaultAllowed: true },
  { key: 'fs_mkdir', kind: 'write', defaultAllowed: true },
  { key: 'fs_touch', kind: 'write', defaultAllowed: true },
  { key: 'fs_cp', kind: 'write', defaultAllowed: true },
  { key: 'fs_mv', kind: 'write', defaultAllowed: true },
  { key: 'fs_rmdir', kind: 'write', defaultAllowed: false },
  { key: 'fs_rm', kind: 'write', defaultAllowed: false },
];
const MCP_TOOL_ORDER = new Map(MCP_TOOL_SPECS.map((tool, index) => [tool.key, index]));
const MCP_VALID_TOOLS = new Set(MCP_TOOL_SPECS.map((tool) => tool.key));
const MCP_DEFAULT_ALLOWED = MCP_TOOL_SPECS
  .filter((tool) => tool.defaultAllowed)
  .map((tool) => tool.key);
const PERMISSION_CHECK_COLOR = 'var(--po-accent)';
const PERMISSION_CHECK_MARK_COLOR = 'var(--po-text-inverse)';

function CliCommandPermissionsRow({
  connector,
  scope,
  onUpdate,
  pending,
  variant = 'default',
  isFirst,
}: {
  readonly connector: AccessSurface;
  readonly scope: RepositoryView | undefined;
  readonly onUpdate: (patch: ConnectorEditPatch) => Promise<void>;
  readonly pending: boolean;
  readonly variant?: ConfigPanelVariant;
  readonly isFirst?: boolean;
}) {
  const allowedCommands = useMemo(
    () => parseCliCommandPermissions(connector.config),
    [connector.config],
  );
  const scopeReadOnly = scope?.max_mode === 'r';
  const readCommands = CLI_COMMAND_SPECS.filter((command) => command.kind === 'read');
  const modifyCommands = CLI_COMMAND_SPECS.filter((command) => command.kind === 'write' && command.defaultAllowed);
  const deleteCommands = CLI_COMMAND_SPECS.filter((command) => command.kind === 'write' && !command.defaultAllowed);

  const writeAllowedCommands = useCallback(
    async (next: Set<string>) => {
      await onUpdate({
        config: {
          ...connector.config,
          [CLI_PERMISSION_CONFIG_KEY]: {
            allowed: Array.from(next).sort(sortCliCommands),
          },
        },
      });
    },
    [connector.config, onUpdate],
  );

  const setCommandAllowed = useCallback(
    async (command: CliCommandSpec, checked: boolean) => {
      if (scopeReadOnly && command.kind === 'write') return;
      const next = new Set(allowedCommands);
      if (checked) {
        next.add(command.key);
      } else {
        next.delete(command.key);
      }
      await writeAllowedCommands(next);
    },
    [allowedCommands, scopeReadOnly, writeAllowedCommands],
  );

  const setCommandsAllowed = useCallback(
    async (commands: readonly CliCommandSpec[], checked: boolean) => {
      if (scopeReadOnly && commands.some((command) => command.kind === 'write')) return;
      const next = new Set(allowedCommands);
      if (checked) {
        commands.forEach((command) => next.add(command.key));
      } else {
        commands.forEach((command) => next.delete(command.key));
      }
      await writeAllowedCommands(next);
    },
    [allowedCommands, scopeReadOnly, writeAllowedCommands],
  );

  const readEnabled = readCommands.every((command) => allowedCommands.has(command.key));
  const modifyEnabled = !scopeReadOnly && modifyCommands.every((command) => allowedCommands.has(command.key));
  const deleteEnabled = !scopeReadOnly && deleteCommands.every((command) => allowedCommands.has(command.key));
  const readAllowedCount = readCommands.filter((command) => allowedCommands.has(command.key)).length;
  const modifyAllowedCount = scopeReadOnly
    ? 0
    : modifyCommands.filter((command) => allowedCommands.has(command.key)).length;
  const deleteAllowedCount = scopeReadOnly
    ? 0
    : deleteCommands.filter((command) => allowedCommands.has(command.key)).length;

  return (
    <div
      style={{
        minWidth: 0,
        padding: variant === 'inline' ? '14px 14px 16px' : '14px 12px 16px',
        borderTop: isFirst ? 'none' : `1px solid ${T.cardBorder}`,
      }}
    >
      <div
        style={{
          minWidth: 0,
          display: 'flex',
          flexDirection: 'column',
          gap: 10,
        }}
      >
        <div
          style={{
            fontFamily: T.fontSans,
            fontSize: 14,
            lineHeight: '18px',
            color: T.text2,
            fontWeight: 600,
          }}
        >
          Permissions
        </div>
        <div
          style={{
            minWidth: 0,
            display: 'flex',
            flexDirection: 'column',
            borderRadius: 7,
            border: `1px solid ${T.cardBorder}`,
            background: 'color-mix(in srgb, var(--po-control) 42%, transparent)',
            overflow: 'hidden',
          }}
        >
          <CommandPermissionGroup
            title='Read files'
            commands={readCommands}
            allowedCommands={allowedCommands}
            allowedCount={readAllowedCount}
            groupEnabled={readEnabled}
            disabled={pending}
            onToggleAll={(checked) => setCommandsAllowed(readCommands, checked)}
            onToggleCommand={setCommandAllowed}
            isFirst
          />
          <CommandPermissionGroup
            title='Modify files'
            commands={modifyCommands}
            allowedCommands={allowedCommands}
            allowedCount={modifyAllowedCount}
            groupEnabled={modifyEnabled}
            disabled={pending || scopeReadOnly}
            muted={scopeReadOnly}
            onToggleAll={(checked) => setCommandsAllowed(modifyCommands, checked)}
            onToggleCommand={setCommandAllowed}
          />
          <CommandPermissionGroup
            title='Delete files'
            commands={deleteCommands}
            allowedCommands={allowedCommands}
            allowedCount={deleteAllowedCount}
            groupEnabled={deleteEnabled}
            disabled={pending || scopeReadOnly}
            muted={scopeReadOnly}
            danger
            onToggleAll={(checked) => setCommandsAllowed(deleteCommands, checked)}
            onToggleCommand={setCommandAllowed}
          />
        </div>
      </div>
    </div>
  );
}

function McpToolPermissionsRow({
  connector,
  scope,
  onUpdate,
  pending,
  variant = 'default',
  isFirst,
}: {
  readonly connector: AccessSurface;
  readonly scope: RepositoryView | undefined;
  readonly onUpdate: (patch: ConnectorEditPatch) => Promise<void>;
  readonly pending: boolean;
  readonly variant?: ConfigPanelVariant;
  readonly isFirst?: boolean;
}) {
  const allowedTools = useMemo(
    () => parseMcpToolPermissions(connector.config),
    [connector.config],
  );
  const writable = getMcpWritable(connector, scope);
  const readTools = MCP_TOOL_SPECS.filter((tool) => tool.kind === 'read');
  const writeTools = MCP_TOOL_SPECS.filter((tool) => tool.kind === 'write' && tool.defaultAllowed);
  const deleteTools = MCP_TOOL_SPECS.filter((tool) => tool.kind === 'write' && !tool.defaultAllowed);

  const writeAllowedTools = useCallback(
    async (next: Set<string>) => {
      await onUpdate({
        config: {
          ...connector.config,
          [MCP_TOOLS_CONFIG_KEY]: buildMcpToolsConfig(connector.config[MCP_TOOLS_CONFIG_KEY], next),
        },
      });
    },
    [connector.config, onUpdate],
  );

  const setToolAllowed = useCallback(
    async (tool: McpToolSpec, checked: boolean) => {
      if (!writable && tool.kind === 'write') return;
      const next = new Set(allowedTools);
      if (checked) {
        next.add(tool.key);
      } else {
        next.delete(tool.key);
      }
      await writeAllowedTools(next);
    },
    [allowedTools, writable, writeAllowedTools],
  );

  const setToolsAllowed = useCallback(
    async (tools: readonly McpToolSpec[], checked: boolean) => {
      if (!writable && tools.some((tool) => tool.kind === 'write')) return;
      const next = new Set(allowedTools);
      if (checked) {
        tools.forEach((tool) => next.add(tool.key));
      } else {
        tools.forEach((tool) => next.delete(tool.key));
      }
      await writeAllowedTools(next);
    },
    [allowedTools, writable, writeAllowedTools],
  );

  const readEnabled = readTools.every((tool) => allowedTools.has(tool.key));
  const writeEnabled = writable && writeTools.every((tool) => allowedTools.has(tool.key));
  const deleteEnabled = writable && deleteTools.every((tool) => allowedTools.has(tool.key));
  const readAllowedCount = readTools.filter((tool) => allowedTools.has(tool.key)).length;
  const writeAllowedCount = writable
    ? writeTools.filter((tool) => allowedTools.has(tool.key)).length
    : 0;
  const deleteAllowedCount = writable
    ? deleteTools.filter((tool) => allowedTools.has(tool.key)).length
    : 0;

  return (
    <div
      style={{
        minWidth: 0,
        padding: variant === 'inline' ? '14px 14px 16px' : '14px 12px 16px',
        borderTop: isFirst ? 'none' : `1px solid ${T.cardBorder}`,
      }}
    >
      <div
        style={{
          minWidth: 0,
          display: 'flex',
          flexDirection: 'column',
          gap: 10,
        }}
      >
        <div
          style={{
            fontFamily: T.fontSans,
            fontSize: 14,
            lineHeight: '18px',
            color: T.text2,
            fontWeight: 600,
          }}
        >
          MCP tools
        </div>
        <div
          style={{
            minWidth: 0,
            display: 'flex',
            flexDirection: 'column',
            borderRadius: 7,
            border: `1px solid ${T.cardBorder}`,
            background: 'color-mix(in srgb, var(--po-control) 42%, transparent)',
            overflow: 'hidden',
          }}
        >
          <CommandPermissionGroup
            title='Read tools'
            commands={readTools}
            allowedCommands={allowedTools}
            allowedCount={readAllowedCount}
            groupEnabled={readEnabled}
            disabled={pending}
            onToggleAll={(checked) => setToolsAllowed(readTools, checked)}
            onToggleCommand={setToolAllowed}
            isFirst
          />
          <CommandPermissionGroup
            title='Write tools'
            commands={writeTools}
            allowedCommands={allowedTools}
            allowedCount={writeAllowedCount}
            groupEnabled={writeEnabled}
            disabled={pending || !writable}
            muted={!writable}
            onToggleAll={(checked) => setToolsAllowed(writeTools, checked)}
            onToggleCommand={setToolAllowed}
          />
          <CommandPermissionGroup
            title='Delete tools'
            commands={deleteTools}
            allowedCommands={allowedTools}
            allowedCount={deleteAllowedCount}
            groupEnabled={deleteEnabled}
            disabled={pending || !writable}
            muted={!writable}
            danger
            onToggleAll={(checked) => setToolsAllowed(deleteTools, checked)}
            onToggleCommand={setToolAllowed}
          />
        </div>
        <div
          style={{
            color: T.text3,
            fontFamily: T.fontSans,
            fontSize: 11,
            lineHeight: '16px',
          }}
        >
          The server applies this policy to both tools/list and tools/call. Client JSON only contains the URL and key.
        </div>
      </div>
    </div>
  );
}

function CommandPermissionGroup({
  title,
  commands,
  allowedCommands,
  allowedCount,
  groupEnabled,
  disabled,
  muted = false,
  danger = false,
  onToggleAll,
  onToggleCommand,
  isFirst,
}: {
  readonly title: string;
  readonly commands: readonly PermissionCommandSpec[];
  readonly allowedCommands: ReadonlySet<string>;
  readonly allowedCount: number;
  readonly groupEnabled: boolean;
  readonly disabled: boolean;
  readonly muted?: boolean;
  readonly danger?: boolean;
  readonly onToggleAll: (checked: boolean) => Promise<void>;
  readonly onToggleCommand: (command: PermissionCommandSpec, checked: boolean) => Promise<void>;
  readonly isFirst?: boolean;
}) {
  const commandCount = commands.length;
  const anyEnabled = allowedCount > 0;
  const statusLabel = groupEnabled
    ? 'Allowed'
    : allowedCount > 0
      ? `${allowedCount}/${commandCount} allowed`
      : 'Off';
  const metaLabel = muted ? 'Blocked by scope' : `${statusLabel} · ${commandCount} commands`;
  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        gap: 8,
        minWidth: 0,
        padding: '10px 12px',
        borderTop: isFirst ? 'none' : `1px solid ${T.cardBorder}`,
        opacity: muted ? 0.62 : 1,
      }}
    >
      <div
        style={{
          minWidth: 0,
          display: 'grid',
          gridTemplateColumns: 'minmax(0, 1fr) auto',
          gap: 12,
          alignItems: 'center',
        }}
      >
        <div
          style={{
            minWidth: 0,
            display: 'flex',
            alignItems: 'baseline',
            gap: 8,
          }}
        >
          <span
            style={{
              fontFamily: T.fontSans,
              fontSize: 12,
              lineHeight: CONFIG_LINE_HEIGHT,
              fontWeight: 500,
              color: T.text1,
              whiteSpace: 'nowrap',
            }}
          >
            {title}
          </span>
          <span
            style={{
              minWidth: 0,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              fontFamily: T.fontSans,
              fontSize: 12,
              lineHeight: '16px',
              fontWeight: 400,
              color: T.text2,
              whiteSpace: 'nowrap',
            }}
          >
            {metaLabel}
          </span>
        </div>
        <PermissionGroupToggle
          checked={groupEnabled && !muted}
          partial={anyEnabled && !groupEnabled && !muted}
          disabled={disabled}
          onToggle={() => { void onToggleAll(!anyEnabled); }}
        />
      </div>
      <div
        style={{
          minWidth: 0,
          display: 'flex',
          flexWrap: 'wrap',
          alignItems: 'center',
          gap: 6,
        }}
      >
        {commands.map((command) => (
          <CommandPermissionPill
            key={command.key}
            command={command}
            enabled={allowedCommands.has(command.key) && !muted}
            disabled={disabled}
            danger={danger}
            onToggle={(checked) => onToggleCommand(command, checked)}
          />
        ))}
      </div>
    </div>
  );
}

function PermissionGroupToggle({
  checked,
  partial,
  disabled,
  onToggle,
}: {
  readonly checked: boolean;
  readonly partial: boolean;
  readonly disabled: boolean;
  readonly onToggle: () => void;
}) {
  const [hovered, setHovered] = useState(false);
  return (
    <button
      type='button'
      disabled={disabled}
      aria-pressed={checked}
      onClick={onToggle}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        height: 24,
        display: 'inline-flex',
        alignItems: 'center',
        gap: 6,
        padding: '0 6px',
        border: 'none',
        borderRadius: 5,
        background: hovered && !disabled ? 'var(--po-hover)' : 'transparent',
        color: disabled ? T.text3 : T.text2,
        cursor: disabled ? 'not-allowed' : 'pointer',
        opacity: disabled ? 0.54 : 1,
        fontFamily: T.fontSans,
        fontSize: 12,
        lineHeight: '16px',
        fontWeight: 400,
        transition: `background 0.12s ${T.ease}, opacity 0.12s ${T.ease}`,
      }}
    >
      <span
        aria-hidden
        style={{
          width: 13,
          height: 13,
          borderRadius: 3,
          border: `1px solid ${checked || partial ? PERMISSION_CHECK_COLOR : T.border}`,
          background: checked || partial ? PERMISSION_CHECK_COLOR : 'transparent',
          color: PERMISSION_CHECK_MARK_COLOR,
          display: 'inline-flex',
          alignItems: 'center',
          justifyContent: 'center',
          flexShrink: 0,
        }}
      >
        {checked ? <CheckGlyph size={9} /> : partial ? <MinusGlyph size={9} /> : null}
      </span>
      All
    </button>
  );
}

function CommandPermissionPill({
  command,
  enabled,
  disabled,
  danger = false,
  onToggle,
}: {
  readonly command: PermissionCommandSpec;
  readonly enabled: boolean;
  readonly disabled: boolean;
  readonly danger?: boolean;
  readonly onToggle: (checked: boolean) => Promise<void>;
}) {
  const [hovered, setHovered] = useState(false);
  const activeBorder = danger
    ? 'color-mix(in srgb, var(--po-danger) 34%, var(--po-border-strong))'
    : 'var(--po-border-strong)';
  const activeBackground = danger
    ? 'color-mix(in srgb, var(--po-danger) 7%, transparent)'
    : 'color-mix(in srgb, var(--po-text) 5%, transparent)';
  return (
    <button
      type='button'
      aria-pressed={enabled}
      disabled={disabled}
      title={command.key}
      onClick={() => { void onToggle(!enabled); }}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        height: 26,
        display: 'inline-flex',
        alignItems: 'center',
        gap: 6,
        padding: '0 9px',
        borderRadius: 6,
        border: `1px solid ${
          enabled
            ? activeBorder
            : hovered && !disabled
              ? 'var(--po-border-strong)'
              : T.cardBorder
        }`,
        background: enabled
          ? activeBackground
          : hovered && !disabled
            ? 'var(--po-hover)'
            : 'transparent',
        color: enabled ? T.text1 : T.text3,
        cursor: disabled ? 'not-allowed' : 'pointer',
        opacity: disabled ? 0.52 : 1,
        fontFamily: T.fontSans,
        fontSize: 12,
        lineHeight: '16px',
        fontWeight: 400,
        transition: `background 0.12s ${T.ease}, border-color 0.12s ${T.ease}, color 0.12s ${T.ease}, opacity 0.12s ${T.ease}`,
      }}
    >
      <span
        aria-hidden
        style={{
          width: 12,
          height: 12,
          borderRadius: 3,
          background: enabled ? PERMISSION_CHECK_COLOR : 'transparent',
          border: enabled ? `1px solid ${PERMISSION_CHECK_COLOR}` : `1px solid ${T.border}`,
          color: PERMISSION_CHECK_MARK_COLOR,
          display: 'inline-flex',
          alignItems: 'center',
          justifyContent: 'center',
          flexShrink: 0,
        }}
      >
        {enabled ? <CheckGlyph size={9} /> : null}
      </span>
      {command.key}
    </button>
  );
}

const CheckGlyph = ({ size = 10 }: { readonly size?: number }) => (
  <svg width={size} height={size} viewBox='0 0 24 24' fill='none' stroke='currentColor' strokeWidth='3' strokeLinecap='round' strokeLinejoin='round' aria-hidden>
    <polyline points='20 6 9 17 4 12' />
  </svg>
);

const MinusGlyph = ({ size = 10 }: { readonly size?: number }) => (
  <svg width={size} height={size} viewBox='0 0 24 24' fill='none' stroke='currentColor' strokeWidth='3' strokeLinecap='round' strokeLinejoin='round' aria-hidden>
    <line x1='6' y1='12' x2='18' y2='12' />
  </svg>
);

function parseCliCommandPermissions(config: Record<string, unknown>): ReadonlySet<string> {
  const raw = config[CLI_PERMISSION_CONFIG_KEY];
  if (!isRecord(raw)) {
    return new Set(CLI_DEFAULT_ALLOWED);
  }

  const allowed = readCommandArray(raw.allowed)
    ?? readCommandArray(raw.allowed_commands);
  if (allowed) {
    return new Set(allowed);
  }

  if (isRecord(raw.commands)) {
    const commandMap = raw.commands;
    return new Set(
      CLI_COMMAND_SPECS
        .filter((command) => commandMap[command.key] === true)
        .map((command) => command.key),
    );
  }

  if (isRecord(raw.groups)) {
    return parseLegacyCliGroups(raw.groups);
  }

  return new Set(CLI_DEFAULT_ALLOWED);
}

function parseLegacyCliGroups(groups: Record<string, unknown>): ReadonlySet<string> {
  const allowed = new Set<string>();
  const addByKind = (kind: PermissionCommandKind) => {
    CLI_COMMAND_SPECS
      .filter((command) => command.kind === kind)
      .forEach((command) => allowed.add(command.key));
  };
  if (groups.read !== false) addByKind('read');
  if (groups.write === true) {
    ['write', 'mkdir', 'touch', 'upload'].forEach((command) => allowed.add(command));
  }
  if (groups.move === true) {
    ['cp', 'mv'].forEach((command) => allowed.add(command));
  }
  if (groups.delete === true) {
    ['rm', 'rmdir'].forEach((command) => allowed.add(command));
  }
  return allowed;
}

function readCommandArray(value: unknown): string[] | null {
  if (!Array.isArray(value)) return null;
  return value.filter((item): item is string => typeof item === 'string' && CLI_VALID_COMMANDS.has(item));
}

function sortCliCommands(a: string, b: string): number {
  return (CLI_COMMAND_ORDER.get(a) ?? 999) - (CLI_COMMAND_ORDER.get(b) ?? 999);
}

function parseMcpToolPermissions(config: Record<string, unknown>): ReadonlySet<string> {
  const raw = config[MCP_TOOLS_CONFIG_KEY];
  const legacyList = parseLegacyMcpToolList(raw);
  if (legacyList) {
    return legacyList;
  }
  if (!isRecord(raw)) {
    return new Set(MCP_DEFAULT_ALLOWED);
  }

  const fsConfig = isRecord(raw.filesystem)
    ? raw.filesystem
    : isRecord(raw.fs)
      ? raw.fs
      : raw;
  const allowed = readMcpToolArray(fsConfig.allowed)
    ?? readMcpToolArray(fsConfig.allowed_tools)
    ?? readMcpToolArray(fsConfig.tools_allowed);
  if (allowed) {
    return new Set(allowed);
  }

  const toolMap = fsConfig.tools;
  if (isRecord(toolMap)) {
    return new Set(
      MCP_TOOL_SPECS
        .filter((tool) => toolMap[tool.key] === true)
        .map((tool) => tool.key),
    );
  }

  if (isRecord(fsConfig.groups)) {
    return parseMcpGroups(fsConfig.groups);
  }
  if ('read' in fsConfig || 'write' in fsConfig || 'delete' in fsConfig) {
    return parseMcpGroups(fsConfig);
  }

  return new Set(MCP_DEFAULT_ALLOWED);
}

function buildMcpToolsConfig(raw: unknown, allowedTools: ReadonlySet<string>) {
  const customTools = readMcpCustomTools(raw);
  return {
    version: 1,
    filesystem: {
      allowed: Array.from(allowedTools)
        .filter((tool) => MCP_VALID_TOOLS.has(tool))
        .sort(sortMcpTools),
    },
    shell: { enabled: false },
    ...(customTools.length > 0 ? { custom_tools: customTools } : {}),
  };
}

function getMcpWritable(connector: AccessSurface, scope: RepositoryView | undefined): boolean {
  if (!scope || scope.max_mode !== 'rw') return false;
  const accesses = Array.isArray(connector.config?.accesses)
    ? connector.config.accesses as Array<{ readonly?: boolean }>
    : [];
  if (accesses.length === 0) return true;
  return accesses.some((access) => access.readonly === false);
}

function parseLegacyMcpToolList(value: unknown): ReadonlySet<string> | null {
  if (!Array.isArray(value)) return null;
  let found = false;
  const allowed = new Set<string>();
  value.forEach((item) => {
    if (!isRecord(item)) return;
    const name = item.name ?? item.tool_name;
    if (typeof name !== 'string' || !MCP_VALID_TOOLS.has(name)) return;
    found = true;
    if (item.enabled !== false) allowed.add(name);
  });
  return found ? allowed : null;
}

function parseMcpGroups(groups: Record<string, unknown>): ReadonlySet<string> {
  const allowed = new Set<string>();
  const addTools = (tools: readonly McpToolSpec[]) => {
    tools.forEach((tool) => allowed.add(tool.key));
  };
  if (groups.read !== false) {
    addTools(MCP_TOOL_SPECS.filter((tool) => tool.kind === 'read'));
  }
  if (groups.write !== false) {
    addTools(MCP_TOOL_SPECS.filter((tool) => tool.kind === 'write' && tool.defaultAllowed));
  }
  if (groups.delete === true) {
    addTools(MCP_TOOL_SPECS.filter((tool) => tool.kind === 'write' && !tool.defaultAllowed));
  }
  return allowed;
}

function readMcpToolArray(value: unknown): string[] | null {
  if (!Array.isArray(value)) return null;
  return value.filter((item): item is string => typeof item === 'string' && MCP_VALID_TOOLS.has(item));
}

function readMcpCustomTools(value: unknown): Array<{ tool_id: string; enabled?: boolean }> {
  const read = (items: unknown): Array<{ tool_id: string; enabled?: boolean }> => {
    if (!Array.isArray(items)) return [];
    return items
      .filter((item): item is Record<string, unknown> => isRecord(item) && typeof item.tool_id === 'string')
      .map((item) => ({
        tool_id: item.tool_id as string,
        ...(typeof item.enabled === 'boolean' ? { enabled: item.enabled } : {}),
      }));
  };
  if (Array.isArray(value)) {
    return read(value);
  }
  if (!isRecord(value)) {
    return [];
  }
  return read(value.custom_tools).concat(read(value.bound_tools), read(value.external_tools));
}

function sortMcpTools(a: string, b: string): number {
  return (MCP_TOOL_ORDER.get(a) ?? 999) - (MCP_TOOL_ORDER.get(b) ?? 999);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

// ─── Connector details ───────────────────────────────────────────────
//
// This expanded row should feel like management, not database metadata.
// Keep only the facts that help the user: when it was last used, when
// it was created, and any current error.

function ConnectorConfigPanel({
  connector,
  scope,
  onUpdate,
  pending = false,
  showLabel = true,
  variant = 'default',
}: {
  readonly connector: AccessSurface;
  readonly scope?: RepositoryView;
  readonly isBuiltin?: boolean;
  readonly onUpdate?: (patch: ConnectorEditPatch) => Promise<void>;
  readonly pending?: boolean;
  readonly showLabel?: boolean;
  readonly variant?: ConfigPanelVariant;
}) {
  const inline = variant === 'inline';
  const showCliCommands = isCliProvider(connector.kind) && !!onUpdate;
  const showMcpTools = isMcpProvider(connector.kind) && !!onUpdate;
  const showError = !!connector.error_message;

  if (!showCliCommands && !showMcpTools && !showError) {
    return null;
  }

  return (
    <div style={{ marginBottom: inline ? 0 : 14 }}>
      {showLabel && !showCliCommands && !showMcpTools ? <SubSectionLabel>Details</SubSectionLabel> : null}
      <div
        style={{
          background: inline ? 'transparent' : 'var(--po-canvas)',
          border: inline ? 'none' : `1px solid ${T.cardBorder}`,
          borderRadius: inline ? 0 : 6,
          overflow: 'hidden',
        }}
      >
        {showCliCommands ? (
          <CliCommandPermissionsRow
            connector={connector}
            scope={scope}
            onUpdate={onUpdate}
            pending={pending}
            variant={variant}
            isFirst
          />
        ) : null}
        {showMcpTools && onUpdate ? (
          <McpToolPermissionsRow
            connector={connector}
            scope={scope}
            onUpdate={onUpdate}
            pending={pending}
            variant={variant}
            isFirst={!showCliCommands}
          />
        ) : null}
        {connector.error_message ? (
          <ConfigRow
            label='Error'
            value={connector.error_message}
            isFirst={!showCliCommands && !showMcpTools}
            variant={variant}
          />
        ) : null}
      </div>
    </div>
  );
}

type ConfigPanelVariant = 'default' | 'inline';
const CONFIG_LABEL_WIDTH = 112;
const CONFIG_LINE_HEIGHT = '18px';

// Plain row — value is a string (read-only metadata). Keeps the same
// visual signature as before; merely extracted so editable rows can
// share identical chrome and we never accidentally drift the styling
// between read and edit cells.
function ConfigRow({
  label,
  value,
  isFirst,
  mono,
  muted,
  variant = 'default',
}: {
  readonly label: string;
  readonly value: string;
  readonly isFirst?: boolean;
  readonly mono?: boolean;
  readonly muted?: boolean;
  readonly variant?: ConfigPanelVariant;
}) {
  return (
    <RowShell label={label} isFirst={isFirst} variant={variant}>
      <span
        style={{
          flex: 1,
          minWidth: 0,
          fontSize: 12,
          lineHeight: CONFIG_LINE_HEIGHT,
          color: muted ? T.text2 : T.text1,
          fontFamily: mono ? T.fontMono : T.fontSans,
          wordBreak: 'break-word',
          fontStyle: 'normal',
        }}
      >
        {value}
      </span>
    </RowShell>
  );
}

function RowShell({
  label,
  isFirst,
  children,
  variant = 'default',
  align = 'center',
}: {
  readonly label: string;
  readonly isFirst?: boolean;
  readonly children: ReactNode;
  readonly variant?: ConfigPanelVariant;
  readonly align?: 'center' | 'start';
}) {
  const inline = variant === 'inline';
  return (
    <div
      style={{
        display: inline ? 'grid' : 'flex',
        gridTemplateColumns: inline ? `${CONFIG_LABEL_WIDTH}px minmax(0, 1fr)` : undefined,
        alignItems: align === 'start' ? 'flex-start' : 'center',
        gap: inline ? 12 : 14,
        minHeight: inline ? 42 : 38,
        padding: inline ? '0' : '7px 12px',
        borderTop: isFirst ? 'none' : `1px solid ${T.cardBorder}`,
      }}
    >
      <span
        style={{
          width: CONFIG_LABEL_WIDTH,
          flexShrink: 0,
          fontSize: 12,
          lineHeight: CONFIG_LINE_HEIGHT,
          color: T.text2,
          fontFamily: T.fontSans,
          fontWeight: 500,
        }}
      >
        {label}
      </span>
      {children}
    </div>
  );
}

// ─── Activity (placeholder until audit log is AP-scoped) ────────────

function ConnectorActivityPanel() {
  return (
    <div>
      <SubSectionLabel
        right={
          <GhostButton icon={<ChevronRightIcon size={10} />}>View all</GhostButton>
        }
      >
        Recent activity
      </SubSectionLabel>
      <div
        style={{
          padding: '10px 12px',
          fontSize: 12,
          color: T.text3,
          fontFamily: T.fontSans,
          background: 'var(--po-canvas)',
          border: `1px solid ${T.cardBorder}`,
          borderRadius: 6,
          fontStyle: 'italic',
        }}
      >
        No activity tracked for this access point yet.
      </div>
    </div>
  );
}
