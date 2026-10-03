'use client';
import { FileImportDialog } from '@/components/FileImportDialog';
import { FolderManageDialog } from '@/components/FolderManageDialog';
import { MoveToDialog } from '@/components/MoveToDialog';
import { NodeRenameDialog } from '@/components/NodeRenameDialog';
import { SupabaseConnectDialog } from '@/components/SupabaseConnectDialog';
import { SupabaseSQLEditorDialog } from '@/components/SupabaseSQLEditorDialog';
import { TableManageDialog } from '@/components/TableManageDialog';
import { BulkDeleteDialog } from '@/features/files/components/BulkDeleteDialog';
import { MoveConfirmDialog } from '@/features/files/components/MoveConfirmDialog';
import type { PendingMoveConfirm } from '@/features/files/hooks/useNodeActions';
import type { ChangeEvent, RefObject } from 'react';

export interface DataPageDialogsProps {
  projectId: string;
  currentFolderId: string | null;
  projects: any[];
  activeProject: any;

  // Rename
  renameDialogOpen: boolean;
  renameTargetName: string;
  renameError: string | null;
  onCloseRename: () => void;
  onRenameConfirm: (newName: string) => Promise<void>;

  // Move
  moveDialogTarget: { id: string; name: string; version_path?: string } | null;
  moveConfirmTarget: PendingMoveConfirm | null;
  onMoveConfirm: (nodeId: string, targetFolderId: string | null) => Promise<void>;
  onMoveFinalConfirm: () => Promise<void>;
  onCloseMove: () => void;
  onCloseMoveConfirm: () => void;

  // Delete
  deleteDialogTarget: { id: string; name: string } | null;
  onDeleteConfirm: () => Promise<void>;
  onCloseDelete: () => void;

  // Table manage dialog
  createTableOpen: boolean;
  onCloseCreateTable: () => void;
  defaultStartOption: 'documents' | 'url';

  // Folder manage dialog
  createFolderOpen: boolean;
  onCloseFolderDialog: () => void;
  onFolderSuccess: () => void;

  // Supabase
  supabaseConnectOpen: boolean;
  onCloseSupabaseConnect: () => void;
  onSupabaseConnected: (importDatabaseSourceId: string) => void;
  supabaseSQLEditorOpen: boolean;
  importDatabaseSourceId: string | null;
  onCloseSupabaseSQLEditor: () => void;
  onSupabaseSaved: () => void;

  // File Import
  fileImportDialogOpen: boolean;
  onCloseFileImport: () => void;
  onFileImportConfirm: (files: File[], mode: 'ocr_parse' | 'raw') => void;
  droppedFiles: File[];
  fileImportTargetLabel: string;
  filePickerInputRef: RefObject<HTMLInputElement>;
  folderPickerInputRef: RefObject<HTMLInputElement>;
  onFilePickerChange: (event: ChangeEvent<HTMLInputElement>) => void;
  onFolderPickerChange: (event: ChangeEvent<HTMLInputElement>) => void;
}

export function DataPageDialogs(props: DataPageDialogsProps) {
  const {
    projectId, currentFolderId, projects, activeProject,
    renameDialogOpen, renameTargetName, renameError, onCloseRename, onRenameConfirm,
    moveDialogTarget, moveConfirmTarget, onMoveConfirm, onMoveFinalConfirm, onCloseMove, onCloseMoveConfirm,
    deleteDialogTarget, onDeleteConfirm, onCloseDelete,
    createTableOpen, onCloseCreateTable, defaultStartOption,
    createFolderOpen, onCloseFolderDialog, onFolderSuccess,
    supabaseConnectOpen, onCloseSupabaseConnect, onSupabaseConnected,
    supabaseSQLEditorOpen, importDatabaseSourceId, onCloseSupabaseSQLEditor, onSupabaseSaved,
    fileImportDialogOpen, onCloseFileImport, onFileImportConfirm, droppedFiles, fileImportTargetLabel,
    filePickerInputRef, folderPickerInputRef, onFilePickerChange, onFolderPickerChange,
  } = props;

  return (
    <>
      {/* Rename Dialog */}
      <NodeRenameDialog
        isOpen={renameDialogOpen}
        currentName={renameTargetName}
        onClose={onCloseRename}
        onConfirm={onRenameConfirm}
        error={renameError}
      />

      {/* Move To Dialog */}
      {moveDialogTarget && (
        <MoveToDialog
          isOpen={true}
          projectId={projectId}
          nodeId={moveDialogTarget.id}
          nodeName={moveDialogTarget.name}
          nodeVersionPath={moveDialogTarget.version_path}
          onConfirm={async (targetFolderId) => {
            await onMoveConfirm(moveDialogTarget.id, targetFolderId);
          }}
          onClose={onCloseMove}
        />
      )}

      {moveConfirmTarget && (
        <MoveConfirmDialog
          open={true}
          nodeName={moveConfirmTarget.nodeName}
          oldPath={moveConfirmTarget.nodePath}
          newPath={moveConfirmTarget.newPath}
          targetLabel={moveConfirmTarget.targetLabel}
          onClose={onCloseMoveConfirm}
          onConfirm={onMoveFinalConfirm}
        />
      )}

      {deleteDialogTarget && (
        <BulkDeleteDialog
          open={true}
          paths={[deleteDialogTarget.id]}
          title={`Delete "${deleteDialogTarget.name}"`}
          noticeTitle={`Delete "${deleteDialogTarget.name}"?`}
          description="This removes the item from the current tree. Previous versions stay recoverable from Puppyone history."
          confirmLabel="Delete"
          onClose={onCloseDelete}
          onConfirm={onDeleteConfirm}
        />
      )}

      {/* Table Create Dialog */}
      {createTableOpen && (
        <TableManageDialog
          mode='create'
          projectId={projectId}
          tableId={null}
          parentId={currentFolderId}
          projects={projects}
          onClose={onCloseCreateTable}
          defaultStartOption={defaultStartOption}
        />
      )}

      {/* Folder Create Dialog */}
      {createFolderOpen && (
        <FolderManageDialog
          projectId={projectId}
          parentId={currentFolderId}
          parentPath={activeProject?.name || ''}
          onClose={onCloseFolderDialog}
          onSuccess={onFolderSuccess}
        />
      )}

      {/* Supabase Connect */}
      {supabaseConnectOpen && (
        <SupabaseConnectDialog
          key={projectId}
          projectId={projectId}
          onClose={onCloseSupabaseConnect}
          onConnected={onSupabaseConnected}
        />
      )}

      {/* Supabase SQL Editor */}
      {supabaseSQLEditorOpen && importDatabaseSourceId && (
        <SupabaseSQLEditorDialog
          projectId={projectId}
          key={`${projectId}:${importDatabaseSourceId}`}
          importDatabaseSourceId={importDatabaseSourceId}
          onClose={onCloseSupabaseSQLEditor}
          onSaved={onSupabaseSaved}
        />
      )}

      <input
        ref={filePickerInputRef}
        type="file"
        multiple
        onChange={onFilePickerChange}
        style={{ display: 'none' }}
      />
      <input
        ref={folderPickerInputRef}
        type="file"
        multiple
        {...({ webkitdirectory: '', directory: '' } as Record<string, string>)}
        onChange={onFolderPickerChange}
        style={{ display: 'none' }}
      />

      {/* File Import (from drag-and-drop) */}
      <FileImportDialog
        isOpen={fileImportDialogOpen}
        onClose={onCloseFileImport}
        onConfirm={onFileImportConfirm}
        initialFiles={droppedFiles.length > 0 ? droppedFiles : undefined}
        targetLabel={fileImportTargetLabel}
      />

      {/* HIDDEN: Tool Creation Panel temporarily disabled */}
    </>
  );
}
