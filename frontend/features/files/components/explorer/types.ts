import type { ContentType } from '@/features/files/components/views/GridView';
import type { EntrypointBadge as DataEntrypointBadge } from '@/features/files/DataLayoutContext';
import type { FileImportTarget } from '@/features/files/hooks/useFileImport';
import type { IntegrityStatus } from '@/lib/contentTreeApi';
import type { CSSProperties, MouseEvent } from 'react';

export type EntrypointBadge = DataEntrypointBadge;
export type ExplorerCreateMenuAction = 'create' | 'access';

export interface MillerColumnItem {
  id: string;
  name: string;
  type: ContentType;
  is_synced?: boolean;
  sync_source?: string | null;
  sync_url?: string | null;
  last_synced_at?: string | null;
  integrity_status?: IntegrityStatus;
}

export interface ExplorerSidebarProps {
  projectId: string;
  currentPath: { id: string; name: string }[];
  activeNodeId?: string;
  onNavigate: (item: MillerColumnItem) => void | boolean;
  onCreate?: (e: MouseEvent<Element>, parentId: string | null) => void;
  onRename?: (id: string, currentName: string) => void;
  onDelete?: (id: string, name: string) => void;
  onDownload?: (id: string, name: string) => void;
  onFilesDrop?: (files: File[], target: FileImportTarget) => void;
  onMoveNode?: (
    nodeId: string,
    targetFolderId: string | null,
    sourceParentId?: string | null,
  ) => Promise<void>;
  // Open the access/expose flow for a specific folder path. Sidebar
  // folder rows call this from their object menu (`Expose as...`);
  // configured folders also expose an inline Access status/action.
  // The row body itself remains a disclosure-only click target.
  onCreateSync?: (event: MouseEvent<Element>, folderPath: string) => void;
  onOpenAccess?: (endpoints: readonly EntrypointBadge[], nodeId: string) => void;
  endpointByNodeId?: ReadonlyMap<string, readonly EntrypointBadge[]>;
  activeSyncNodeId?: string | null;
  highlightNodeId?: string | null;
  highlightVariant?: 'default' | 'access-point';
  createMenuOpenForId?: string | null;
  createMenuOpenAction?: ExplorerCreateMenuAction | null;
  className?: string;
  style?: CSSProperties;
}
