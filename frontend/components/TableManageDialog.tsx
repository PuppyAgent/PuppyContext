'use client';

import { useState, useEffect, useRef, useCallback } from 'react';
import {
  resolveDataTransferSnapshot,
  snapshotDataTransfer,
} from '@/lib/dropFiles';
import type { ProjectInfo } from '../lib/projectsApi';
import { updateTable, deleteTable } from '../lib/projectsApi';
import { refreshProjects } from '../lib/hooks/useData';
import { useOrganization } from '@/contexts/OrganizationContext';
import { useAuth } from '@/contexts/SupabaseAuthProvider';
import { ImportModal } from './editors/table/components/ImportModal';
import { uploadFiles } from '../lib/uploadApi';
import { Dots } from './loading';
import { ActivityIconButton } from './ActivityIconButton';
import { ModalPortal } from './ui/ModalPortal';
import { ActionButton } from './ui/ActionButton';
import { BUTTON_HEIGHT } from './ui/buttonTokens';
import { APP_Z_INDEX } from '@/lib/zIndex';
import { useTaskActions } from '@/contexts/TaskProvider';
import {
  detectImportType,
  supportsCrawlOptions,
  type CrawlOptions,
} from '../lib/importApi';
import CrawlOptionsPanel from './CrawlOptionsPanel';

type StartOption = 'documents' | 'url';
type DialogMode = 'create' | 'edit' | 'delete';

type TableManageDialogProps = {
  mode: DialogMode;
  projectId: string | null;
  tableId: string | null;
  parentId?: string | null;
  projects: ProjectInfo[];
  onClose: () => void;
  onModeChange?: (mode: DialogMode) => void;
  defaultStartOption?: StartOption;
};

export function TableManageDialog({
  mode,
  projectId,
  tableId,
  parentId = null,
  projects,
  onClose,
  onModeChange,
  defaultStartOption = 'documents',
}: TableManageDialogProps) {
  const { session } = useAuth();
  const { currentOrg } = useOrganization();
  const { addPendingTasks, replaceTaskId, updateTaskProgress, updateTaskStatusById } = useTaskActions(projectId, currentOrg?.id);
  void projects;
  const tableName = tableId?.split('/').filter(Boolean).pop() ?? '';

  const [name, setName] = useState(tableName);
  const [loading, setLoading] = useState(false);
  const [startOption] = useState<StartOption>(defaultStartOption);
  const [isDragging, setIsDragging] = useState(false);
  // ``File[]`` rather than ``FileList`` — folder drops produce
  // synthesized File arrays (the entry-walker can't return a real
  // FileList) and we want one shape for both code paths.
  const [selectedFiles, setSelectedFiles] = useState<File[] | null>(null);
  const [urlInput, setUrlInput] = useState('');
  const [showImportModal, setShowImportModal] = useState(false);
  const [crawlOptions, setCrawlOptions] = useState<CrawlOptions>({
    limit: 50,
    maxDepth: 3,
    crawlEntireDomain: true,
    sitemap: 'include',
  });
  // Import mode is fixed at 'raw' for now — the OCR / Smart Parse
  // pipeline is paused on the backend (see `config.ENABLE_OCR`).
  // We deliberately keep no state for this so the picker UI can't
  // be revived by accident; reintroducing the choice is a single
  // local refactor when the backend pipeline comes back online.


  const fileInputRef = useRef<HTMLInputElement>(null);
  const folderInputRef = useRef<HTMLInputElement>(null);
  const dropzoneRef = useRef<HTMLDivElement>(null);
  const urlSupportsCrawlOptions = supportsCrawlOptions(urlInput);
  const urlImportType = detectImportType(urlInput);
  const destinationNamePlaceholder =
    urlImportType === 'github'
      ? 'Optional; defaults to the repository name'
      : urlImportType === 'notion'
        ? 'Optional; defaults to the source page name'
        : 'Optional; used as the folder name in Puppyone';

  useEffect(() => {
    if (tableName) setName(tableName);
  }, [tableName]);

  const handleDragEnter = useCallback((e: React.DragEvent) => {
    e.preventDefault(); e.stopPropagation(); setIsDragging(true);
  }, []);

  const handleDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();

    const rect = e.currentTarget.getBoundingClientRect();
    const { clientX, clientY } = e;
    const isOutside =
      clientX <= rect.left ||
      clientX >= rect.right ||
      clientY <= rect.top ||
      clientY >= rect.bottom;

    if (isOutside) {
      setIsDragging(false);
    }
  }, []);

  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    e.dataTransfer.dropEffect = 'copy';
  }, []);

  // Reusable: pick a sensible Context name from the first file's
  // path. For folder drops we want the FOLDER name, not the first
  // file inside it; for plain file drops we strip the extension.
  const inferNameFromFiles = useCallback(
    (files: File[]) => {
      if (name.trim()) return;
      const firstFile = files[0];
      if (!firstFile) return;
      if (firstFile.webkitRelativePath) {
        const folderName = firstFile.webkitRelativePath.split('/')[0];
        setName(folderName);
      } else {
        const fn = firstFile.name;
        setName(fn.substring(0, fn.lastIndexOf('.')) || fn);
      }
    },
    [name],
  );

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      e.stopPropagation();
      setIsDragging(false);
      // Snapshot synchronously: see lib/dropFiles.ts. Without this,
      // dragging a folder produces "1 file, 0 KB" instead of
      // walking into its contents.
      const snapshot = snapshotDataTransfer(e.nativeEvent);
      void resolveDataTransferSnapshot(snapshot).then((files) => {
        if (files.length === 0) return;
        setSelectedFiles(files);
        inferNameFromFiles(files);
      });
    },
    [inferNameFromFiles],
  );

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files.length > 0) {
      const files = Array.from(e.target.files);
      setSelectedFiles(files);
      inferNameFromFiles(files);
    }
  };


  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;

    // Direct submit with current config
    await handleFinalSubmit();
  };

  const handleFinalSubmit = async () => {
    try {
      setLoading(true);

      if (mode === 'edit' && tableId) {
        await updateTable(projectId || '', tableId, name.trim());
        await refreshProjects(currentOrg?.id);
        onClose();
        return;
      }

      if (startOption === 'documents' && selectedFiles && selectedFiles.length > 0) {
        if (!projectId || !session?.access_token) {
          throw new Error('Missing project ID or auth');
        }

        const files = Array.from(selectedFiles);
        // ``parentId`` here is actually the parent version path (the
        // explorer passes its current folder path through this prop;
        // the name is a holdover from the legacy `parent_id` field).
        // We treat empty string as "root".
        const parentPath = (parentId || '').trim();

        // Optimistic refresh while we kick off the upload. The
        // authoritative refresh happens AFTER the worker writes the
        // file into the Version Engine (task completion invalidates the
        // corresponding project queries throughout the app).
        await refreshProjects(currentOrg?.id);
        onClose();

        // Direct-to-S3 upload pipeline. We don't await this — the
        // dialog has already closed and the user follows progress
        // via TaskStatusWidget. Errors are surfaced into the widget
        // through ``onTaskFailed``; we only need a top-level catch
        // for unexpected issues (e.g. the init call failing before
        // any task IDs were created).
        //
        // Why background instead of awaited:
        //   - User wants to keep working while the upload runs
        //   - Multipart uploads of GB-scale files take minutes
        //   - The widget owns progress UX from here
        (async () => {
          const placeholderIds: string[] = [];
          try {
            await uploadFiles(
              {
                projectId,
                files,
                parentPath: parentPath || null,
              },
              session!.access_token,
              {
                // Spawn placeholder rows the instant the dialog
                // closes — without this the widget stays empty for
                // the 2–5s init takes on a cold backend.
                onUploadStart: (entries) => {
                  entries.forEach((f) => {
                    const tmpId = `tmp-${crypto.randomUUID()}`;
                    placeholderIds[f.fileIndex] = tmpId;
                  });
                  addPendingTasks(
                    entries.map((f) => ({
                      taskId: placeholderIds[f.fileIndex],
                      projectId,
                      tableId: undefined,
                      tableName: f.filename,
                      filename: f.filename,
                      status: 'uploading',
                    })),
                  );
                },
                onTaskCreated: ({ fileIndex, taskId }) => {
                  const tmpId = placeholderIds[fileIndex];
                  if (tmpId) {
                    replaceTaskId(tmpId, taskId);
                    placeholderIds[fileIndex] = taskId;
                  }
                },
                onProgress: (taskId, _loaded, _total, percent) => {
                  updateTaskProgress(taskId, percent);
                },
                onAllPartsUploaded: (taskId) => {
                  // Bytes are in S3 — server is now writing into the Version Engine.
                  // ``finalizing`` keeps the row visibly active so it
                  // doesn't read as "Uploading 100%" frozen.
                  updateTaskStatusById(taskId, 'finalizing');
                },
                onTaskCompleted: (taskId) => {
                  // Inline finalize: /upload/complete returns 200
                  // only after the Version Engine has the bytes, so the task is
                  // already COMPLETED in the DB.
                  updateTaskStatusById(taskId, 'completed');
                },
                onTaskFailed: (taskId, error) => {
                  updateTaskStatusById(taskId, 'failed', { error });
                },
              },
            );
          } catch (uploadError) {
            // /upload/init failed — flip every placeholder to
            // ``failed`` so the widget surfaces a stable terminal
            // state instead of leaving uploading rows orphaned.
            const errMsg =
              uploadError instanceof Error
                ? uploadError.message
                : String(uploadError);
            placeholderIds.forEach((id) => {
              if (id) updateTaskStatusById(id, 'failed', { error: errMsg });
            });
            console.error('Direct-to-S3 upload init failed:', uploadError);
          }
          // Each completed task invalidates the owning project's SWR queries,
          // even though this dialog has already closed.
        })();

        return;
      }
    } catch (error) {
      console.error('Failed to save table:', error);
      alert('Operation failed: ' + (error instanceof Error ? error.message : 'Unknown error'));
    } finally {
      setLoading(false);
    }
  };

  const handleDelete = async () => {
    if (!tableId) return;
    try {
      setLoading(true);
      await deleteTable(projectId || '', tableId);
      await refreshProjects(currentOrg?.id);
      onClose();
    } catch (error) {
      alert('Delete failed: ' + (error instanceof Error ? error.message : 'Unknown error'));
    } finally {
      setLoading(false);
    }
  };

  // Title logic
  const getDialogTitle = () => {
    if (mode === 'delete') return 'Delete context';
    if (mode === 'edit') return 'Edit context';
    return startOption === 'documents' ? 'Upload files' : 'Import URL';
  };

  return (
    <ModalPortal>
    <div
      role='presentation'
      style={{
        position: 'fixed', inset: 0, background: 'var(--po-backdrop)',
        display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: APP_Z_INDEX.modal,
        backdropFilter: 'blur(2px)'
      }}
      onClick={onClose}
    >
      <div
        role='dialog'
        aria-modal='true'
        aria-label={getDialogTitle()}
        style={{
          background: 'var(--po-overlay)',
          border: '1px solid var(--po-border)',
          borderRadius: 12,
          width: 520, // Slightly simpler width
          maxWidth: '90vw',
          boxShadow: '0 24px 48px var(--po-shadow)',
          display: 'flex', flexDirection: 'column', overflow: 'hidden',
          animation: 'dialog-fade-in 0.2s ease-out',
        }}
        onClick={e => e.stopPropagation()}
      >
        <style jsx>{`
          @keyframes dialog-fade-in {
            from { opacity: 0; transform: scale(0.98); }
            to { opacity: 1; transform: scale(1); }
          }
          input::placeholder { color: var(--po-text-disabled); }
        `}</style>

        {/* Header */}
        <div style={{
          padding: '14px 20px 8px',
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        }}>
          <h2 style={{ margin: 0, fontSize: 13, fontWeight: 500, color: 'var(--po-text-muted)' }}>
            {getDialogTitle()}
          </h2>
          <ActivityIconButton kind="close" title="Close" onClick={onClose} />
        </div>

        {mode === 'delete' ? (
          <div style={{ padding: 24 }}>
            <p style={{ color: 'var(--po-text-muted)', fontSize: 16, lineHeight: 1.6, margin: '0 0 24px' }}>
              Are you sure you want to delete <strong style={{ color: 'var(--po-text)' }}>{tableName || 'this context'}</strong>? This cannot be undone.
            </p>
            <div style={{ display: 'flex', gap: 10, justifyContent: 'flex-end' }}>
              <ActionButton type='button' onClick={onClose}>Cancel</ActionButton>
              <ActionButton type='button' onClick={handleDelete} variant='danger' loading={loading}>
                {loading && <Dots size="xs" tone="danger" />}
                {loading ? 'Deleting…' : 'Delete'}
              </ActionButton>
            </div>
          </div>
        ) : (
          <form onSubmit={handleSubmit} style={{ display: 'flex', flexDirection: 'column' }}>
            <div style={{ padding: '12px 24px 20px', display: 'flex', flexDirection: 'column', gap: 18 }}>

              {startOption === 'documents' && (
                <>
                  <input
                    ref={fileInputRef}
                    type='file'
                    multiple
                    onChange={handleFileSelect}
                    style={{ display: 'none' }}
                  />
                  <input
                    ref={folderInputRef}
                    type='file'
                    {...({ webkitdirectory: '', directory: '' } as any)}
                    multiple
                    onChange={handleFileSelect}
                    style={{ display: 'none' }}
                  />

                  {selectedFiles && selectedFiles.length > 0 ? (
                    <div
                      style={{
                        border: '1px solid var(--po-border)',
                        borderRadius: 8,
                        background: 'var(--po-panel-raised)',
                        overflow: 'hidden',
                      }}
                    >
                      <div
                        style={{
                          padding: '8px 12px',
                          display: 'flex',
                          justifyContent: 'space-between',
                          alignItems: 'center',
                          borderBottom: '1px solid var(--po-border-subtle)',
                        }}
                      >
                        <span style={{ fontSize: 12, color: 'var(--po-text-muted)' }}>
                          {selectedFiles.length} file{selectedFiles.length === 1 ? '' : 's'} selected
                        </span>
                        <button
                          type='button'
                          onClick={() => {
                            setSelectedFiles(null);
                            setName('');
                          }}
                          style={{
                            height: 30,
                            background: 'transparent',
                            border: 'none',
                            color: 'var(--po-text-subtle)',
                            fontSize: 12,
                            cursor: 'pointer',
                            padding: '0 8px',
                            borderRadius: 4,
                            transition: 'color 0.12s, background 0.12s',
                          }}
                          onMouseEnter={e => {
                            e.currentTarget.style.color = 'var(--po-text)';
                            e.currentTarget.style.background = 'var(--po-hover)';
                          }}
                          onMouseLeave={e => {
                            e.currentTarget.style.color = 'var(--po-text-subtle)';
                            e.currentTarget.style.background = 'transparent';
                          }}
                        >
                          Clear
                        </button>
                      </div>
                      <div
                        style={{
                          maxHeight: 180,
                          overflowY: 'auto',
                          padding: '6px 8px',
                          display: 'flex',
                          flexDirection: 'column',
                          gap: 2,
                        }}
                      >
                        {Array.from(selectedFiles)
                          .slice(0, 5)
                          .map((file, i) => (
                            <div
                              key={i}
                              style={{
                                display: 'flex',
                                alignItems: 'center',
                                gap: 8,
                                padding: '6px 8px',
                                borderRadius: 4,
                              }}
                            >
                              {/*
                                Filename was rendered at 16px, which
                                fought with the rest of the product's
                                13px content scale and made the file
                                list look like a different surface.
                                Dropped to 13px for parity.
                              */}
                              <span
                                style={{
                                  fontSize: 13,
                                  color: 'var(--po-text)',
                                  overflow: 'hidden',
                                  textOverflow: 'ellipsis',
                                  whiteSpace: 'nowrap',
                                  flex: 1,
                                  minWidth: 0,
                                }}
                              >
                                {file.name}
                              </span>
                              <span
                                style={{
                                  fontSize: 11,
                                  color: 'var(--po-text-subtle)',
                                  whiteSpace: 'nowrap',
                                  flexShrink: 0,
                                }}
                              >
                                {(file.size / 1024).toFixed(0)} KB
                              </span>
                            </div>
                          ))}
                        {selectedFiles.length > 5 && (
                          <div
                            style={{
                              fontSize: 11,
                              color: 'var(--po-text-subtle)',
                              textAlign: 'center',
                              padding: 4,
                            }}
                          >
                            + {selectedFiles.length - 5} more
                          </div>
                        )}
                      </div>
                    </div>
                  ) : (
                    <div
                      ref={dropzoneRef}
                      onDragEnter={handleDragEnter}
                      onDragLeave={handleDragLeave}
                      onDragOver={handleDragOver}
                      onDrop={handleDrop}
                      style={{
                        padding: '28px 20px',
                        border: '1px dashed',
                        borderColor: isDragging ? 'var(--po-focus-ring)' : 'var(--po-border-strong)',
                        borderRadius: 8,
                        // No solid fill — the dashed border alone
                        // signals "drop here", letting the dialog's
                        // own surface read through. The previous
                        // `#27272A` fill made the dropzone look like
                        // a separate panel slammed onto the dialog.
                        background: isDragging ? 'var(--po-selected)' : 'transparent',
                        display: 'flex',
                        flexDirection: 'column',
                        alignItems: 'center',
                        gap: 12,
                        transition: 'all 0.15s',
                      }}
                    >
                      <svg
                        width='22'
                        height='22'
                        viewBox='0 0 24 24'
                        fill='none'
                        stroke={isDragging ? 'var(--po-accent)' : 'var(--po-text-subtle)'}
                        strokeWidth='1.5'
                        strokeLinecap='round'
                        strokeLinejoin='round'
                      >
                        <path d='M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4'></path>
                        <polyline points='17 8 12 3 7 8'></polyline>
                        <line x1='12' y1='3' x2='12' y2='15'></line>
                      </svg>
                      <div style={{ fontSize: 13, color: 'var(--po-text-muted)', textAlign: 'center' }}>
                        Drag and drop files or folders here
                      </div>
                      <div style={{ display: 'flex', gap: 8 }}>
                        <button
                          type='button'
                          onClick={() => fileInputRef.current?.click()}
                          style={dropzoneActionButton}
                          onMouseEnter={e => {
                            e.currentTarget.style.background = 'var(--po-border-subtle)';
                            e.currentTarget.style.borderColor = 'var(--po-border-strong)';
                          }}
                          onMouseLeave={e => {
                            e.currentTarget.style.background = 'transparent';
                            e.currentTarget.style.borderColor = 'var(--po-border-strong)';
                          }}
                        >
                          Upload Files
                        </button>
                        <button
                          type='button'
                          onClick={() => folderInputRef.current?.click()}
                          style={dropzoneActionButton}
                          onMouseEnter={e => {
                            e.currentTarget.style.background = 'var(--po-border-subtle)';
                            e.currentTarget.style.borderColor = 'var(--po-border-strong)';
                          }}
                          onMouseLeave={e => {
                            e.currentTarget.style.background = 'transparent';
                            e.currentTarget.style.borderColor = 'var(--po-border-strong)';
                          }}
                        >
                          Upload Folder
                        </button>
                      </div>
                    </div>
                  )}

                  {selectedFiles && selectedFiles.length > 0 && (
                    <div>
                      <label style={labelStyle}>Context Name</label>
                      <input
                        type='text'
                        value={name}
                        onChange={e => setName(e.target.value)}
                        style={inputStyle}
                        placeholder='Enter a name for this context…'
                      />
                    </div>
                  )}
                </>
              )}

              {startOption === 'url' && (
                <>
                  <div>
                    <label style={labelStyle}>URL</label>
                    <input
                      type='text'
                      placeholder='https://github.com/org/repo, https://notion.so/..., or https://example.com'
                      value={urlInput}
                      onChange={e => setUrlInput(e.target.value)}
                      style={inputStyle}
                      autoFocus
                    />
                  </div>
                  {urlSupportsCrawlOptions && (
                    <CrawlOptionsPanel url={urlInput} options={crawlOptions} onChange={setCrawlOptions} />
                  )}
                  {urlInput.trim() && (
                    <div>
                      <label style={labelStyle}>Destination name (optional)</label>
                      <input type='text' value={name} onChange={e => setName(e.target.value)} placeholder={destinationNamePlaceholder} style={inputStyle} />
                    </div>
                  )}
                </>
              )}

            </div>

            {/* Footer */}
            <div style={{
              padding: '0 20px 20px',
              display: 'flex', justifyContent: 'flex-end', gap: 10,
              background: 'var(--po-overlay)',
            }}>
              <ActionButton type='button' onClick={onClose}>Cancel</ActionButton>
              <ActionButton
                type={startOption === 'url' ? 'button' : 'submit'}
                onClick={
                  startOption === 'url' ? (urlInput.trim() ? () => setShowImportModal(true) : undefined) :
                  undefined
                }
                disabled={
                  loading ||
                  (startOption === 'documents' && (!selectedFiles || selectedFiles.length === 0 || !name.trim())) ||
                  (startOption === 'url' && !urlInput.trim())
                }
                variant='primary'
                loading={loading}
              >
                {mode === 'edit' ? 'Save Changes' : startOption === 'documents' ? 'Start Import' : 'Import'}
              </ActionButton>
            </div>
          </form>
        )}
      </div>

      {showImportModal && projectId && (
        <ImportModal
          visible={showImportModal}
          projectId={projectId || ''}
          mode='create_table'
          tableName={name}
          initialUrl={urlInput}
          initialCrawlOptions={urlSupportsCrawlOptions ? crawlOptions : undefined}
          onClose={() => { setShowImportModal(false); setUrlInput(''); }}
          onSuccess={() => { setShowImportModal(false); setUrlInput(''); refreshProjects(currentOrg?.id); onClose(); }}
        />
      )}

    </div>
    </ModalPortal>
  );
}

// Aligned with the rest of the product's tokens — section labels
// across the data explorer / access panel use this same uppercase
// 11px treatment ("ALL SCOPES IN THIS …", "CONNECT", "INTEGRATIONS").
// The dialog used to use 12px lower-case labels which read as a
// different design family.
const labelStyle: React.CSSProperties = {
  fontSize: 11,
  fontWeight: 600,
  color: 'var(--po-text-subtle)',
  textTransform: 'uppercase',
  letterSpacing: '0.05em',
  marginBottom: 8,
  display: 'block',
};

// Form-field styling matched to the menu / sidebar surfaces
// (var(--po-canvas) family). The previous version used a brighter
// `#27272A` fill that read as a sunken card on the dialog
// background and 16px text that towered over the explorer rows
// (13px) launching the dialog. 13px keeps the dialog visually
// at parity with everything else.
const inputStyle: React.CSSProperties = {
  width: '100%',
  height: 32,
  padding: '0 12px',
  background: 'var(--po-panel-raised)',
  border: '1px solid var(--po-border-strong)',
  borderRadius: 6,
  color: 'var(--po-text)',
  fontSize: 13,
  outline: 'none',
  transition: 'border-color 0.15s',
  boxSizing: 'border-box',
};

// Used inside the dropzone for `Upload Files` / `Upload Folder`.
// Lighter than the footer primary so the visual hierarchy stays
// "primary action lives in the footer" — the dropzone buttons are
// shortcut affordances, not the call-to-action.
const dropzoneActionButton: React.CSSProperties = {
  height: BUTTON_HEIGHT,
  padding: '0 14px',
  borderRadius: 6,
  border: '1px solid var(--po-border-strong)',
  background: 'transparent',
  color: 'var(--po-text)',
  fontSize: 13,
  fontWeight: 500,
  cursor: 'pointer',
  transition: 'background 0.15s, border-color 0.15s',
};
