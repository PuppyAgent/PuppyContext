'use client';

import { StatusDot } from '@/components/ui/StatusDot';
import type { EntrypointBadge } from '@/features/files/components/explorer/types';
import {
  isMcpProvider,
  isSandboxProvider,
} from '@/lib/accessProviderRegistry';
import { APP_Z_INDEX } from '@/lib/zIndex';
import type { MouseEvent as ReactMouseEvent, ReactNode } from 'react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';

function SyncSourceIcon({ size = 16, isEmpty = false }: { size?: number; isEmpty?: boolean }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={isEmpty ? '1.5' : '2'}
      strokeLinecap="round"
      strokeLinejoin="round"
      style={{ color: isEmpty ? 'color-mix(in srgb, var(--po-text) 22%, transparent)' : 'color-mix(in srgb, var(--po-text) 80%, transparent)' }}
    >
      <path d="M12 22v-5" />
      <path d="M9 8V2" />
      <path d="M15 8V2" />
      <path d="M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8Z" strokeDasharray={isEmpty ? '2 2' : 'none'} />
    </svg>
  );
}

export function EndpointIconRenderer({ ep, size = 14 }: { ep: EntrypointBadge; size?: number }) {
  const isAgent = ep.provider.startsWith('agent:');
  const isMcp = isMcpProvider(ep.provider);
  const isSandbox = isSandboxProvider(ep.provider);
  const color = 'var(--po-text-muted)';
  const status = ep.status === 'error' ? 'failed' : ep.status === 'stopped' ? 'inactive' : 'active';

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
      <StatusDot status={status} />

      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', width: size, height: size }}>
        {isAgent ? (
          <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ color }}>
            <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
          </svg>
        ) : isMcp ? (
          <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ color }}>
            <rect x="2" y="2" width="20" height="8" rx="2" ry="2" />
            <rect x="2" y="14" width="20" height="8" rx="2" ry="2" />
            <line x1="6" y1="6" x2="6.01" y2="6" />
            <line x1="6" y1="18" x2="6.01" y2="18" />
          </svg>
        ) : isSandbox ? (
          <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ color }}>
            <polyline points="4 17 10 11 4 5" />
            <line x1="12" y1="19" x2="20" y2="19" />
          </svg>
        ) : (
          <SyncSourceIcon size={size} />
        )}
      </div>
    </div>
  );
}

export function ItemContextMenu({
  itemId,
  itemName,
  isSynced,
  exposeLabel,
  onExpose,
  onRename,
  onDelete,
  onDownload,
  onOpenChange,
}: {
  itemId: string;
  itemName: string;
  isSynced?: boolean;
  exposeLabel?: string;
  onExpose?: (event: ReactMouseEvent<Element>) => void;
  onRename?: (id: string, name: string) => void;
  onDelete?: (id: string, name: string) => void;
  onDownload?: (id: string, name: string) => void;
  onOpenChange?: (open: boolean) => void;
}) {
  const [open, setOpenRaw] = useState(false);
  const [pos, setPos] = useState<{ x: number; y: number } | null>(null);
  const btnRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  const setOpen = useCallback(
    (nextOpen: boolean) => {
      setOpenRaw(nextOpen);
      onOpenChange?.(nextOpen);
    },
    [onOpenChange],
  );

  const updatePosition = useCallback(() => {
    const triggerEl = btnRef.current;
    if (!triggerEl) return;

    const triggerRect = triggerEl.getBoundingClientRect();
    const hostRect =
      triggerEl.closest('[data-menu-host="true"]')?.getBoundingClientRect() ?? triggerRect;

    {
      const viewport = window.visualViewport;
      const left = viewport?.offsetLeft ?? 0;
      const top = viewport?.offsetTop ?? 0;
      const width = viewport?.width ?? window.innerWidth;
      const height = viewport?.height ?? window.innerHeight;
      const menu = menuRef.current?.getBoundingClientRect();
      setPos({
        x: Math.max(left + 8, Math.min(triggerRect.left, left + width - (menu?.width || 160) - 8)),
        y: Math.max(top + 8, Math.min(hostRect.bottom - 1, top + height - (menu?.height || 240) - 8)),
      });
      return;
    }
  }, []);

  useEffect(() => {
    if (!open) return;

    updatePosition();

    const handlePointerDown = (event: MouseEvent) => {
      const target = event.target as Node;
      if (btnRef.current?.contains(target) || menuRef.current?.contains(target)) return;
      setOpen(false);
    };

    const handleReposition = () => updatePosition();
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      event.stopPropagation();
      setOpen(false);
      btnRef.current?.focus();
    };

    document.addEventListener('mousedown', handlePointerDown);
    document.addEventListener('keydown', handleKeyDown, true);
    window.addEventListener('resize', handleReposition);
    window.addEventListener('scroll', handleReposition, true);

    return () => {
      document.removeEventListener('mousedown', handlePointerDown);
      document.removeEventListener('keydown', handleKeyDown, true);
      window.removeEventListener('resize', handleReposition);
      window.removeEventListener('scroll', handleReposition, true);
    };
  }, [open, setOpen, updatePosition]);

  const handleToggle = (e: ReactMouseEvent<HTMLButtonElement>) => {
    e.preventDefault();
    e.stopPropagation();

    if (open) {
      setOpen(false);
      return;
    }

    updatePosition();
    setOpen(true);
  };

  return (
    <>
      <button
        ref={btnRef}
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        onPointerDown={(e) => {
          e.stopPropagation();
        }}
        onContextMenu={(e) => {
          e.preventDefault();
          e.stopPropagation();
        }}
        onClick={handleToggle}
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          width: 22,
          height: 22,
          borderRadius: 4,
          background: open ? 'var(--po-active)' : 'transparent',
          border: 'none',
          cursor: 'pointer',
          color: open ? 'var(--po-text)' : 'var(--po-text-subtle)',
          padding: 0,
          transition: 'background 0.1s, color 0.1s',
        }}
        onMouseEnter={(e) => {
          e.currentTarget.style.background = 'var(--po-active)';
          e.currentTarget.style.color = 'var(--po-text)';
        }}
        onMouseLeave={(e) => {
          if (!open) {
            e.currentTarget.style.background = 'transparent';
            e.currentTarget.style.color = 'var(--po-text-subtle)';
          }
        }}
      >
        <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
          <circle cx="12" cy="5" r="2" />
          <circle cx="12" cy="12" r="2" />
          <circle cx="12" cy="19" r="2" />
        </svg>
      </button>

      {open && pos && typeof document !== 'undefined' && createPortal(
        <div
          ref={menuRef}
          data-workspace-row-menu=''
          role="menu"
          onPointerDown={(e) => {
            e.stopPropagation();
          }}
          onClick={(e) => {
            e.stopPropagation();
          }}
          style={{
            position: 'fixed',
            top: pos.y,
            left: pos.x,
            zIndex: APP_Z_INDEX.popover,
            background: 'var(--po-overlay)',
            backdropFilter: 'blur(20px)',
            border: '1px solid var(--po-border)',
            borderRadius: 8,
            boxShadow: '0 16px 44px var(--po-shadow)',
            minWidth: 160,
            padding: '4px 0',
            fontSize: 13,
          }}
        >
          {onRename && !isSynced && (
            <RowMenuItem
              label="Rename"
              icon={
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="M12 20h9" />
                  <path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" />
                </svg>
              }
              onClick={() => {
                setOpen(false);
                onRename(itemId, itemName);
              }}
            />
          )}

          {onExpose && (
            <RowMenuItem
              label={exposeLabel ?? 'Expose as...'}
              icon={
                <svg
                  width="14"
                  height="14"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <path d="M9 17H7A5 5 0 0 1 7 7h2" />
                  <path d="M15 7h2a5 5 0 1 1 0 10h-2" />
                  <line x1="8" y1="12" x2="16" y2="12" />
                </svg>
              }
              onClick={(event) => {
                setOpen(false);
                onExpose(event);
              }}
            />
          )}

          {onDownload && (
            <RowMenuItem
              label="Download"
              icon={
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                  <polyline points="7 10 12 15 17 10" />
                  <line x1="12" y1="15" x2="12" y2="3" />
                </svg>
              }
              onClick={() => {
                setOpen(false);
                onDownload(itemId, itemName);
              }}
            />
          )}

          {onDelete && (
            <RowMenuItem
              label="Delete"
              destructive
              icon={
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="M3 6h18" />
                  <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                </svg>
              }
              onClick={() => {
                setOpen(false);
                onDelete(itemId, itemName);
              }}
            />
          )}
        </div>,
        document.body,
      )}
    </>
  );
}

/**
 * Shared menu-item primitive for ExplorerRow popups.
 *
 * Styling intentionally mirrors the `MenuItem` used inside
 * `CreateMenu` — same 32px row height, 13px label, 6px-radius
 * floating hover, 4px lateral inset. The two popups (this row's
 * `Rename / Download / Delete` and the `+` button's create
 * picker) used to drift apart visually because each declared its
 * own ad-hoc styles inline; consolidating them here means any
 * future tweak propagates to both surfaces and the menus stay in
 * the same family.
 *
 * We keep the underlying tag as `<button>` (CreateMenu uses a
 * `<div>`) so the `role="menuitem"` is on a natively-focusable
 * element; the user's mental model is that these are real,
 * keyboard-reachable actions ("Delete this file"), unlike the
 * create picker where the items are mostly mouse-driven submenu
 * hovers.
 */
function RowMenuItem({
  icon,
  label,
  onClick,
  destructive,
}: {
  icon: ReactNode;
  label: string;
  onClick: (e: ReactMouseEvent) => void;
  destructive?: boolean;
}) {
  // Destructive items keep a tinted text + icon color on default,
  // and shift to a red-tinted hover background instead of the
  // neutral white wash so the affordance reads as "this will
  // remove something". Non-destructive items follow the same
  // neutral pattern as CreateMenu's MenuItem exactly.
  const baseColor = destructive ? 'var(--po-danger)' : 'var(--po-text)';
  const hoverBg = destructive ? 'color-mix(in srgb, var(--po-danger) 10%, transparent)' : 'var(--po-hover)';

  return (
    <button
      type="button"
      role="menuitem"
      onClick={(e) => {
        e.stopPropagation();
        onClick(e);
      }}
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        // calc(100% - 8px) + margin '0 4px' reproduces CreateMenu's
        // `<div>` items, which rely on natural 100%-width-of-parent
        // div behavior + 4px lateral margin. <button> doesn't fill
        // its parent by default, so we widen explicitly.
        width: 'calc(100% - 8px)',
        height: 32,
        margin: '0 4px',
        padding: '0 12px',
        background: 'transparent',
        border: 'none',
        borderRadius: 6,
        color: baseColor,
        cursor: 'pointer',
        fontSize: 13,
        textAlign: 'left',
        transition: 'background 0.1s',
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.background = hoverBg;
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.background = 'transparent';
      }}
    >
      <span
        style={{
          display: 'flex',
          width: 14,
          height: 14,
          alignItems: 'center',
          justifyContent: 'center',
          // 0.7 opacity matches CreateMenu's icon treatment so
          // every leading glyph reads at the same visual weight
          // regardless of the underlying SVG's stroke color.
          // Destructive items use 0.85 to keep the red recognizable
          // — at 0.7 it blended into the menu chrome on dark BGs.
          opacity: destructive ? 0.85 : 0.7,
          color: baseColor,
          flexShrink: 0,
        }}
      >
        {icon}
      </span>
      <span style={{ flex: 1, whiteSpace: 'nowrap' }}>{label}</span>
    </button>
  );
}
