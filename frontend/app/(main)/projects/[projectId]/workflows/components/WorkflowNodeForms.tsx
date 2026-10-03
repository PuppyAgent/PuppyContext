'use client';

import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import { ArrowRight, Check } from 'lucide-react';
import { InlineLoading } from '@/components/loading';
import { ActionButton } from '@/components/ui/ActionButton';
import { DialogBody, DialogFooter } from '@/components/ui/Dialog';
import { TreeDisclosureMarker } from '@/components/ui/TreeDisclosureMarker';
import { Field } from '@/components/ui/Field';
import {
  normalizeTreePath,
  sortNodes,
  type NodeInfo,
} from '@/lib/contentTreeApi';
import { useContentNodes } from '@/lib/hooks/useData';
import { FileGlyphIcon } from '@/lib/fileIcons';
import { updateSynchronizeBinding as updateWorkflowConnection } from '@/lib/synchronizeApi';
import {
  configDraftFrom,
  configPatchFrom,
  hasConfigErrors,
  sourceConfigFrom,
  userConfigFieldsFor,
  validateConfigDraft,
} from '../lib/workflowModel';
import { triggerLabel } from './workflowHelpers';
import { ConfigFieldInput, ProviderMark } from './WorkflowPrimitives';
import { WorkflowTriggerControl } from './WorkflowTriggerControl';
import styles from './WorkflowPage.module.css';
import type { WorkflowShellProps } from './workflowTypes';

type DestinationSettingsFormProps = WorkflowShellProps & {
  targetPath: string;
};

type SyncSettingsFormProps = WorkflowShellProps & {
  targetPath: string;
};

export function SyncSettingsForm({
  model,
  actions,
  targetPath,
}: SyncSettingsFormProps) {
  const configFields = useMemo(
    () => userConfigFieldsFor(model.detailProvider),
    [model.detailProvider],
  );
  const currentConfigDraft = useMemo(
    () => configDraftFrom(configFields, model.selectedConnection?.config),
    [configFields, model.selectedConnection?.config],
  );
  const currentTargetPath = normalizeTreePath(targetPath);
  const [configDraft, setConfigDraft] = useState<Record<string, string>>(currentConfigDraft);
  const [pathDraft, setPathDraft] = useState(currentTargetPath);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setConfigDraft(currentConfigDraft);
    setPathDraft(currentTargetPath);
    setError(null);
  }, [currentConfigDraft, currentTargetPath, model.selectedConnection?.id]);

  const configErrors = validateConfigDraft(configFields, configDraft);
  const configDirty = configFields.some((field) => (
    (configDraft[field.key] ?? '') !== (currentConfigDraft[field.key] ?? '')
  ));
  const normalizedPathDraft = normalizeTreePath(pathDraft);
  const pathDirty = normalizedPathDraft !== currentTargetPath;
  const dirty = configDirty || pathDirty;
  const invalid = hasConfigErrors(configErrors);
  const triggerText = triggerLabel(model.detailTrigger.mode, model.detailTrigger.schedule);
  const sourceConfig = sourceConfigFrom(model.selectedConnection?.config);
  const providerTitle = sourceConfig.resource_name || model.detailProvider?.display_name || 'Source';
  const destinationTitle = normalizedPathDraft ? `/${normalizedPathDraft}` : 'Project root';

  const reset = () => {
    setConfigDraft(currentConfigDraft);
    setPathDraft(currentTargetPath);
    setError(null);
  };

  const save = async () => {
    if (!model.selectedConnection || !dirty || invalid) return;
    setSaving(true);
    setError(null);
    try {
      const patch: { config?: Record<string, unknown>; target_path?: string } = {};
      if (configDirty) {
        patch.config = configPatchFrom(configFields, configDraft);
        if (patch.config.source) {
          patch.config.source = {
            ...sourceConfigFrom(model.selectedConnection.config),
            ...(patch.config.source as Record<string, unknown>),
          };
        }
      }
      if (pathDirty) {
        patch.target_path = normalizedPathDraft;
      }
      await updateWorkflowConnection(model.selectedConnection.id, patch);
      await actions.refreshAll();
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : 'Failed to save sync settings.');
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <DialogBody style={{ padding: '12px 20px 16px' }}>
        <div className={styles.syncSettingsForm}>
          {model.feedback ? (
            <div className={model.feedback.type === 'error' ? `${styles.feedback} ${styles.feedbackError}` : `${styles.feedback} ${styles.feedbackSuccess}`}>
              {model.feedback.text}
            </div>
          ) : null}

          <div className={styles.syncWorkflowMap}>
            <section className={styles.syncWorkflowNode}>
              <div className={styles.syncWorkflowNodeHeader}>
                <span className={styles.syncWorkflowNodeIcon}>
                  <ProviderMark provider={model.detailProvider} />
                </span>
                <span className={styles.syncWorkflowNodeText}>
                  <span className={styles.syncWorkflowNodeTitle}>{providerTitle}</span>
                </span>
              </div>
              <div className={styles.syncWorkflowNodeBody}>
                {configFields.length > 0 ? (
                  <div className={styles.settingsGrid}>
                    {configFields.map((field) => (
                      <Field
                        key={field.key}
                        label={field.label}
                        hint={field.hint}
                        error={configErrors[field.key]}
                      >
                        <ConfigFieldInput
                          field={field}
                          value={configDraft[field.key] ?? ''}
                          invalid={Boolean(configErrors[field.key])}
                          onChange={(value) => setConfigDraft((current) => ({ ...current, [field.key]: value }))}
                        />
                      </Field>
                    ))}
                  </div>
                ) : (
                  <div className={styles.settingsEmpty}>No editable source settings.</div>
                )}
              </div>
            </section>

            <div className={styles.syncWorkflowConnector} aria-label="Sync trigger">
              <span className={styles.syncWorkflowLine} />
              <WorkflowTriggerControl triggerText={triggerText} model={model} actions={actions} />
              <span className={styles.syncWorkflowLine} />
              <span className={styles.syncWorkflowArrow}>
                <ArrowRight size={16} />
              </span>
            </div>

            <section className={styles.syncWorkflowNode}>
              <div className={styles.syncWorkflowNodeHeader}>
                <span className={styles.syncWorkflowNodeIcon}>
                  <img src="/icons/folder.svg" alt="" />
                </span>
                <span className={styles.syncWorkflowNodeText}>
                  <span className={styles.syncWorkflowNodeTitle}>{destinationTitle}</span>
                </span>
              </div>
              <div className={styles.syncWorkflowNodeBody}>
                <ProjectFolderSelect
                  projectId={model.projectId}
                  value={pathDraft}
                  onChange={setPathDraft}
                  allowRoot
                  missingLabel="current folder"
                  showSelection={false}
                />
              </div>
            </section>
          </div>

          {error ? <div className={styles.settingsError}>{error}</div> : null}
        </div>
      </DialogBody>
      <DialogFooter style={{ padding: '0 20px 20px' }}>
        <SettingsActions
          className={styles.settingsFooterActions}
          dirty={dirty}
          invalid={invalid}
          saving={saving}
          onReset={reset}
          onSave={save}
        />
      </DialogFooter>
    </>
  );
}

export function SourceSettingsForm({ model, actions }: WorkflowShellProps) {
  const configFields = useMemo(
    () => userConfigFieldsFor(model.detailProvider),
    [model.detailProvider],
  );
  const [draft, setDraft] = useState<Record<string, string>>(
    () => configDraftFrom(configFields, model.selectedConnection?.config),
  );
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setDraft(configDraftFrom(configFields, model.selectedConnection?.config));
    setError(null);
  }, [configFields, model.selectedConnection?.config, model.selectedConnection?.id]);

  const currentDraft = configDraftFrom(configFields, model.selectedConnection?.config);
  const errors = validateConfigDraft(configFields, draft);
  const dirty = configFields.some((field) => (draft[field.key] ?? '') !== (currentDraft[field.key] ?? ''));
  const invalid = hasConfigErrors(errors);

  const save = async () => {
    if (!model.selectedConnection || !dirty || invalid) return;
    setSaving(true);
    setError(null);
    try {
      const patch = configPatchFrom(configFields, draft);
      if (patch.source) {
        patch.source = {
          ...sourceConfigFrom(model.selectedConnection.config),
          ...(patch.source as Record<string, unknown>),
        };
      }
      await updateWorkflowConnection(model.selectedConnection.id, { config: patch });
      await actions.refreshAll();
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : 'Failed to save source settings.');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className={styles.nodeSettings}>
      {configFields.length > 0 ? (
        <div className={styles.settingsGrid}>
          {configFields.map((field) => (
            <Field
              key={field.key}
              label={field.label}
              hint={field.hint}
              error={errors[field.key]}
            >
              <ConfigFieldInput
                field={field}
                value={draft[field.key] ?? ''}
                invalid={Boolean(errors[field.key])}
                onChange={(value) => setDraft((current) => ({ ...current, [field.key]: value }))}
              />
            </Field>
          ))}
        </div>
      ) : (
        <div className={styles.settingsEmpty}>No editable source settings.</div>
      )}
      {error ? <div className={styles.settingsError}>{error}</div> : null}
      {configFields.length > 0 ? (
        <SettingsActions
          dirty={dirty}
          invalid={invalid}
          saving={saving}
          onReset={() => setDraft(currentDraft)}
          onSave={save}
        />
      ) : null}
    </div>
  );
}

export function DestinationSettingsForm({
  model,
  actions,
  targetPath,
}: DestinationSettingsFormProps) {
  const currentTargetPath = normalizeTreePath(targetPath);
  const [pathDraft, setPathDraft] = useState(currentTargetPath);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setPathDraft(currentTargetPath);
    setError(null);
  }, [currentTargetPath, model.selectedConnection?.id]);

  const normalizedDraft = normalizeTreePath(pathDraft);
  const dirty = normalizedDraft !== currentTargetPath;
  const invalid = false;

  const save = async () => {
    if (!model.selectedConnection || !dirty || invalid) return;
    setSaving(true);
    setError(null);
    try {
      await updateWorkflowConnection(model.selectedConnection.id, {
        target_path: normalizedDraft,
      });
      await actions.refreshAll();
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : 'Failed to save destination settings.');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className={styles.nodeSettings}>
      <div className={styles.settingsGrid}>
        <Field label="Project folder">
          <ProjectFolderSelect
            projectId={model.projectId}
            value={pathDraft}
            onChange={setPathDraft}
            allowRoot
            missingLabel="current folder"
          />
        </Field>
      </div>
      {error ? <div className={styles.settingsError}>{error}</div> : null}
      <SettingsActions
        dirty={dirty}
        invalid={invalid}
        saving={saving}
        onReset={() => setPathDraft(currentTargetPath)}
        onSave={save}
      />
    </div>
  );
}

export function ProjectFolderSelect({
  projectId,
  value,
  onChange,
  allowRoot = false,
  missingLabel = 'new folder',
  showSelection = true,
}: {
  projectId: string;
  value: string;
  onChange: (path: string) => void;
  allowRoot?: boolean;
  missingLabel?: string;
  showSelection?: boolean;
  invalid?: boolean;
}) {
  const normalizedValue = normalizeTreePath(value);
  const [expandedPaths, setExpandedPaths] = useState<ReadonlySet<string>>(
    () => new Set(['', ...ancestorPaths(normalizedValue)]),
  );
  const selectedFolder = useContentNodes(projectId, normalizedValue);

  useEffect(() => {
    if (!normalizedValue) return;
    setExpandedPaths((current) => {
      const next = new Set(current);
      next.add('');
      ancestorPaths(normalizedValue).forEach((path) => next.add(path));
      return next;
    });
  }, [normalizedValue]);

  const isExpanded = useCallback(
    (path: string) => expandedPaths.has(normalizeTreePath(path)),
    [expandedPaths],
  );
  const toggleExpanded = useCallback((path: string) => {
    const normalizedPath = normalizeTreePath(path);
    setExpandedPaths((current) => {
      const next = new Set(current);
      if (next.has(normalizedPath)) next.delete(normalizedPath);
      else next.add(normalizedPath);
      next.add('');
      return next;
    });
  }, []);
  const selectedLabel = normalizedValue ? `/${normalizedValue}` : allowRoot ? 'Project root' : 'Choose a folder';

  return (
    <div className={styles.folderPicker}>
      {showSelection ? (
        <div className={styles.folderPickerSelection} title={selectedLabel}>
          <span>{selectedLabel}</span>
          {normalizedValue && selectedFolder.error ? <em>{missingLabel}</em> : null}
        </div>
      ) : null}
      <div className={styles.folderTreeShell}>
        <div className={styles.folderTreeHeader}>Choose from Files</div>
        <div className={styles.folderTree}>
          <FolderTreeRootRow
            selected={allowRoot && normalizedValue === ''}
            selectable={allowRoot}
            onSelect={() => onChange('')}
          />
          <FolderTreeChildren
            projectId={projectId}
            parentPath=""
            depth={1}
            selectedPath={normalizedValue}
            allowRoot={allowRoot}
            isExpanded={isExpanded}
            onToggle={toggleExpanded}
            onSelect={onChange}
          />
        </div>
      </div>
    </div>
  );
}

function FolderTreeRootRow({
  selected,
  selectable,
  onSelect,
}: {
  selected: boolean;
  selectable: boolean;
  onSelect: () => void;
}) {
  return (
    <div className={selected ? `${styles.folderTreeRow} ${styles.selected}` : styles.folderTreeRow}>
      <span className={styles.folderTreeMarker}>
        <TreeDisclosureMarker expanded />
      </span>
      <span className={styles.folderTreeName}>Root</span>
      {selectable ? (
        <button type="button" className={styles.folderTreeSelect} onClick={onSelect}>
          {selected ? 'Selected' : 'Select'}
        </button>
      ) : null}
    </div>
  );
}

function FolderTreeChildren({
  projectId,
  parentPath,
  depth,
  selectedPath,
  allowRoot: _allowRoot,
  isExpanded,
  onToggle,
  onSelect,
}: {
  projectId: string;
  parentPath: string;
  depth: number;
  selectedPath: string;
  allowRoot: boolean;
  isExpanded: (path: string) => boolean;
  onToggle: (path: string) => void;
  onSelect: (path: string) => void;
}) {
  const { nodes, isLoading, error } = useContentNodes(projectId, parentPath);
  const entries = useMemo(() => sortNodes(nodes), [nodes]);

  if (isLoading) return <FolderTreeMessage depth={depth}><InlineLoading label="Loading" size="xs" /></FolderTreeMessage>;
  if (error) return <FolderTreeMessage depth={depth}>Could not load this folder.</FolderTreeMessage>;
  if (entries.length === 0) return <FolderTreeMessage depth={depth}>Empty folder</FolderTreeMessage>;

  return (
    <>
      {entries.map((node) => {
        const normalizedPath = normalizeTreePath(node.path);
        if (node.type !== 'folder') {
          return <FolderTreeFileRow key={node.path} node={node} depth={depth} />;
        }
        const expanded = isExpanded(normalizedPath);
        const selected = selectedPath === normalizedPath;
        return (
          <div key={node.path}>
            <FolderTreeFolderRow
              node={node}
              depth={depth}
              expanded={expanded}
              selected={selected}
              onToggle={() => onToggle(normalizedPath)}
              onSelect={() => onSelect(normalizedPath)}
            />
            {expanded ? (
              <FolderTreeChildren
                projectId={projectId}
                parentPath={normalizedPath}
                depth={depth + 1}
                selectedPath={selectedPath}
                allowRoot={_allowRoot}
                isExpanded={isExpanded}
                onToggle={onToggle}
                onSelect={onSelect}
              />
            ) : null}
          </div>
        );
      })}
    </>
  );
}

function FolderTreeFolderRow({
  node,
  depth,
  expanded,
  selected,
  onToggle,
  onSelect,
}: {
  node: NodeInfo;
  depth: number;
  expanded: boolean;
  selected: boolean;
  onToggle: () => void;
  onSelect: () => void;
}) {
  return (
    <div
      role="button"
      tabIndex={0}
      className={selected ? `${styles.folderTreeRow} ${styles.selected}` : styles.folderTreeRow}
      style={{ paddingLeft: 8 + depth * 16 }}
      onClick={onToggle}
      onKeyDown={(event) => {
        if (event.key !== 'Enter' && event.key !== ' ') return;
        event.preventDefault();
        onToggle();
      }}
    >
      <span className={styles.folderTreeMarker}>
        <TreeDisclosureMarker expanded={expanded} />
      </span>
      <span className={styles.folderTreeName}>{node.name}</span>
      <button
        type="button"
        className={styles.folderTreeSelect}
        onClick={(event) => {
          event.stopPropagation();
          onSelect();
        }}
      >
        {selected ? 'Selected' : 'Select'}
      </button>
    </div>
  );
}

function FolderTreeFileRow({ node, depth }: { node: NodeInfo; depth: number }) {
  return (
    <div className={`${styles.folderTreeRow} ${styles.file}`} style={{ paddingLeft: 8 + depth * 16 }}>
      <span className={styles.folderTreeMarker} />
      <FileGlyphIcon name={node.name} type={node.type} size={16} />
      <span className={styles.folderTreeName}>{node.name}</span>
    </div>
  );
}

function FolderTreeMessage({ depth, children }: { depth: number; children: ReactNode }) {
  return (
    <div className={styles.folderTreeMessage} style={{ paddingLeft: 28 + depth * 16 }}>
      {children}
    </div>
  );
}

function ancestorPaths(path: string): string[] {
  const parts = normalizeTreePath(path).split('/').filter(Boolean);
  const ancestors: string[] = [];
  for (let index = 1; index < parts.length; index += 1) {
    ancestors.push(parts.slice(0, index).join('/'));
  }
  return ancestors;
}

function SettingsActions({
  className,
  dirty,
  invalid,
  saving,
  onReset,
  onSave,
}: {
  className?: string;
  dirty: boolean;
  invalid: boolean;
  saving: boolean;
  onReset: () => void;
  onSave: () => Promise<void>;
}) {
  return (
    <div className={className ?? styles.settingsActions}>
      <ActionButton
        size="sm"
        variant="secondary"
        disabled={!dirty || saving}
        onClick={onReset}
      >
        Reset
      </ActionButton>
      <ActionButton
        size="sm"
        variant="primary"
        disabled={!dirty || saving || invalid}
        loading={saving}
        leadingIcon={<Check size={14} />}
        onClick={() => void onSave()}
      >
        Save
      </ActionButton>
    </div>
  );
}
