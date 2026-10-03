'use client';

import { useState } from 'react';
import type { AccessSurface, RepositoryView } from '@/lib/repoApi';
import { T } from '@/features/access/lib/tokens';
import type { ConnectorEditPatch } from '@/features/access/hooks/useAccessData';
import { ConnectorConnectDialog } from '@/features/access/components/ConnectorConnectDialog';
import { ConnectorExpandedDetail, ConnectorListRow } from '@/features/access/components/ConnectorMethodRow';

// ─── ConnectorList ──────────────────────────────────────────────────
//
// Compact table-style list inspired by the newer Access direction:
// every connector is a row with identity, state, and its primary setup
// action. The selected row expands in-place for heavier configuration
// material, so the page stays scannable until the user asks for detail.

export function ConnectorList({
  scope,
  connectors,
  selectedId,
  onSelect,
  onPauseResume,
  onUpdate,
  pendingConnectorIds,
  canManage,
}: {
  readonly scope: RepositoryView | undefined;
  readonly connectors: readonly AccessSurface[];
  readonly selectedId: string | null;
  readonly onSelect: (id: string) => void;
  readonly onPauseResume: (id: string) => Promise<void> | void;
  readonly onUpdate: (id: string, patch: ConnectorEditPatch) => Promise<void>;
  readonly pendingConnectorIds: ReadonlySet<string>;
  readonly canManage: boolean;
}) {
  const [connectDialogConnector, setConnectDialogConnector] = useState<AccessSurface | null>(null);

  return (
    <>
      <div
        style={{
          marginBottom: 10,
          minWidth: 0,
          display: 'flex',
          flexDirection: 'column',
          gap: 10,
        }}
      >
        {connectors.map((connector) => {
          const inactive = connector.status === 'paused';
          const selected = !inactive && connector.id === selectedId;
          const pending = pendingConnectorIds.has(connector.id);
          return (
            <div
              key={connector.id}
              style={{
                position: 'relative',
                borderRadius: 8,
                border: `1px solid ${selected ? 'var(--po-border-strong)' : T.cardBorder}`,
                background: selected
                  ? 'color-mix(in srgb, var(--po-panel) 72%, var(--po-control) 28%)'
                  : inactive
                    ? 'color-mix(in srgb, var(--po-canvas) 92%, var(--po-panel) 8%)'
                    : T.bg,
                overflow: 'hidden',
                boxShadow: selected ? '0 1px 2px color-mix(in srgb, var(--po-shadow) 16%, transparent)' : 'none',
                transition: `border-color 0.15s ${T.ease}, box-shadow 0.15s ${T.ease}, background 0.15s ${T.ease}`,
              }}
            >
              <ConnectorListRow
                scope={scope}
                connector={connector}
                selected={selected}
                showPromptPreview
                onSelect={() => onSelect(connector.id)}
                onConnect={() => setConnectDialogConnector(connector)}
                onPauseResume={() => onPauseResume(connector.id)}
                pending={pending}
                canManage={canManage}
              />
              {canManage && selected && !inactive ? (
                <ConnectorExpandedDetail
                  connector={connector}
                  scope={scope}
                  pending={pending}
                  onPauseResume={() => onPauseResume(connector.id)}
                  onUpdate={(patch) => onUpdate(connector.id, patch)}
                />
              ) : null}
            </div>
          );
        })}
      </div>
      {connectDialogConnector ? (
        <ConnectorConnectDialog
          connector={connectDialogConnector}
          scope={scope}
          onClose={() => setConnectDialogConnector(null)}
        />
      ) : null}
    </>
  );
}
