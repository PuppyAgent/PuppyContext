'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import useSWR from 'swr';
import { openOAuthPopup } from '@/lib/oauthApi';
import {
  createSynchronizeBinding as createWorkflow,
  deleteSynchronizeBinding as deleteWorkflowConnection,
  listSynchronizeProviders as getWorkflowProviderSpecs,
  getSynchronizeStatus as getWorkflowStatus,
  listSynchronizeBindings as listWorkflowConnections,
  listFailedSynchronizeRuns as listWorkflowFailedRuns,
  pauseSynchronizeBinding as pauseWorkflowConnection,
  refreshSynchronizeBinding as refreshWorkflowConnection,
  resumeSynchronizeBinding as resumeWorkflowConnection,
  updateSynchronizeTrigger as updateWorkflowTrigger,
  type SynchronizeBinding as WorkflowConnection,
  type SynchronizeSourceResource as WorkflowSourceResource,
} from '@/lib/synchronizeApi';
import {
  buildCreateWorkflowRequest,
  buildRecentRuns,
  buildTriggerPayload,
  defaultConfigForProvider,
  hasValidSourceSelection,
  providerName,
  providerOAuthType,
  providerUsesOAuth,
  targetDefault,
  triggerFromStatus,
  triggerValidationError,
  userConfigFieldsFor,
  validateConfigDraft,
  visibleWorkflowProviders,
  workflowName,
  type BusyAction,
  type DetailMode,
  type TriggerDraft,
  type TriggerMode,
  type WorkflowFeedback,
} from '../lib/workflowModel';

export type WorkflowViewModel = ReturnType<typeof useWorkflowController>['model'];
export type WorkflowActions = ReturnType<typeof useWorkflowController>['actions'];

export function useWorkflowController(projectId: string) {
  const [mode, setMode] = useState<DetailMode>('detail');
  const [selectedConnectionId, setSelectedConnectionId] = useState<string | null>(null);
  const [selectedProviderId, setSelectedProviderId] = useState<string>('');
  const [selectedResources, setSelectedResources] = useState<WorkflowSourceResource[]>([]);
  const [configValues, setConfigValues] = useState<Record<string, string>>({});
  const [targetPath, setTargetPath] = useState('');
  const [triggerMode, setTriggerMode] = useState<TriggerMode>('manual');
  const [schedule, setSchedule] = useState('0 9 * * *');
  const [timezone, setTimezone] = useState(
    Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC',
  );
  const [creating, setCreating] = useState(false);
  const [authBusy, setAuthBusy] = useState(false);
  const [triggerOpen, setTriggerOpen] = useState(false);
  const [savingTrigger, setSavingTrigger] = useState(false);
  const [busyById, setBusyById] = useState<Record<string, BusyAction>>({});
  const [feedback, setFeedback] = useState<WorkflowFeedback>(null);
  const [deleteTarget, setDeleteTarget] = useState<WorkflowConnection | null>(null);

  const {
    data: providers = [],
    isLoading: providersLoading,
  } = useSWR(['workflow-providers'], getWorkflowProviderSpecs, {
    revalidateOnFocus: false,
  });
  const {
    data: connections = [],
    isLoading: connectionsLoading,
    mutate: mutateConnections,
  } = useSWR(['synchronize-bindings', projectId], () => listWorkflowConnections(projectId), {
    revalidateOnFocus: true,
  });
  const {
    data: workflowStatus,
    mutate: mutateStatus,
  } = useSWR(['synchronize-status', projectId], () => getWorkflowStatus(projectId), {
    revalidateOnFocus: true,
  });
  const {
    data: failedRuns = [],
    mutate: mutateFailedRuns,
  } = useSWR(['synchronize-failed-runs', projectId], () => listWorkflowFailedRuns(projectId, 8), {
    revalidateOnFocus: true,
  });

  const visibleProviders = useMemo(() => visibleWorkflowProviders(providers), [providers]);
  const providersById = useMemo(
    () => new Map(visibleProviders.map((provider) => [provider.provider, provider])),
    [visibleProviders],
  );
  const statusById = useMemo(
    () => new Map((workflowStatus?.bindings ?? []).map((item) => [item.id, item])),
    [workflowStatus?.bindings],
  );
  const selectedProvider = useMemo(
    () => visibleProviders.find((provider) => provider.provider === selectedProviderId),
    [selectedProviderId, visibleProviders],
  );
  const selectedConnection = useMemo(
    () => connections.find((connection) => connection.id === selectedConnectionId),
    [connections, selectedConnectionId],
  );
  const selectedStatus = selectedConnection ? statusById.get(selectedConnection.id) : undefined;
  const selectedConnectionProvider = selectedConnection
    ? providersById.get(selectedConnection.provider)
    : undefined;

  const createConfigFields = useMemo(
    () => userConfigFieldsFor(selectedProvider),
    [selectedProvider],
  );
  const createConfigErrors = useMemo(
    () => validateConfigDraft(createConfigFields, configValues),
    [configValues, createConfigFields],
  );
  const missingRequired = useMemo(
    () => Object.keys(createConfigErrors).length > 0,
    [createConfigErrors],
  );
  const missingSource = useMemo(
    () => !hasValidSourceSelection(selectedProvider, selectedResources),
    [selectedProvider, selectedResources],
  );
  const triggerDraft: TriggerDraft = useMemo(
    () => ({ mode: triggerMode, schedule, timezone }),
    [schedule, timezone, triggerMode],
  );
  const triggerError = triggerValidationError(triggerDraft);

  useEffect(() => {
    if (!selectedProviderId && visibleProviders.length) {
      setSelectedProviderId(visibleProviders[0].provider);
    }
  }, [selectedProviderId, visibleProviders]);

  useEffect(() => {
    if (!selectedProvider) return;
    setConfigValues(defaultConfigForProvider(selectedProvider));
    setSelectedResources([]);
    setTargetPath(targetDefault(selectedProvider));
    setTriggerMode(selectedProvider.default_sync_mode === 'scheduled' ? 'scheduled' : 'manual');
  }, [selectedProvider]);

  useEffect(() => {
    if (mode === 'new') return;
    if (!connections.length) {
      setSelectedConnectionId(null);
      return;
    }
    const stillExists = selectedConnectionId
      ? connections.some((connection) => connection.id === selectedConnectionId)
      : false;
    if (!stillExists) {
      setSelectedConnectionId(connections[0].id);
    }
  }, [connections, mode, selectedConnectionId]);

  const refreshAll = useCallback(async () => {
    await Promise.all([
      mutateConnections(),
      mutateStatus(),
      mutateFailedRuns(),
    ]);
  }, [mutateConnections, mutateFailedRuns, mutateStatus]);

  const startNew = useCallback(() => {
    setMode('new');
    setTriggerOpen(false);
    setFeedback(null);
  }, []);

  const cancelNew = useCallback(() => {
    setMode('detail');
    setTriggerOpen(false);
    setFeedback(null);
    if (!selectedConnectionId && connections.length > 0) {
      setSelectedConnectionId(connections[0].id);
    }
  }, [connections, selectedConnectionId]);

  const selectConnection = useCallback((id: string) => {
    setMode('detail');
    setSelectedConnectionId(id);
    setTriggerOpen(false);
    setFeedback(null);
  }, []);

  const authorize = useCallback(async () => {
    const saasType = providerOAuthType(selectedProvider);
    if (!saasType) return;
    setAuthBusy(true);
    setFeedback(null);
    try {
      await openOAuthPopup(saasType);
      setFeedback({ type: 'success', text: 'Authorization window closed.' });
    } catch (error) {
      setFeedback({
        type: 'error',
        text: error instanceof Error ? error.message : 'Authorization failed.',
      });
    } finally {
      setAuthBusy(false);
    }
  }, [selectedProvider]);

  const create = useCallback(async () => {
    if (!selectedProvider || missingRequired || missingSource || !targetPath.trim() || triggerError) return;
    setCreating(true);
    setFeedback(null);
    try {
      const result = await createWorkflow(buildCreateWorkflowRequest({
        projectId,
        provider: selectedProvider,
        configValues,
        selectedResources,
        targetPath,
        trigger: triggerDraft,
      }));
      setFeedback({ type: 'success', text: 'Sync created.' });
      setMode('detail');
      setSelectedConnectionId(result.binding.id);
      await refreshAll();
    } catch (error) {
      setFeedback({
        type: 'error',
        text: error instanceof Error ? error.message : 'Failed to create sync.',
      });
    } finally {
      setCreating(false);
    }
  }, [
    configValues,
    missingRequired,
    missingSource,
    projectId,
    refreshAll,
    selectedProvider,
    selectedResources,
    targetPath,
    triggerDraft,
    triggerError,
  ]);

  const runAction = useCallback(async (
    connectionId: string,
    action: Exclude<BusyAction, null>,
  ) => {
    const connection = connections.find((item) => item.id === connectionId);
    if (action === 'delete') {
      setDeleteTarget(connection ?? null);
      return;
    }
    setBusyById((current) => ({ ...current, [connectionId]: action }));
    setFeedback(null);
    try {
      if (action === 'refresh') await refreshWorkflowConnection(connectionId);
      if (action === 'pause') await pauseWorkflowConnection(connectionId);
      if (action === 'resume') await resumeWorkflowConnection(connectionId);
      await refreshAll();
    } catch (error) {
      setFeedback({
        type: 'error',
        text: error instanceof Error ? error.message : `Failed to ${action}.`,
      });
    } finally {
      setBusyById((current) => ({ ...current, [connectionId]: null }));
    }
  }, [connections, refreshAll]);

  const confirmDelete = useCallback(async () => {
    if (!deleteTarget) return;
    const connectionId = deleteTarget.id;
    setBusyById((current) => ({ ...current, [connectionId]: 'delete' }));
    setFeedback(null);
    try {
      await deleteWorkflowConnection(connectionId);
      setSelectedConnectionId(null);
      setDeleteTarget(null);
      await refreshAll();
    } catch (error) {
      setFeedback({
        type: 'error',
        text: error instanceof Error ? error.message : 'Failed to delete integration.',
      });
    } finally {
      setBusyById((current) => ({ ...current, [connectionId]: null }));
    }
  }, [deleteTarget, refreshAll]);

  const openTriggerEditor = useCallback(() => {
    if (mode === 'detail') {
      const trigger = triggerFromStatus(selectedStatus);
      setTriggerMode(trigger.mode);
      setSchedule(trigger.schedule);
      setTimezone(trigger.timezone);
    }
    setTriggerOpen((current) => !current);
  }, [mode, selectedStatus]);

  const saveTrigger = useCallback(async () => {
    if (triggerError) return;
    if (mode === 'new') {
      setTriggerOpen(false);
      return;
    }
    if (!selectedConnection) return;
    setSavingTrigger(true);
    setFeedback(null);
    try {
      await updateWorkflowTrigger(selectedConnection.id, buildTriggerPayload(triggerDraft));
      setTriggerOpen(false);
      await refreshAll();
    } catch (error) {
      setFeedback({
        type: 'error',
        text: error instanceof Error ? error.message : 'Failed to update trigger.',
      });
    } finally {
      setSavingTrigger(false);
    }
  }, [mode, refreshAll, selectedConnection, triggerDraft, triggerError]);

  const detailProvider = mode === 'new' ? selectedProvider : selectedConnectionProvider;
  const detailTitle = mode === 'new'
    ? 'New sync'
    : workflowName(selectedConnection, selectedConnectionProvider);
  const detailStatus = mode === 'new' ? 'draft' : selectedConnection?.status || 'active';
  const detailTrigger = mode === 'new' ? triggerDraft : triggerFromStatus(selectedStatus);
  const selectedBusy = selectedConnection ? busyById[selectedConnection.id] ?? null : null;
  const paused = selectedConnection?.status === 'paused';
  const hasSelection = mode === 'new' || Boolean(selectedConnection);
  const recentRuns = useMemo(
    () => buildRecentRuns(
      selectedConnectionId,
      selectedConnection?.status,
      selectedConnection?.path,
      selectedStatus?.last_synced_at,
      failedRuns,
    ),
    [
      failedRuns,
      selectedConnection?.path,
      selectedConnection?.status,
      selectedConnectionId,
      selectedStatus?.last_synced_at,
    ],
  );

  return {
    model: {
      projectId,
      loading: providersLoading || connectionsLoading,
      mode,
      hasSelection,
      connections,
      connectionsLoading,
      providersById,
      statusById,
      selectedConnection,
      selectedStatus,
      selectedBusy,
      detailProvider,
      detailTitle,
      detailStatus,
      detailTrigger,
      paused,
      feedback,
      visibleProviders,
      selectedProvider,
      selectedProviderId,
      configValues,
      selectedResources,
      configFields: createConfigFields,
      configErrors: createConfigErrors,
      targetPath,
      triggerMode,
      schedule,
      timezone,
      triggerOpen,
      triggerError,
      savingTrigger,
      creating,
      authBusy,
      missingRequired,
      missingSource,
      usesOAuth: providerUsesOAuth(selectedProvider),
      canAuthorize: Boolean(providerOAuthType(selectedProvider)),
      recentRuns,
      deleteTarget,
      deleteLoading: Boolean(deleteTarget && busyById[deleteTarget.id] === 'delete'),
    },
    actions: {
      startNew,
      cancelNew,
      selectConnection,
      setSelectedProviderId,
      setSelectedResources,
      setConfigValues,
      setTargetPath,
      setTriggerMode,
      setSchedule,
      setTimezone,
      authorize,
      create,
      runAction,
      openTriggerEditor,
      closeTrigger: () => setTriggerOpen(false),
      saveTrigger,
      refreshAll,
      cancelDelete: () => setDeleteTarget(null),
      confirmDelete,
      providerName,
    },
  };
}
