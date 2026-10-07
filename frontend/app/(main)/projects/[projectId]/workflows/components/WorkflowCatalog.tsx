'use client';

import { useEffect, useState } from 'react';
import {
  ArrowRight,
  Check,
  ChevronRight,
  ExternalLink,
  Pause,
  Play,
  Plus,
  Trash2,
} from 'lucide-react';
import { ActionButton } from '@/components/ui/ActionButton';
import { CountBadge } from '@/components/ui/CountBadge';
import { StatusIndicator, type StatusDotStatus } from '@/components/ui/StatusDot';
import { oauth, openOAuthPopup, type OAuthStatusResponse } from '@/lib/oauthApi';
import {
  DialogBody,
  DialogHeader,
  DialogRoot,
  DialogSurface,
} from '@/components/ui/Dialog';
import { Field } from '@/components/ui/Field';
import {
  listSynchronizeProviderResources as listWorkflowProviderResources,
  type SynchronizeBinding as WorkflowConnection,
  type SynchronizeProviderSpec as WorkflowProviderSpec,
  type SynchronizeSourceResource as WorkflowSourceResource,
} from '@/lib/synchronizeApi';
import {
  ConfigFieldInput,
  IconButton,
  ProviderMark,
} from './WorkflowPrimitives';
import {
  ProjectFolderSelect,
  SyncSettingsForm,
} from './WorkflowNodeForms';
import { WorkflowTriggerControl } from './WorkflowTriggerControl';
import {
  dataPath,
  labelize,
  providerName,
  providerOAuthType,
  providerUsesOAuth,
  triggerFromStatus,
  triggerLabel,
  formatDate,
  workflowName,
} from './workflowHelpers';
import styles from './WorkflowIntegrations.module.css';
import sharedStyles from './WorkflowPage.module.css';
import type { WorkflowShellProps } from './workflowTypes';
import { Dots, InlineLoading, PageLoading } from '@/components/loading';

type ProviderAuthState = {
  loading: boolean;
  connected: boolean;
  label: string;
  error?: boolean;
};

export function WorkflowCatalog({ model, actions }: WorkflowShellProps) {
  const [newProviderId, setNewProviderId] = useState<string | null>(null);
  const [manageConnectionId, setManageConnectionId] = useState<string | null>(null);

  useEffect(() => {
    if (model.mode === 'new' && !newProviderId) {
      setNewProviderId(model.selectedProviderId || model.visibleProviders[0]?.provider || null);
    }
    if (model.mode === 'detail' && newProviderId) {
      setNewProviderId(null);
    }
  }, [model.mode, model.selectedProviderId, model.visibleProviders, newProviderId]);

  const connections = [...model.connections].sort((left, right) => {
    const leftProvider = providerForId(model, left.provider).display_name;
    const rightProvider = providerForId(model, right.provider).display_name;
    return leftProvider.localeCompare(rightProvider) || (left.path || '').localeCompare(right.path || '');
  });
  const providerGroups = groupConnectionsByProvider(model, connections);

  const closeNewDialog = () => {
    setNewProviderId(null);
    actions.cancelNew();
  };

  return (
    <section className={styles.integrationCatalog}>
      {model.connectionsLoading ? (
        <div className={sharedStyles.blankDetail}>
          <PageLoading variant="fill" />
        </div>
      ) : connections.length === 0 ? (
        <div className={styles.emptyCatalogPanel}>
          <h2>No syncs yet</h2>
          <p>Create a sync to bring an external resource into this project.</p>
          <ActionButton
            variant="secondary"
            size="sm"
            leadingIcon={<Plus size={14} />}
            onClick={actions.startNew}
          >
            Add sync
          </ActionButton>
        </div>
      ) : (
        <ConnectionsSection
          providerGroups={providerGroups}
          manageConnectionId={manageConnectionId}
          setNewProviderId={setNewProviderId}
          setManageConnectionId={setManageConnectionId}
          model={model}
          actions={actions}
        />
      )}

      {newProviderId ? (
        <NewSyncDialog
          provider={model.providersById.get(newProviderId)}
          model={model}
          actions={actions}
          onProviderChange={(providerId) => {
            actions.setSelectedProviderId(providerId);
            setNewProviderId(providerId);
          }}
          onClose={closeNewDialog}
        />
      ) : null}

      {manageConnectionId && model.selectedConnection?.id === manageConnectionId ? (
        <ManageSyncDialog
          provider={model.providersById.get(model.selectedConnection.provider)}
          model={model}
          actions={actions}
          onClose={() => setManageConnectionId(null)}
        />
      ) : null}
    </section>
  );
}

function ConnectionsSection({
  providerGroups,
  setNewProviderId,
  manageConnectionId,
  setManageConnectionId,
  model,
  actions,
}: WorkflowShellProps & {
  providerGroups: Array<{
    provider: WorkflowProviderSpec;
    connections: WorkflowConnection[];
  }>;
  manageConnectionId: string | null;
  setNewProviderId: (providerId: string | null) => void;
  setManageConnectionId: (connectionId: string | null) => void;
}) {
  const startNewSync = () => {
    const providerId = model.selectedProviderId || model.visibleProviders[0]?.provider || null;
    if (providerId) {
      actions.setSelectedProviderId(providerId);
      setNewProviderId(providerId);
    }
    actions.startNew();
    setManageConnectionId(null);
  };

  return (
    <section className={styles.integrationSection}>
      <div className={styles.integrationHeading}>
        <button type="button" className={styles.addSyncButton} onClick={startNewSync}>
          <Plus size={14} />
          Add sync
        </button>
      </div>

      <div className={styles.integrationDetail}>
        <div className={styles.connectionStack}>
          {providerGroups.length === 0 ? (
            <div className={styles.emptyConnections}>No project syncs yet.</div>
          ) : (
            providerGroups.map(({ provider, connections }) => (
              <ProviderConnectionGroup
                key={provider.provider}
                provider={provider}
                connections={connections}
                manageConnectionId={manageConnectionId}
                setManageConnectionId={setManageConnectionId}
                model={model}
                actions={actions}
              />
            ))
          )}
        </div>
      </div>
    </section>
  );
}

function ProviderConnectionGroup({
  provider,
  connections,
  manageConnectionId,
  setManageConnectionId,
  model,
  actions,
}: WorkflowShellProps & {
  provider: WorkflowProviderSpec;
  connections: WorkflowConnection[];
  manageConnectionId: string | null;
  setManageConnectionId: (connectionId: string | null) => void;
}) {
  return (
    <section className={styles.providerGroup}>
      <div className={styles.providerGroupHeader}>
        <h3 className={styles.providerSummaryTitle}>{provider.display_name}</h3>
        <CountBadge value={connections.length} size="sm" tone="muted" />
      </div>
      <div className={styles.providerGroupBody}>
        <div className={styles.providerSummary} aria-label={`${provider.display_name} connections`}>
          <div className={styles.providerHeroIcon}>
            <ProviderMark provider={provider} />
          </div>
        </div>
        <div className={styles.providerConnectionList}>
          {connections.map((connection) => (
            <ConnectionCard
              key={connection.id}
              provider={provider}
              connection={connection}
              selected={manageConnectionId === connection.id}
              onManage={() => {
                actions.selectConnection(connection.id);
                setManageConnectionId(connection.id);
              }}
              model={model}
            />
          ))}
        </div>
      </div>
    </section>
  );
}

function ConnectionCard({
  provider,
  connection,
  selected,
  onManage,
  model,
}: {
  provider: WorkflowProviderSpec;
  connection: WorkflowConnection;
  selected: boolean;
  onManage: () => void;
  model: WorkflowShellProps['model'];
}) {
  const status = model.statusById.get(connection.id);
  const trigger = triggerFromStatus(status);
  const sourceLabel = sourceConfigLabel(provider, connection);
  const statusLabel = labelize(status?.status || connection.status || 'active');
  const normalizedStatus = statusLabel.toLowerCase();
  const statusIndicator = workflowConnectionStatus(normalizedStatus);
  const lastSynced = status?.last_synced_at ? `Last synced ${formatDate(status.last_synced_at)}` : 'Never synced';
  const errorMessage = status?.error_message || connection.error_message || '';
  const statusTitle = errorMessage ? `${statusLabel}: ${errorMessage}` : statusLabel;

  return (
    <article className={selected ? `${styles.connectionCard} ${styles.selected}` : styles.connectionCard}>
      <div className={styles.connectionRoute}>
        <span className={styles.sourceConfig} title={`${provider.display_name}: ${sourceLabel}`}>
          <ProviderMark provider={provider} />
          <span className={styles.sourceName}>{provider.display_name}</span>
        </span>
        <ArrowRight size={15} />
        <ProjectPathTrail path={connection.path} />
      </div>
      <div className={styles.connectionRight}>
        <div className={styles.connectionMeta}>
          <StatusIndicator
            className={styles.statusMeta}
            status={statusIndicator}
            label={statusLabel}
            title={statusTitle}
          />
          <span className={styles.syncMetaText}>
            {triggerLabel(trigger.mode, trigger.schedule)}
            <span className={styles.metaDivider}>·</span>
            {lastSynced}
          </span>
        </div>
        <div className={styles.connectionActions}>
          <button type="button" className={styles.manageButton} onClick={onManage}>
            Manage
            <ChevronRight size={13} />
          </button>
        </div>
      </div>
    </article>
  );
}

function ProjectPathTrail({ path }: { path: string | null | undefined }) {
  const segments = normalizeProjectPathSegments(path);
  const title = segments.length === 0 ? 'Project root' : `/${segments.join('/')}`;

  if (segments.length === 0) {
    return (
      <span className={styles.pathTrail} title={title}>
        <img className={styles.pathIcon} src="/icons/folder.svg" alt="" />
        <span className={styles.pathText}>/</span>
      </span>
    );
  }

  return (
    <span className={styles.pathTrail} title={title}>
      <img className={styles.pathIcon} src="/icons/folder.svg" alt="" />
      <span className={styles.pathText}>/ {segments.join(' / ')}</span>
    </span>
  );
}

function ManageSyncDialog({
  provider,
  model,
  actions,
  onClose,
}: WorkflowShellProps & {
  provider?: WorkflowProviderSpec;
  onClose: () => void;
}) {
  if (!model.selectedConnection) return null;
  const targetTitle = model.selectedConnection.path ? `/${model.selectedConnection.path}` : 'Project root';
  const title = workflowName(model.selectedConnection, provider);
  const sourceName = providerName(provider, model.selectedConnection.provider);

  return (
    <DialogRoot open onClose={onClose} backdrop="strong">
      <DialogSurface width={860} ariaLabel="Manage sync">
        <DialogHeader
          title={title}
          description={`${sourceName} syncs into ${targetTitle}`}
          onClose={onClose}
        >
          <ActionButton
            variant="secondary"
            size="sm"
            disabled={model.selectedBusy !== null}
            loading={model.selectedBusy === 'refresh'}
            leadingIcon={<Play size={15} />}
            onClick={() => void actions.runAction(model.selectedConnection!.id, 'refresh')}
          >
            Run now
          </ActionButton>
          <ActionButton
            variant="secondary"
            size="sm"
            disabled={model.selectedBusy !== null}
            loading={model.selectedBusy === 'pause' || model.selectedBusy === 'resume'}
            leadingIcon={model.paused ? <Play size={15} /> : <Pause size={15} />}
            onClick={() => void actions.runAction(model.selectedConnection!.id, model.paused ? 'resume' : 'pause')}
          >
            {model.paused ? 'Resume' : 'Pause'}
          </ActionButton>
          <IconButton
            title="Open in Files"
            disabled={!model.selectedConnection.path}
            onClick={() => window.location.assign(dataPath(model.projectId, model.selectedConnection?.path))}
          >
            <ExternalLink size={15} />
          </IconButton>
          <IconButton
            title="Delete"
            disabled={model.selectedBusy !== null}
            onClick={() => void actions.runAction(model.selectedConnection!.id, 'delete')}
          >
            {model.selectedBusy === 'delete' ? <Dots size="sm" ariaLabel="Deleting" /> : <Trash2 size={15} />}
          </IconButton>
        </DialogHeader>
        <SyncSettingsForm
          targetPath={model.selectedConnection.path ?? ''}
          model={model}
          actions={actions}
        />
      </DialogSurface>
    </DialogRoot>
  );
}

function NewSyncDialog({
  provider,
  model,
  actions,
  onProviderChange,
  onClose,
}: WorkflowShellProps & {
  provider?: WorkflowProviderSpec;
  onProviderChange: (providerId: string) => void;
  onClose: () => void;
}) {
  const [step, setStep] = useState<'source' | 'configure'>('source');
  const [authStatuses, setAuthStatuses] = useState<Record<string, ProviderAuthState>>({});
  const [authActionProviderId, setAuthActionProviderId] = useState<string | null>(null);
  const triggerText = triggerLabel(model.detailTrigger.mode, model.detailTrigger.schedule);
  const createDisabled = model.creating || model.missingRequired || model.missingSource || !model.targetPath.trim() || Boolean(model.triggerError);
  const createHint = model.missingSource
    ? 'Choose a source resource'
    : model.missingRequired
    ? 'Fill required fields'
    : !model.targetPath.trim()
      ? 'Add a project path'
      : model.triggerError || 'Ready to create';

  useEffect(() => {
    let cancelled = false;

    const checkProvider = async (item: WorkflowProviderSpec) => {
      const oauthType = providerOAuthType(item);
      if (!providerUsesOAuth(item) || !oauthType) {
        setAuthStatuses((current) => ({
          ...current,
          [item.provider]: {
            loading: false,
            connected: true,
            label: 'Authorized',
          },
        }));
        return;
      }

      setAuthStatuses((current) => ({
        ...current,
        [item.provider]: { ...current[item.provider], loading: true },
      }));
      try {
        const status = await oauth[oauthType].getStatus();
        if (cancelled) return;
        setAuthStatuses((current) => ({
          ...current,
          [item.provider]: authStateFromStatus(status),
        }));
      } catch {
        if (cancelled) return;
        setAuthStatuses((current) => ({
          ...current,
          [item.provider]: {
            loading: false,
            connected: false,
            label: 'Unable to check authorization',
            error: true,
          },
        }));
      }
    };

    model.visibleProviders.forEach((item) => {
      void checkProvider(item);
    });

    return () => {
      cancelled = true;
    };
  }, [model.visibleProviders]);

  const refreshProviderAuth = async (item: WorkflowProviderSpec) => {
    const oauthType = providerOAuthType(item);
    if (!oauthType) return;
    setAuthStatuses((current) => ({
      ...current,
      [item.provider]: { ...current[item.provider], loading: true },
    }));
    const status = await oauth[oauthType].getStatus();
    setAuthStatuses((current) => ({
      ...current,
      [item.provider]: authStateFromStatus(status),
    }));
  };

  const authorizeProvider = async (item: WorkflowProviderSpec) => {
    const oauthType = providerOAuthType(item);
    if (!oauthType) return;
    onProviderChange(item.provider);
    setAuthActionProviderId(item.provider);
    try {
      await openOAuthPopup(oauthType);
      await refreshProviderAuth(item);
    } catch {
      setAuthStatuses((current) => ({
        ...current,
        [item.provider]: {
          loading: false,
          connected: false,
          label: 'Authorization failed',
          error: true,
        },
      }));
    } finally {
      setAuthActionProviderId(null);
    }
  };

  if (!provider) return null;
  const selectedAuthState = authStatuses[provider.provider];

  return (
    <DialogRoot open onClose={onClose}>
      <DialogSurface width={step === 'configure' ? 860 : 820} ariaLabel="New sync">
        <DialogHeader
          title={step === 'source' ? 'Add sync' : `Configure ${provider.display_name}`}
          description={step === 'source'
            ? 'Choose an information source and confirm its authorization status.'
            : 'Choose what to import, where it lands, and how it runs.'}
          onClose={onClose}
        />
        <DialogBody style={{ padding: '12px 20px 20px' }}>
          <div className={styles.dialogBodyStack}>
            {model.feedback ? (
              <div className={model.feedback.type === 'error' ? `${sharedStyles.feedback} ${sharedStyles.feedbackError}` : `${sharedStyles.feedback} ${sharedStyles.feedbackSuccess}`}>
                {model.feedback.text}
              </div>
            ) : null}
            {step === 'source' ? (
              <>
                <div className={styles.sourcePickerGrid}>
                  {model.visibleProviders.map((item) => {
                    const authState = authStatuses[item.provider];
                    const selected = item.provider === provider.provider;
                    const needsAuth = providerUsesOAuth(item) && Boolean(providerOAuthType(item));
                    return (
                      <button
                        key={item.provider}
                        type="button"
                        className={selected ? `${styles.sourcePickerCard} ${styles.sourcePickerCardSelected}` : styles.sourcePickerCard}
                        onClick={() => onProviderChange(item.provider)}
                      >
                        <span className={styles.sourcePickerIcon}>
                          <ProviderMark provider={item} />
                        </span>
                        <span className={styles.sourcePickerContent}>
                          <span className={styles.sourcePickerTitle}>{item.display_name}</span>
                          <StatusIndicator
                            className={styles.authStatus}
                            status={authStatusIndicator(authState)}
                            label={authState?.loading ? 'Checking authorization' : authState?.label ?? 'Not authorized'}
                          />
                        </span>
                        {needsAuth ? (
                          <span
                            role="button"
                            tabIndex={0}
                            className={styles.sourcePickerAuthButton}
                            aria-disabled={authActionProviderId === item.provider}
                            onClick={(event) => {
                              event.stopPropagation();
                              if (authActionProviderId !== item.provider) {
                                void authorizeProvider(item);
                              }
                            }}
                            onKeyDown={(event) => {
                              if (event.key === 'Enter' || event.key === ' ') {
                                event.preventDefault();
                                event.stopPropagation();
                                if (authActionProviderId !== item.provider) {
                                  void authorizeProvider(item);
                                }
                              }
                            }}
                          >
                            {authActionProviderId === item.provider ? 'Authorizing' : authState?.connected ? 'Reconnect' : 'Authorize'}
                          </span>
                        ) : null}
                      </button>
                    );
                  })}
                </div>
                <div className={styles.sourceStepFooter}>
                  <span className={selectedAuthState?.connected ? sharedStyles.createHintReady : sharedStyles.createHintMuted}>
                    {selectedAuthState?.loading ? 'Checking authorization' : selectedAuthState?.label ?? 'Choose a source'}
                  </span>
                  <div className={sharedStyles.newWorkflowActions}>
                    <ActionButton variant="secondary" size="sm" onClick={onClose}>
                      Cancel
                    </ActionButton>
                    <ActionButton
                      variant="primary"
                      size="sm"
                      trailingIcon={<ChevronRight size={15} />}
                      disabled={providerUsesOAuth(provider) && !selectedAuthState?.connected}
                      onClick={() => setStep('configure')}
                    >
                      Continue
                    </ActionButton>
                  </div>
                </div>
              </>
            ) : (
              <>
                <NewSyncWorkflowMap
                  provider={provider}
                  triggerText={triggerText}
                  model={model}
                  actions={actions}
                />
                <div className={sharedStyles.workflowPanelFooter}>
                  <span className={createDisabled ? sharedStyles.createHintMuted : sharedStyles.createHintReady}>{createHint}</span>
                  <div className={sharedStyles.newWorkflowActions}>
                    <ActionButton variant="secondary" size="sm" onClick={() => setStep('source')}>
                      Back
                    </ActionButton>
                    {model.usesOAuth && model.canAuthorize ? (
                      <ActionButton
                        variant="secondary"
                        size="sm"
                        leadingIcon={<ExternalLink size={15} />}
                        onClick={() => void actions.authorize()}
                        loading={model.authBusy}
                      >
                        Authorize
                      </ActionButton>
                    ) : null}
                    <ActionButton
                      variant="primary"
                      size="sm"
                      leadingIcon={<Check size={15} />}
                      onClick={() => void actions.create()}
                      loading={model.creating}
                      disabled={createDisabled}
                    >
                      Create sync
                    </ActionButton>
                  </div>
                </div>
              </>
            )}
          </div>
        </DialogBody>
      </DialogSurface>
    </DialogRoot>
  );
}

function NewSyncWorkflowMap({
  provider,
  triggerText,
  model,
  actions,
}: WorkflowShellProps & {
  provider: WorkflowProviderSpec;
  triggerText: string;
}) {
  const needsResourcePicker = providerUsesOAuth(provider) && provider.provider !== 'url';
  const destinationSegments = normalizeProjectPathSegments(model.targetPath);
  const destinationTitle = destinationSegments.length > 0
    ? `/${destinationSegments.join('/')}`
    : 'Project folder';

  return (
    <div className={sharedStyles.syncWorkflowMap}>
      <section className={sharedStyles.syncWorkflowNode}>
        <div className={sharedStyles.syncWorkflowNodeHeader}>
          <span className={sharedStyles.syncWorkflowNodeIcon}>
            <ProviderMark provider={provider} />
          </span>
          <span className={sharedStyles.syncWorkflowNodeText}>
            <span className={sharedStyles.syncWorkflowNodeTitle}>{provider.display_name}</span>
          </span>
        </div>
        <div className={sharedStyles.syncWorkflowNodeBody}>
          <div className={sharedStyles.settingsGrid}>
            {needsResourcePicker ? (
              <ProviderResourcePicker
                provider={provider}
                selectedResources={model.selectedResources}
                onChange={actions.setSelectedResources}
              />
            ) : null}
            {model.configFields.length === 0 ? (
              needsResourcePicker ? null : (
                <div className={sharedStyles.settingsEmpty}>No source settings required.</div>
              )
            ) : model.configFields.map((field) => (
              <Field
                key={field.key}
                label={`${field.label}${field.required ? ' *' : ''}`}
                hint={field.hint}
                error={model.configErrors[field.key]}
              >
                <ConfigFieldInput
                  field={field}
                  value={model.configValues[field.key] ?? ''}
                  invalid={Boolean(model.configErrors[field.key])}
                  onChange={(value) => actions.setConfigValues((current) => ({ ...current, [field.key]: value }))}
                />
              </Field>
            ))}
          </div>
        </div>
      </section>

      <div className={sharedStyles.syncWorkflowConnector} aria-label="Sync trigger">
        <span className={sharedStyles.syncWorkflowLine} />
        <WorkflowTriggerControl triggerText={triggerText} model={model} actions={actions} />
        <span className={sharedStyles.syncWorkflowLine} />
        <span className={sharedStyles.syncWorkflowArrow}>
          <ArrowRight size={16} />
        </span>
      </div>

      <section className={sharedStyles.syncWorkflowNode}>
        <div className={sharedStyles.syncWorkflowNodeHeader}>
          <span className={sharedStyles.syncWorkflowNodeIcon}>
            <img src="/icons/folder.svg" alt="" />
          </span>
          <span className={sharedStyles.syncWorkflowNodeText}>
            <span className={sharedStyles.syncWorkflowNodeTitle}>{destinationTitle}</span>
          </span>
        </div>
        <div className={sharedStyles.syncWorkflowNodeBody}>
          <ProjectFolderSelect
            projectId={model.projectId}
            value={model.targetPath}
            invalid={!model.targetPath.trim()}
            onChange={actions.setTargetPath}
            missingLabel="new folder"
            showSelection={false}
          />
        </div>
      </section>
    </div>
  );
}

function ProviderResourcePicker({
  provider,
  selectedResources,
  onChange,
}: {
  provider: WorkflowProviderSpec;
  selectedResources: WorkflowSourceResource[];
  onChange: (resources: WorkflowSourceResource[]) => void;
}) {
  const [query, setQuery] = useState('');
  const [resources, setResources] = useState<WorkflowSourceResource[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const multiple = provider.provider === 'google_calendar';

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    void listWorkflowProviderResources(provider.provider, { q: query.trim() })
      .then((result) => {
        if (cancelled) return;
        setResources(result.resources ?? []);
      })
      .catch((loadError) => {
        if (cancelled) return;
        setError(loadError instanceof Error ? loadError.message : 'Could not load resources');
        setResources([]);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [provider.provider, query]);

  useEffect(() => {
    if (provider.provider === 'gmail' && selectedResources.length === 0 && resources.length === 1) {
      onChange([resources[0]]);
    }
  }, [onChange, provider.provider, resources, selectedResources.length]);

  const selectedIds = new Set(selectedResources.map((resource) => resource.id));

  const toggleResource = (resource: WorkflowSourceResource) => {
    if (!multiple) {
      onChange([resource]);
      return;
    }
    if (selectedIds.has(resource.id)) {
      onChange(selectedResources.filter((item) => item.id !== resource.id));
      return;
    }
    onChange([...selectedResources, resource]);
  };

  return (
    <div className={styles.resourcePicker}>
      <input
        className={styles.resourceSearch}
        value={query}
        onChange={(event) => setQuery(event.target.value)}
        placeholder="Search"
      />
      <div className={styles.resourceList}>
        {loading ? (
          <div className={styles.resourceState}>
            <InlineLoading label="Loading" size="sm" />
          </div>
        ) : error ? (
          <div className={styles.resourceState}>{error}</div>
        ) : resources.length === 0 ? (
          <div className={styles.resourceState}>No resources</div>
        ) : resources.map((resource) => {
          const selected = selectedIds.has(resource.id);
          return (
            <button
              key={resource.id}
              type="button"
              className={selected ? `${styles.resourceRow} ${styles.resourceRowSelected}` : styles.resourceRow}
              onClick={() => toggleResource(resource)}
            >
              <span className={styles.resourceRowIcon}>
                <ProviderMark provider={provider} icon={resource.icon} />
              </span>
              <span className={styles.resourceRowText}>
                <span className={styles.resourceRowTitle}>{resource.name}</span>
                {resource.subtitle ? (
                  <span className={styles.resourceRowMeta}>{resource.subtitle}</span>
                ) : null}
              </span>
              {selected ? <Check size={14} /> : null}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function authStateFromStatus(status: OAuthStatusResponse): ProviderAuthState {
  if (status.connected) {
    return {
      loading: false,
      connected: true,
      label: 'Authorized',
    };
  }
  return {
    loading: false,
    connected: false,
    label: 'Not authorized',
  };
}

function authStatusIndicator(authState?: ProviderAuthState): StatusDotStatus {
  if (authState?.loading) return 'inactive';
  if (authState?.error) return 'error';
  if (authState?.connected) return 'connected';
  return 'warning';
}

function workflowConnectionStatus(status: string): StatusDotStatus {
  if (status === 'active') return 'active';
  if (status === 'syncing') return 'syncing';
  if (status === 'paused') return 'paused';
  if (status === 'error' || status === 'failed') return 'failed';
  return 'inactive';
}

function providerForId(
  model: WorkflowShellProps['model'],
  providerId: string,
): WorkflowProviderSpec {
  return model.providersById.get(providerId) ?? {
    provider: providerId,
    display_name: providerName(undefined, providerId),
    description: null,
    auth: 'none',
    creation_mode: 'direct',
    category: 'datasource',
    icon: null,
  };
}

function groupConnectionsByProvider(
  model: WorkflowShellProps['model'],
  connections: WorkflowConnection[],
): Array<{ provider: WorkflowProviderSpec; connections: WorkflowConnection[] }> {
  const groups = new Map<string, WorkflowConnection[]>();
  connections.forEach((connection) => {
    const group = groups.get(connection.provider) ?? [];
    group.push(connection);
    groups.set(connection.provider, group);
  });
  return Array.from(groups.entries()).map(([providerId, providerConnections]) => ({
    provider: providerForId(model, providerId),
    connections: providerConnections,
  }));
}

function normalizeProjectPathSegments(path: string | null | undefined): string[] {
  const normalized = (path ?? '').trim().replace(/^\/+|\/+$/g, '').replace(/\/+/g, '/');
  return normalized ? normalized.split('/').filter(Boolean) : [];
}

function sourceConfigLabel(
  provider: WorkflowProviderSpec,
  connection: WorkflowConnection,
): string {
  const source = connection.config?.source;
  if (source && typeof source === 'object') {
    const resourceName = (source as { resource_name?: unknown }).resource_name;
    if (typeof resourceName === 'string' && resourceName.trim()) return resourceName.trim();
    const resourceUrl = (source as { resource_url?: unknown }).resource_url;
    if (typeof resourceUrl === 'string' && resourceUrl.trim()) return resourceUrl.trim();
  }
  return provider.display_name;
}
