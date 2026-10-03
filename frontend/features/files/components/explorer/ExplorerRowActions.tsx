'use client';

import { ItemContextMenu } from '@/features/files/components/explorer/ExplorerRowMenus';
import type { ExplorerCreateMenuAction, ExplorerSidebarProps, EntrypointBadge } from '@/features/files/components/explorer/types';
import type { MouseEvent, ReactNode } from 'react';
import { useState } from 'react';

export const EXPLORER_ROW_ACTION_LAYER_WIDTH = 50;
const EXPLORER_ROW_MENU_ACTION_WIDTH = 26;
const EXPLORER_ROW_ACCESS_ACTION_WIDTH = 26;

export function getExplorerRowActionLayerWidth(hasAccessPoint: boolean): number {
  return hasAccessPoint
    ? EXPLORER_ROW_ACTION_LAYER_WIDTH + EXPLORER_ROW_ACCESS_ACTION_WIDTH
    : EXPLORER_ROW_ACTION_LAYER_WIDTH;
}

export function getExplorerRowReservedActionWidth(
  hasAccessPoint: boolean,
  hasObjectMenu: boolean,
): number {
  return (hasObjectMenu ? EXPLORER_ROW_MENU_ACTION_WIDTH : 0)
    + (hasAccessPoint ? EXPLORER_ROW_ACCESS_ACTION_WIDTH : 0);
}

type RowActionButtonVariant = 'default' | 'createActive' | 'accessConfigured';

function rowActionButtonClass(variant: RowActionButtonVariant) {
  const base =
    'flex h-[22px] w-[22px] items-center justify-center rounded border border-transparent p-0 transition-colors';

  if (variant === 'createActive') {
    return `${base} bg-[var(--po-selected)] text-[var(--po-text)]`;
  }

  if (variant === 'accessConfigured') {
    return `${base} bg-transparent text-[var(--po-access-active-text)] hover:bg-[var(--po-access-active-hover)] hover:text-[var(--po-access-active-text)]`;
  }

  return `${base} bg-transparent text-[var(--po-text-subtle)] hover:bg-[var(--po-hover)] hover:text-[var(--po-text)]`;
}

function PlusIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
      <line x1="12" y1="5" x2="12" y2="19" />
      <line x1="5" y1="12" x2="19" y2="12" />
    </svg>
  );
}

function AccessChainIcon({ size = 13 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.45"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M9 17H7A5 5 0 0 1 7 7h2" />
      <path d="M15 7h2a5 5 0 1 1 0 10h-2" />
      <line x1="8" y1="12" x2="16" y2="12" />
    </svg>
  );
}

function RowActionButton({
  title,
  ariaLabel,
  active,
  variant,
  onClick,
  children,
}: {
  title: string;
  ariaLabel: string;
  active?: boolean;
  variant: RowActionButtonVariant;
  onClick: (event: MouseEvent<HTMLButtonElement>) => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      title={title}
      aria-label={ariaLabel}
      aria-pressed={active || undefined}
      onPointerDown={(event) => {
        event.preventDefault();
        event.stopPropagation();
      }}
      onClick={(event) => {
        event.preventDefault();
        event.stopPropagation();
        onClick(event);
      }}
      onContextMenu={(event) => {
        event.preventDefault();
        event.stopPropagation();
      }}
      className={rowActionButtonClass(variant)}
    >
      {children}
    </button>
  );
}

export function ExplorerRowActions({
  nodeId,
  createParentId,
  accessPath,
  isFolder,
  endpoints,
  openMenuAction,
  alwaysVisible = false,
  isSynced,
  itemName,
  onCreate,
  onCreateSync,
  onOpenAccess,
  onRename,
  onDelete,
  onDownload,
}: {
  nodeId: string;
  createParentId: string | null;
  accessPath: string;
  isFolder: boolean;
  endpoints: readonly EntrypointBadge[];
  openMenuAction?: ExplorerCreateMenuAction | null;
  alwaysVisible?: boolean;
  isSynced?: boolean;
  itemName: string;
  onCreate?: ExplorerSidebarProps['onCreate'];
  onCreateSync?: ExplorerSidebarProps['onCreateSync'];
  onOpenAccess?: ExplorerSidebarProps['onOpenAccess'];
  onRename?: ExplorerSidebarProps['onRename'];
  onDelete?: ExplorerSidebarProps['onDelete'];
  onDownload?: ExplorerSidebarProps['onDownload'];
}) {
  const [isContextMenuOpen, setIsContextMenuOpen] = useState(false);

  const isCreateMenuOpen = openMenuAction === 'create';
  const isAccessMenuOpen = openMenuAction === 'access';
  const isAnyMenuOpen =
    isCreateMenuOpen || isContextMenuOpen || isAccessMenuOpen;
  const endpointCount = endpoints.length;
  const hasAccessPoint = isFolder && endpointCount > 0;
  const peerVisibility = alwaysVisible || isAnyMenuOpen ? 'flex' : 'hidden group-hover/row:flex';
  const hasObjectMenu = !!(onRename || onDelete || onDownload || (isFolder && onCreateSync));
  const hasCreateButton = !!(isFolder && onCreate);
  const hasAccessButton = !!(hasAccessPoint && onOpenAccess);

  if (!hasObjectMenu && !hasCreateButton && !hasAccessButton) return null;

  return (
    <div
      className={`workspace-row-actions ${hasAccessButton ? 'flex' : peerVisibility} absolute top-1/2 z-20 -translate-y-1/2 items-center gap-0.5`}
      onClick={(e) => e.stopPropagation()}
      onContextMenu={(e) => e.stopPropagation()}
      style={{ right: 4 }}
    >
      {hasCreateButton && (
        <div className={peerVisibility}>
          <RowActionButton
            title="New item"
            ariaLabel="New item"
            active={isCreateMenuOpen}
            variant={isCreateMenuOpen ? 'createActive' : 'default'}
            onClick={(e) => {
              onCreate(e, createParentId);
            }}
          >
            <PlusIcon />
          </RowActionButton>
        </div>
      )}

      {hasObjectMenu && (
        <div className={peerVisibility}>
          <ItemContextMenu
            itemId={nodeId}
            itemName={itemName}
            isSynced={isSynced}
            exposeLabel={hasAccessPoint ? 'Manage Access' : 'Expose as...'}
            onExpose={
              isFolder && onCreateSync
                ? (event) => {
                    if (hasAccessPoint && onOpenAccess) {
                      onOpenAccess(endpoints, accessPath);
                      return;
                    }
                    onCreateSync(event, accessPath);
                  }
                : undefined
            }
            onRename={onRename}
            onDelete={onDelete}
            onDownload={onDownload}
            onOpenChange={setIsContextMenuOpen}
          />
        </div>
      )}

      {hasAccessButton && (
        <RowActionButton
          title={`${endpointCount} configured Access ${endpointCount === 1 ? 'point' : 'points'}`}
          ariaLabel="Manage Access"
          variant="accessConfigured"
          onClick={() => {
            onOpenAccess?.(endpoints, accessPath);
          }}
        >
          <AccessChainIcon />
        </RowActionButton>
      )}
    </div>
  );
}
