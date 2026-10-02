import type { SaasType } from '@/lib/oauthApi';
import type {
  SynchronizeBindingCreate as CreateWorkflowRequest,
  SynchronizeConfigField as WorkflowConfigField,
  SynchronizeBinding as WorkflowConnection,
  SynchronizeFailedRun as WorkflowFailedRunRow,
  SynchronizeMaterializationSchema as WorkflowMaterializationSchema,
  SynchronizeProviderSpec as WorkflowProviderSpec,
  SynchronizeSourceResource as WorkflowSourceResource,
  SynchronizeStatusItem as WorkflowStatusItem,
} from '@/lib/synchronizeApi';

export type BusyAction = 'refresh' | 'pause' | 'resume' | 'delete' | null;
export type DetailMode = 'new' | 'detail';
export type TriggerMode = 'manual' | 'scheduled';
export type WorkflowFeedback = { type: 'error' | 'success'; text: string } | null;

export interface TriggerDraft {
  mode: TriggerMode;
  schedule: string;
  timezone: string;
}

export interface RecentRun {
  id: string;
  time: string;
  result: string;
  items: string;
  target: string;
  duration: string;
}

export interface WorkflowSourceConfig {
  provider?: string;
  resource_type?: string;
  resource_id?: string;
  resource_name?: string;
  resource_url?: string;
  account_label?: string;
  metadata?: Record<string, unknown>;
}

const OAUTH_MAP: Partial<Record<string, SaasType>> = {
  github: 'github',
  gmail: 'gmail',
  google_calendar: 'google_calendar',
  calendar: 'google_calendar',
  google_docs: 'google_docs',
  docs: 'google_docs',
  google_sheets: 'google_sheets',
  sheets: 'google_sheets',
  google_drive: 'google_drive',
  drive: 'google_drive',
  google_search_console: 'google_search_console',
};

const INTERNAL_CONFIG_KEYS = new Set([
  'access_key',
  'authority',
  'connection_id',
  'credentials_ref',
  'credential_ref',
  'direction',
  'external_resource_id',
  'external_resource',
  'last_sync_commit_id',
  'last_synchronize_commit_id',
  'synchronize_binding_id',
  'name',
  'oauth_user_id',
  'provider',
  'resource_id',
  'status',
  'sync_behavior',
  'target_folder_path',
  'target_output',
  'target_path',
  'user_id',
  'write_behavior',
]);

export function visibleWorkflowProviders(providers: WorkflowProviderSpec[]): WorkflowProviderSpec[] {
  return providers.filter((provider) => provider.category === 'datasource');
}

export function providerName(provider: WorkflowProviderSpec | undefined, fallback: string): string {
  return provider?.display_name || fallback.replaceAll('_', ' ');
}

export function workflowName(
  connection: WorkflowConnection | undefined,
  provider: WorkflowProviderSpec | undefined,
): string {
  if (!connection) return 'New sync';
  const sourceConfig = sourceConfigFrom(connection.config);
  const source = sourceConfig.resource_name || providerName(provider, connection.provider);
  const target = connection.path ? connection.path.split('/').filter(Boolean).pop() : 'Project root';
  if (target && target.toLowerCase() === source.toLowerCase()) return source;
  return `${source} to ${target || 'Project root'}`;
}

export function targetDefault(provider: WorkflowProviderSpec | undefined): string {
  const name = provider?.display_name || provider?.provider || 'Workflow';
  return name.replace(/[<>:"|?*]/g, '-');
}

export function normalizeDefaultValue(field: WorkflowConfigField): string {
  if (field.default === null || field.default === undefined) return '';
  return String(field.default);
}

export function defaultConfigForProvider(provider: WorkflowProviderSpec | undefined): Record<string, string> {
  const values: Record<string, string> = {};
  for (const field of provider?.config_fields ?? []) {
    values[field.key] = normalizeDefaultValue(field);
  }
  return values;
}

export function userConfigFieldsFor(provider: WorkflowProviderSpec | undefined): WorkflowConfigField[] {
  return (provider?.config_fields ?? []).filter((field) => !INTERNAL_CONFIG_KEYS.has(field.key));
}

export function configDraftFrom(
  fields: WorkflowConfigField[],
  config: Record<string, unknown> | undefined,
): Record<string, string> {
  const options = optionsFromConfig(config);
  const source = sourceConfigFrom(config);
  const draft: Record<string, string> = {};
  for (const field of fields) {
    const value = field.key === 'resource_url' ? source.resource_url : options[field.key];
    if (value !== null && value !== undefined) {
      draft[field.key] = String(value);
    } else {
      draft[field.key] = normalizeDefaultValue(field);
    }
  }
  return draft;
}

export function validateConfigDraft(
  fields: WorkflowConfigField[],
  draft: Record<string, string>,
): Record<string, string> {
  const errors: Record<string, string> = {};
  for (const field of fields) {
    const value = draft[field.key] ?? '';
    const trimmed = value.trim();
    if (field.required && !trimmed) {
      errors[field.key] = 'Required';
      continue;
    }
    if (field.type === 'number' && trimmed && !Number.isFinite(Number(trimmed))) {
      errors[field.key] = 'Enter a valid number';
    }
    if (field.type === 'url' && trimmed) {
      try {
        new URL(trimmed);
      } catch {
        errors[field.key] = 'Enter a valid URL';
      }
    }
  }
  return errors;
}

export function hasConfigErrors(errors: Record<string, string>): boolean {
  return Object.keys(errors).length > 0;
}

export function configPatchFrom(
  fields: WorkflowConfigField[],
  draft: Record<string, string>,
): Record<string, unknown> {
  const options: Record<string, unknown> = {};
  let sourcePatch: Record<string, unknown> | null = null;
  for (const field of fields) {
    const value = draft[field.key] ?? '';
    const trimmed = value.trim();
    if (!trimmed) continue;
    if (field.key === 'resource_url') {
      sourcePatch = {
        resource_id: trimmed,
        resource_name: trimmed,
        resource_url: trimmed,
      };
      continue;
    }
    if (field.type === 'number') {
      const parsed = Number(trimmed);
      if (Number.isFinite(parsed)) options[field.key] = parsed;
    } else {
      options[field.key] = value;
    }
  }
  return {
    ...(sourcePatch ? { source: sourcePatch } : {}),
    options,
  };
}

export function cleanOptionsForCreate(
  provider: WorkflowProviderSpec | undefined,
  values: Record<string, string>,
): Record<string, unknown> {
  const fieldsByKey = new Map((provider?.config_fields ?? []).map((field) => [field.key, field]));
  const config: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(values)) {
    const trimmed = value.trim();
    if (!trimmed) continue;
    if (key === 'resource_url') continue;
    const field = fieldsByKey.get(key);
    if (field?.type === 'number') {
      const parsed = Number(trimmed);
      if (Number.isFinite(parsed)) config[key] = parsed;
    } else {
      config[key] = value;
    }
  }
  return config;
}

export function sourceConfigFrom(config: Record<string, unknown> | undefined): WorkflowSourceConfig {
  const source = config?.source;
  return source && typeof source === 'object' ? source as WorkflowSourceConfig : {};
}

export function optionsFromConfig(config: Record<string, unknown> | undefined): Record<string, unknown> {
  const options = config?.options;
  return options && typeof options === 'object' ? options as Record<string, unknown> : {};
}

export function providerNeedsResourceSelection(provider?: WorkflowProviderSpec): boolean {
  if (!provider || provider.provider === 'url') return false;
  return providerUsesOAuth(provider);
}

export function hasValidSourceSelection(
  provider: WorkflowProviderSpec | undefined,
  selectedResources: WorkflowSourceResource[],
): boolean {
  if (!providerNeedsResourceSelection(provider)) return true;
  return selectedResources.some((resource) => resource.id.trim().length > 0);
}

function accountLabelFromResource(resource: WorkflowSourceResource): string | undefined {
  const value = resource.metadata?.account_label ?? resource.metadata?.owner;
  return typeof value === 'string' && value.trim() ? value : undefined;
}

function sourceFromResource(
  provider: WorkflowProviderSpec,
  resource: WorkflowSourceResource,
): WorkflowSourceConfig {
  return {
    provider: provider.provider,
    resource_type: resource.type,
    resource_id: resource.id,
    resource_name: resource.name,
    resource_url: resource.url ?? undefined,
    account_label: accountLabelFromResource(resource),
    metadata: resource.metadata ?? {},
  };
}

export function buildStructuredConfigForCreate({
  provider,
  configValues,
  selectedResources,
}: {
  provider: WorkflowProviderSpec;
  configValues: Record<string, string>;
  selectedResources: WorkflowSourceResource[];
}): Record<string, unknown> {
  const options = cleanOptionsForCreate(provider, configValues);

  if (provider.provider === 'url') {
    const resourceUrl = (configValues.resource_url ?? '').trim();
    return {
      source: {
        provider: provider.provider,
        resource_type: 'web_page',
        resource_id: resourceUrl,
        resource_name: resourceUrl,
        resource_url: resourceUrl,
      },
      options,
    };
  }

  if (provider.provider === 'google_calendar') {
    const selected = selectedResources.filter((resource) => resource.id.trim().length > 0);
    const calendarIds = selected.map((resource) => resource.id);
    return {
      source: {
        provider: provider.provider,
        resource_type: 'calendar_set',
        resource_id: calendarIds.join(','),
        resource_name: selected.map((resource) => resource.name).join(', '),
        metadata: { calendar_ids: calendarIds },
      },
      options: {
        ...options,
        calendar_ids: calendarIds,
      },
    };
  }

  const resource = selectedResources[0];
  return {
    source: resource ? sourceFromResource(provider, resource) : {
      provider: provider.provider,
      resource_type: 'unknown',
      resource_id: '',
      resource_name: provider.display_name,
    },
    options,
  };
}

export function buildTriggerPayload(trigger: TriggerDraft) {
  if (trigger.mode === 'scheduled') {
    return {
      sync_mode: 'scheduled' as const,
      trigger: {
        type: 'scheduled',
        schedule: trigger.schedule.trim(),
        timezone: trigger.timezone.trim(),
      },
    };
  }
  return {
    sync_mode: 'manual' as const,
    trigger: { type: 'manual' },
  };
}

export function triggerValidationError(trigger: TriggerDraft): string | null {
  if (trigger.mode !== 'scheduled') return null;
  if (!trigger.schedule.trim()) return 'Schedule is required.';
  if (!trigger.timezone.trim()) return 'Timezone is required.';
  return null;
}

export function buildCreateWorkflowRequest({
  projectId,
  provider,
  configValues,
  selectedResources,
  targetPath,
  trigger,
}: {
  projectId: string;
  provider: WorkflowProviderSpec;
  configValues: Record<string, string>;
  selectedResources: WorkflowSourceResource[];
  targetPath: string;
  trigger: TriggerDraft;
}): CreateWorkflowRequest {
  const triggerPayload = buildTriggerPayload(trigger);
  return {
    project_id: projectId,
    provider: provider.provider,
    config: buildStructuredConfigForCreate({ provider, configValues, selectedResources }),
    target_path: targetPath.trim(),
    direction: provider.supported_directions?.[0] ?? 'inbound',
    sync_mode: triggerPayload.sync_mode,
    trigger: triggerPayload.trigger,
  };
}

type MaterializationRef = {
  id?: unknown;
  version?: unknown;
};

function materializationRefFromConfig(
  config: Record<string, unknown> | undefined,
): MaterializationRef | null {
  const value = config?.materialization_schema;
  if (!value || typeof value !== 'object') return null;
  return value as MaterializationRef;
}

export function materializationSchemaFor(
  provider: WorkflowProviderSpec | undefined,
  config?: Record<string, unknown>,
): WorkflowMaterializationSchema | undefined {
  const ref = materializationRefFromConfig(config);
  const schemas = provider?.materialization_schemas ?? [];
  if (ref?.id && ref.version !== undefined) {
    const version = Number(ref.version);
    const matched = schemas.find((schema) => (
      schema.id === ref.id && schema.version === version
    ));
    if (matched) return matched;
  }
  return provider?.materialization_schema ?? schemas.find((schema) => schema.latest) ?? schemas[0];
}

export function materializationPreviewPaths(
  provider: WorkflowProviderSpec | undefined,
  config?: Record<string, unknown>,
): string[] {
  return materializationSchemaFor(provider, config)?.preview_paths ?? [
    '_meta/source.json',
    'index.json',
  ];
}

export function providerOAuthKey(provider?: WorkflowProviderSpec): string | undefined {
  return provider?.oauth_ui_type || provider?.oauth_type || provider?.provider;
}

export function providerOAuthType(provider?: WorkflowProviderSpec): SaasType | undefined {
  const key = providerOAuthKey(provider);
  return key ? OAUTH_MAP[key] : undefined;
}

export function providerUsesOAuth(provider?: WorkflowProviderSpec): boolean {
  return provider?.auth === 'oauth' || provider?.auth === 'optional_oauth';
}

export function triggerFromStatus(status?: WorkflowStatusItem): TriggerDraft {
  const trigger = status?.trigger ?? {};
  const type = typeof trigger.type === 'string' ? trigger.type : 'manual';
  const schedule = typeof trigger.schedule === 'string' ? trigger.schedule : '0 9 * * *';
  const timezone = typeof trigger.timezone === 'string' ? trigger.timezone : 'UTC';
  return {
    mode: type === 'scheduled' ? 'scheduled' : 'manual',
    schedule,
    timezone,
  };
}

export function triggerLabel(mode: TriggerMode, schedule: string): string {
  if (mode === 'scheduled') return schedule.trim() || 'Scheduled';
  return 'Manual';
}

export function formatDate(value?: string | null): string {
  if (!value) return 'Never';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return 'Never';
  return new Intl.DateTimeFormat(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date);
}

export function dataPath(projectId: string, path?: string | null): string {
  if (!path) return `/projects/${projectId}/data`;
  const encoded = path.split('/').filter(Boolean).map(encodeURIComponent).join('/');
  return `/projects/${projectId}/data/${encoded}`;
}

export function labelize(value: string): string {
  return value.replaceAll('_', ' ');
}

export function buildRecentRuns(
  selectedConnectionId: string | null,
  status: string | undefined,
  path: string | null | undefined,
  lastSyncedAt: string | null | undefined,
  failedRuns: WorkflowFailedRunRow[],
): RecentRun[] {
  if (!selectedConnectionId) return [];
  const target = path || 'Project root';
  const rows: RecentRun[] = [];
  if (status === 'syncing') {
    rows.push({
      id: `${selectedConnectionId}-running`,
      time: 'Now',
      result: 'Running',
      items: 'Sync in progress',
      target,
      duration: '-',
    });
  }
  if (lastSyncedAt) {
    rows.push({
      id: `${selectedConnectionId}-success`,
      time: formatDate(lastSyncedAt),
      result: 'Success',
      items: 'Updated',
      target,
      duration: '-',
    });
  }
  for (const run of failedRuns.filter((item) => item.synchronize_binding_id === selectedConnectionId)) {
    rows.push({
      id: run.id,
      time: formatDate(run.finished_at || run.started_at),
      result: 'Failed',
      items: run.result_summary || run.error || 'Failed run',
      target: run.target_path || target,
      duration: run.duration_ms ? `${Math.round(run.duration_ms / 1000)}s` : '-',
    });
  }
  return rows.slice(0, 5);
}
