'use client';

import { CountBadge } from '@/components/ui/CountBadge';
import { DialogBody, DialogHeader, DialogRoot, DialogSurface } from '@/components/ui/Dialog';
import { CreateAccessModal } from '@/features/access/components/CreateAccessModal';
import { AllAccessPointsList } from '@/features/files/components/access-points/AllAccessPointsList';
import { CreateAccessPointCTACard } from '@/features/files/components/access-points/CreateAccessPointCTACard';
import { DataAccessQuickModal } from '@/features/files/components/access-points/DataAccessQuickModal';
import type { ProviderIconLookup } from '@/features/files/components/access-points/types';
import type { AccessSurface, RepositoryView } from '@/lib/repoApi';
import { repositoryViewKey } from '@/lib/repoApi';

export function DataAccessModalHost({
  projectId,
  accessOverviewOpen,
  quickAccessScope,
  quickAccessConnectors,
  createAccessInitialPath,
  existingScopes,
  connectorsByTarget,
  providerIcons,
  onCloseAccessOverview,
  onOpenExistingAccess,
  onCloseQuickAccess,
  onCreateAccess,
  onOpenFullSettings,
  onCloseCreateAccess,
  onCreated,
}: {
  projectId: string;
  accessOverviewOpen: boolean;
  quickAccessScope: RepositoryView | null;
  quickAccessConnectors: AccessSurface[];
  createAccessInitialPath: string | null;
  existingScopes: RepositoryView[];
  connectorsByTarget: Map<string, AccessSurface[]>;
  providerIcons: ProviderIconLookup;
  onCloseAccessOverview: () => void;
  onOpenExistingAccess: (scope: RepositoryView) => void;
  onCloseQuickAccess: () => void;
  onCreateAccess: (folderPath: string | null | undefined) => void;
  onOpenFullSettings: (targetKey: string) => void;
  onCloseCreateAccess: () => void;
  onCreated: (scope: RepositoryView) => void | Promise<void>;
}) {
  return (
    <>
      {accessOverviewOpen && !quickAccessScope && createAccessInitialPath === null && (
        <DataAccessOverviewModal
          scopes={existingScopes}
          connectorsByTarget={connectorsByTarget}
          providerIcons={providerIcons}
          onClose={onCloseAccessOverview}
          onSelectScope={(targetKey) => {
            const scope = existingScopes.find(
              (item) => repositoryViewKey(item) === targetKey,
            );
            if (scope) onOpenExistingAccess(scope);
          }}
          onCreateAccess={() => onCreateAccess(null)}
        />
      )}

      {quickAccessScope && createAccessInitialPath === null && (
        <DataAccessQuickModal
          scope={quickAccessScope}
          connectors={quickAccessConnectors}
          onClose={onCloseQuickAccess}
          onCreateAccess={onCreateAccess}
          onOpenFullSettings={onOpenFullSettings}
        />
      )}

      {createAccessInitialPath !== null && (
        <CreateAccessModal
          projectId={projectId}
          existingScopes={existingScopes}
          connectorsByTarget={connectorsByTarget}
          initialPath={createAccessInitialPath}
          onClose={onCloseCreateAccess}
          onCreated={onCreated}
        />
      )}
    </>
  );
}

function DataAccessOverviewModal({
  scopes,
  connectorsByTarget,
  providerIcons,
  onClose,
  onSelectScope,
  onCreateAccess,
}: {
  readonly scopes: RepositoryView[];
  readonly connectorsByTarget: Map<string, AccessSurface[]>;
  readonly providerIcons: ProviderIconLookup;
  readonly onClose: () => void;
  readonly onSelectScope: (targetKey: string) => void;
  readonly onCreateAccess: () => void;
}) {
  return (
    <DialogRoot open onClose={onClose} backdrop="strong" dismissOnBackdrop>
      <DialogSurface width={560} ariaLabel="Access">
        <DialogHeader
          title={
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
              <span>Access</span>
              <CountBadge
                value={scopes.length}
                size="md"
                tone="neutral"
              />
            </span>
          }
          description="Manage existing access points or create a new folder boundary."
          onClose={onClose}
        />
        <DialogBody style={{ padding: '10px 20px 20px' }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
            <AllAccessPointsList
              scopes={scopes}
              connectorsByTarget={connectorsByTarget}
              providerIcons={providerIcons}
              currentScopePath={null}
              onSelectScope={onSelectScope}
            />
            <CreateAccessPointCTACard onCreate={onCreateAccess} />
          </div>
        </DialogBody>
      </DialogSurface>
    </DialogRoot>
  );
}
