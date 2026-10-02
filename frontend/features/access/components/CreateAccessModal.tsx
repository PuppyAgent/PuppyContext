'use client';

import { useMemo, useState } from 'react';
import type { CSSProperties, ReactNode } from 'react';
import { Plus } from 'lucide-react';
import { DialogBody, DialogFooter, DialogHeader, DialogRoot, DialogSurface } from '@/components/ui/Dialog';
import { TreeDisclosureMarker } from '@/components/ui/TreeDisclosureMarker';
import { ToggleSwitch } from '@/components/ui/ToggleSwitch';
import {
  createAccessSurface,
  createScope,
  deleteScope,
  enableTargetAccess,
  repositoryScopeView,
  repositoryViewKey,
  type AccessSurface,
  type AccessDirection,
  type RepositoryView,
} from '@/lib/repoApi';
import { createMcpEndpoint } from '@/lib/mcpEndpointsApi';
import { getAccessProviderLabel } from '@/lib/accessProviderRegistry';
import { T } from '@/features/access/lib/tokens';
import { ProviderIcon } from '@/features/access/components/icons';
import { FolderAccessTree } from '@/features/access/components/FolderAccessTree';

type OptionalProvider = 'mcp' | 'sandbox';
type AccessIntent = 'remote_workspace' | 'git_remote' | 'cli' | 'ai_agent';

const ACCESS_MODAL_TYPE = {
  body: 13,
  meta: 12,
  label: 11,
} as const;

const OPTIONAL_METHODS: Array<{
  readonly provider: OptionalProvider;
  readonly direction: AccessDirection;
  readonly description: string;
  readonly supported: boolean;
}> = [
  {
    provider: 'mcp',
    direction: 'inbound',
    description: 'External AI tools connect through MCP.',
    supported: true,
  },
  {
    provider: 'sandbox',
    direction: 'inbound',
    description: 'Run tools with this folder mounted.',
    supported: false,
  },
];

const INTENT_OPTIONS: Array<{
  readonly id: AccessIntent;
  readonly label: string;
  readonly provider: string;
  readonly preview: string;
  readonly chips: readonly string[];
}> = [
  {
    id: 'remote_workspace',
    label: 'Open editor',
    provider: 'sandbox',
    preview: 'Cursor opens a ready Git workspace',
    chips: ['Editor', 'Git ready', 'No clone'],
  },
  {
    id: 'git_remote',
    label: 'Clone repo',
    provider: 'git_remote',
    preview: 'git clone https://.../access.git',
    chips: ['Local files', 'Git flow'],
  },
  {
    id: 'cli',
    label: 'Use shell',
    provider: 'cli',
    preview: 'puppyone fs ls /company/sales',
    chips: ['No clone', 'Scriptable'],
  },
  {
    id: 'ai_agent',
    label: 'Connect AI agent',
    provider: 'mcp',
    preview: 'Agent calls approved file tools',
    chips: ['AI agent', 'Tool calls'],
  },
];

export function CreateAccessModal({
  projectId,
  existingScopes,
  connectorsByTarget,
  initialPath,
  onClose,
  onCreated,
}: {
  readonly projectId: string;
  readonly existingScopes: readonly RepositoryView[];
  readonly connectorsByTarget: ReadonlyMap<string, readonly AccessSurface[]>;
  readonly initialPath?: string | null;
  readonly onClose: () => void;
  readonly onCreated: (scope: RepositoryView) => Promise<void> | void;
}) {
  const normalizedInitialPath = normalizePath(initialPath ?? '');
  const initialSelectedPath = normalizedInitialPath === '' ? null : normalizedInitialPath;
  const [selectedPath, setSelectedPath] = useState<string | null>(initialSelectedPath);
  const [name, setName] = useState(
    initialSelectedPath === null ? '' : defaultScopeName(initialSelectedPath),
  );
  const [nameTouched, setNameTouched] = useState(initialSelectedPath !== null);
  const [optionalProviders, setOptionalProviders] = useState<ReadonlySet<OptionalProvider>>(() => new Set());
  const [intent, setIntent] = useState<AccessIntent>('remote_workspace');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const existingPathSet = useMemo(
    () => new Set(existingScopes.map((scope) => normalizePath(scope.path))),
    [existingScopes],
  );

  const normalizedSelected = selectedPath === null ? null : normalizePath(selectedPath);
  const selectedExistingScope = useMemo(
    () =>
      normalizedSelected === null
        ? null
        : existingScopes.find((scope) => normalizePath(scope.path) === normalizedSelected) ?? null,
    [existingScopes, normalizedSelected],
  );
  const existingProviders = useMemo(() => {
    if (!selectedExistingScope) return new Set<string>();
    return new Set(
      (connectorsByTarget.get(repositoryViewKey(selectedExistingScope)) ?? [])
        .map((connector) => connector.kind),
    );
  }, [connectorsByTarget, selectedExistingScope]);
  const optionalProvidersToCreate = useMemo(
    () => Array.from(optionalProviders).filter((provider) => {
      const method = OPTIONAL_METHODS.find((item) => item.provider === provider);
      return method?.supported === true && !existingProviders.has(provider);
    }),
    [existingProviders, optionalProviders],
  );
  const trimmedName = name.trim();
  const canCreate =
    !saving
    && normalizedSelected !== null
    && normalizedSelected !== ''
    && (selectedExistingScope !== null || trimmedName.length > 0);
  const selectedLabel = normalizedSelected === null ? 'Choose a path' : formatPath(normalizedSelected);
  const actionLabel = saving
    ? 'Saving...'
    : selectedExistingScope
      ? optionalProvidersToCreate.length > 0
        ? 'Update access'
        : 'Open access'
      : 'Create access';

  const selectPath = (path: string) => {
    const normalized = normalizePath(path);
    if (normalized === '') return;
    setSelectedPath(normalized);
    setError(null);
    if (!nameTouched) {
      setName(defaultScopeName(normalized));
    }
  };

  const toggleOptionalProvider = (provider: OptionalProvider, checked: boolean) => {
    setOptionalProviders((current) => {
      const next = new Set(current);
      if (checked) next.add(provider);
      else next.delete(provider);
      return next;
    });
  };

  const handleCreate = async () => {
    if (!canCreate || normalizedSelected === null) return;
    setSaving(true);
    setError(null);
    let createdScopeId: string | null = null;
    try {
      const scope = selectedExistingScope ?? repositoryScopeView(
        await createScope(projectId, {
          name: (name.trim() || defaultScopeName(normalizedSelected)).slice(0, 100),
          path: normalizedSelected,
          max_mode: 'rw',
          exclude: [],
        }),
      );
      if (!selectedExistingScope && scope.target.kind === 'scope') {
        createdScopeId = scope.target.scope_id;
      }

      await enableTargetAccess(projectId, scope.target);

      await createOptionalConnectors(scope, optionalProvidersToCreate, projectId);

      await onCreated(scope);
      onClose();
    } catch (err) {
      if (createdScopeId) {
        await deleteScope(projectId, createdScopeId).catch(() => undefined);
      }
      console.error('[CreateAccessModal] Failed to create access:', err);
      setError(errorMessage(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <DialogRoot open onClose={saving ? undefined : onClose} backdrop="strong" dismissOnBackdrop={!saving}>
      <DialogSurface width={760} ariaLabel="Create access">
        <DialogHeader
          title="New folder access"
          description="Choose the job first, then bind it to a folder. Git Remote and Puppyone CLI are always included for the folder."
          onClose={saving ? undefined : onClose}
        />
        <DialogBody style={{ padding: '12px 20px 16px' }}>
          <IntentPicker value={intent} onChange={setIntent} />
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))',
              gap: 16,
              alignItems: 'start',
            }}
          >
            <FolderAccessTree
              projectId={projectId}
              selectedPath={normalizedSelected}
              existingPathSet={existingPathSet}
              initialExpandedPath={initialSelectedPath}
              onSelect={selectPath}
            />

            <div style={{ display: 'flex', flexDirection: 'column', gap: 12, minWidth: 0 }}>
              <FieldLabel label="Access name" required>
                <input
                  value={name}
                  onChange={(event) => {
                    setNameTouched(true);
                    setName(event.target.value);
                  }}
                  placeholder={normalizedSelected === null ? 'Choose a path first' : defaultScopeName(normalizedSelected)}
                  disabled={saving}
                  style={{
                    width: '100%',
                    height: 34,
                    boxSizing: 'border-box',
                    borderRadius: 7,
                    border: `1px solid ${T.border}`,
                    background: 'var(--po-control)',
                    color: T.text1,
                    padding: '0 10px',
                    fontSize: ACCESS_MODAL_TYPE.body,
                    fontFamily: T.fontSans,
                    lineHeight: '18px',
                    outline: 'none',
                  }}
                />
              </FieldLabel>

              <FieldLabel label="Path" required>
                <div
                  title={selectedLabel}
                  style={{
                    minHeight: 36,
                    display: 'flex',
                    alignItems: 'center',
                    gap: 8,
                    padding: '0 10px',
                    borderRadius: 7,
                    border: `1px solid ${selectedExistingScope ? 'var(--po-border-strong)' : T.border}`,
                    background: 'var(--po-inset)',
                    color: normalizedSelected === null ? T.text4 : T.text2,
                    fontFamily: T.fontSans,
                    fontSize: ACCESS_MODAL_TYPE.body,
                    lineHeight: '18px',
                    overflow: 'hidden',
                  }}
                >
                  <span style={{ flexShrink: 0, color: selectedExistingScope ? 'var(--po-success)' : T.text3 }}>
                    <TreeDisclosureMarker expanded={normalizedSelected !== null} />
                  </span>
                  <span style={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {selectedLabel}
                  </span>
                </div>
                {selectedExistingScope ? (
                  <div style={{ marginTop: 6, fontSize: ACCESS_MODAL_TYPE.meta, lineHeight: '17px', color: T.text3 }}>
                    This path already has access. You can add share methods or open it.
                  </div>
                ) : null}
              </FieldLabel>

              <div>
                <SectionHeading>Always included</SectionHeading>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                  <MethodRow provider="git_remote" description="Native Git clone, pull, and push for this folder." locked />
                  <MethodRow provider="cli" description="Scoped FS CLI commands for this folder." locked />
                </div>
              </div>

              <div>
                <SectionHeading>Optional methods</SectionHeading>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                  {OPTIONAL_METHODS.map((method) => (
                    <MethodRow
                      key={method.provider}
                      provider={method.provider}
                      description={method.description}
                      checked={method.supported && (optionalProviders.has(method.provider) || existingProviders.has(method.provider))}
                      disabled={!method.supported}
                      locked={existingProviders.has(method.provider)}
                      onCheckedChange={(checked) => toggleOptionalProvider(method.provider, checked)}
                    />
                  ))}
                </div>
              </div>
            </div>
          </div>

          {error ? (
            <div
              style={{
                marginTop: 12,
                padding: '9px 10px',
                borderRadius: 7,
                border: '1px solid color-mix(in srgb, var(--po-danger) 30%, transparent)',
                background: 'color-mix(in srgb, var(--po-danger) 7%, transparent)',
                color: 'var(--po-danger)',
                fontSize: ACCESS_MODAL_TYPE.meta,
                lineHeight: '17px',
                fontFamily: T.fontSans,
              }}
            >
              {error}
            </div>
          ) : null}
        </DialogBody>
        <DialogFooter style={{ padding: '0 20px 20px' }}>
          <button
            type="button"
            onClick={onClose}
            disabled={saving}
            style={secondaryButtonStyle}
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={handleCreate}
            disabled={!canCreate}
            style={{
              ...primaryButtonStyle,
              opacity: canCreate ? 1 : 0.45,
              cursor: canCreate ? 'pointer' : 'not-allowed',
            }}
          >
            <Plus size={14} strokeWidth={2.1} />
            {actionLabel}
          </button>
        </DialogFooter>
      </DialogSurface>
    </DialogRoot>
  );
}

function IntentPicker({
  value,
  onChange,
}: {
  readonly value: AccessIntent;
  readonly onChange: (value: AccessIntent) => void;
}) {
  return (
    <div
      style={{
        marginBottom: 16,
        borderRadius: 12,
        border: `1px solid ${T.cardBorder}`,
        background: 'color-mix(in srgb, var(--po-control) 66%, var(--po-panel) 34%)',
        padding: 12,
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'baseline',
          justifyContent: 'space-between',
          gap: 12,
          marginBottom: 10,
        }}
      >
        <div>
          <div
            style={{
              color: T.text3,
              fontFamily: T.fontSans,
              fontSize: ACCESS_MODAL_TYPE.label,
              lineHeight: '14px',
              fontWeight: 650,
              letterSpacing: '0.04em',
              textTransform: 'uppercase',
              marginBottom: 4,
            }}
          >
            Start with the job
          </div>
          <div
            style={{
              color: T.text1,
              fontFamily: T.fontSans,
              fontSize: 17,
              lineHeight: '22px',
              fontWeight: 650,
            }}
          >
            What are you trying to do?
          </div>
        </div>
        <span
          style={{
            color: T.text3,
            fontFamily: T.fontSans,
            fontSize: ACCESS_MODAL_TYPE.meta,
            lineHeight: '17px',
            whiteSpace: 'nowrap',
          }}
        >
          This helps pick the right way in.
        </span>
      </div>
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(4, minmax(0, 1fr))',
          gap: 8,
        }}
      >
        {INTENT_OPTIONS.map((option) => {
          const active = value === option.id;
          return (
            <button
              key={option.id}
              type="button"
              onClick={() => onChange(option.id)}
              style={{
                minHeight: 104,
                borderRadius: 10,
                border: `1px solid ${active ? 'var(--po-border-strong)' : T.cardBorder}`,
                background: active ? 'var(--po-panel)' : 'transparent',
                boxShadow: active ? '0 1px 2px color-mix(in srgb, var(--po-shadow) 16%, transparent)' : 'none',
                padding: 10,
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'flex-start',
                gap: 8,
                textAlign: 'left',
                cursor: 'pointer',
              }}
            >
              <span
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 8,
                  minWidth: 0,
                }}
              >
                <span
                  style={{
                    width: 28,
                    height: 28,
                    borderRadius: 7,
                    background: providerTileBg(option.provider, active),
                    border: `1px solid ${active ? 'var(--po-border-strong)' : T.border}`,
                    color: providerTileColor(option.provider, active),
                    display: 'inline-flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    flexShrink: 0,
                  }}
                >
                  <ProviderIcon provider={option.provider} size={option.provider === 'git_remote' ? 24 : 15} />
                </span>
                <span
                  style={{
                    color: T.text1,
                    fontFamily: T.fontSans,
                    fontSize: 13,
                    lineHeight: '17px',
                    fontWeight: 650,
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                  }}
                >
                  {option.label}
                </span>
              </span>
              <span
                style={{
                  color: T.text2,
                  fontFamily: option.preview.includes(' ') && option.preview.includes('://') ? T.fontMono : T.fontSans,
                  fontSize: 11,
                  lineHeight: '16px',
                  minHeight: 32,
                }}
              >
                {option.preview}
              </span>
              <span style={{ display: 'flex', gap: 5, flexWrap: 'wrap' }}>
                {option.chips.map((chip) => (
                  <span
                    key={chip}
                    style={{
                      height: 20,
                      padding: '0 7px',
                      borderRadius: 999,
                      border: `1px solid ${T.cardBorder}`,
                      background: 'color-mix(in srgb, var(--po-control) 70%, var(--po-panel) 30%)',
                      color: T.text3,
                      display: 'inline-flex',
                      alignItems: 'center',
                      fontFamily: T.fontSans,
                      fontSize: 10,
                      lineHeight: '14px',
                      fontWeight: 650,
                    }}
                  >
                    {chip}
                  </span>
                ))}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

function MethodRow({
  provider,
  description,
  locked = false,
  disabled = false,
  checked = false,
  onCheckedChange,
}: {
  readonly provider: string;
  readonly description: string;
  readonly locked?: boolean;
  readonly disabled?: boolean;
  readonly checked?: boolean;
  readonly onCheckedChange?: (checked: boolean) => void;
}) {
  const inactive = disabled && !locked;
  const enabled = locked || checked;

  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 10,
        minHeight: 48,
        padding: '8px 10px',
        borderRadius: 8,
        border: `1px solid ${enabled ? 'var(--po-border-strong)' : T.cardBorder}`,
        background: enabled ? 'var(--po-control)' : inactive ? 'color-mix(in srgb, var(--po-panel) 72%, var(--po-canvas))' : 'transparent',
        boxSizing: 'border-box',
        opacity: inactive ? 0.58 : 1,
      }}
    >
      <span
        style={{
          width: 28,
          height: 28,
          flexShrink: 0,
          borderRadius: 7,
          display: 'inline-flex',
          alignItems: 'center',
          justifyContent: 'center',
          background: 'var(--po-hover)',
          color: inactive ? T.text4 : T.text2,
        }}
      >
        <ProviderIcon provider={provider} size={16} />
      </span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 }}>
          <span style={{ fontSize: ACCESS_MODAL_TYPE.body, fontWeight: 600, color: inactive ? T.text3 : T.text2, fontFamily: T.fontSans, lineHeight: '18px', minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {getAccessProviderLabel(provider)}
          </span>
          {inactive ? (
            <span
              style={{
                flexShrink: 0,
                height: 18,
                padding: '0 6px',
                borderRadius: 999,
                border: `1px solid ${T.cardBorder}`,
                color: T.text4,
                fontSize: ACCESS_MODAL_TYPE.label,
                lineHeight: '16px',
                fontWeight: 600,
                fontFamily: T.fontSans,
              }}
            >
              Soon
            </span>
          ) : null}
        </div>
        <div style={{ marginTop: 2, fontSize: ACCESS_MODAL_TYPE.meta, lineHeight: '17px', color: inactive ? T.text4 : T.text3, fontFamily: T.fontSans, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
          {description}
        </div>
      </div>
      <ToggleSwitch
        checked={enabled}
        onCheckedChange={locked || disabled ? undefined : onCheckedChange}
        disabled={disabled}
        ariaLabel={`${getAccessProviderLabel(provider)} ${enabled ? 'on' : 'off'}`}
        title={locked ? 'Already enabled' : disabled ? 'Coming soon' : undefined}
        size="xs"
      />
    </div>
  );
}

function FieldLabel({
  label,
  required = false,
  children,
}: {
  readonly label: string;
  readonly required?: boolean;
  readonly children: ReactNode;
}) {
  return (
    <label style={{ display: 'block', minWidth: 0 }}>
      <div style={{ marginBottom: 8, fontSize: ACCESS_MODAL_TYPE.label, lineHeight: '14px', fontWeight: 600, letterSpacing: '0.04em', textTransform: 'uppercase', color: 'var(--po-text-subtle)', fontFamily: T.fontSans, display: 'inline-flex', alignItems: 'center', gap: 4 }}>
        {label}
        {required ? (
          <span
            aria-hidden
            style={{
              width: 5,
              height: 5,
              borderRadius: 999,
              background: 'var(--po-danger)',
              display: 'inline-block',
            }}
          />
        ) : null}
      </div>
      {children}
    </label>
  );
}

function SectionHeading({ children }: { readonly children: ReactNode }) {
  return (
    <div style={{ marginBottom: 8, fontSize: ACCESS_MODAL_TYPE.label, lineHeight: '14px', fontWeight: 600, letterSpacing: '0.04em', textTransform: 'uppercase', color: 'var(--po-text-subtle)', fontFamily: T.fontSans }}>
      {children}
    </div>
  );
}

function providerTileBg(provider: string, active: boolean): string {
  if (provider === 'cli') return active ? 'color-mix(in srgb, var(--po-accent) 92%, var(--po-panel) 8%)' : 'color-mix(in srgb, var(--po-accent) 14%, var(--po-panel) 86%)';
  if (provider === 'git_remote') return active ? 'color-mix(in srgb, var(--po-git-brand) 18%, var(--po-panel) 82%)' : 'color-mix(in srgb, var(--po-git-brand) 9%, var(--po-panel) 91%)';
  return active ? 'var(--po-panel)' : 'color-mix(in srgb, var(--po-control) 70%, var(--po-panel) 30%)';
}

function providerTileColor(provider: string, active: boolean): string {
  if (provider === 'cli') return active ? 'var(--po-text-inverse)' : 'var(--po-accent)';
  if (provider === 'git_remote') return 'var(--po-git-brand-text)';
  return active ? T.text1 : T.text2;
}

function normalizePath(path: string): string {
  return path.trim().replace(/^\/+|\/+$/g, '').replace(/\/+/g, '/');
}

async function createOptionalConnectors(
  scope: RepositoryView,
  providers: readonly OptionalProvider[],
  projectId: string,
) {
  await Promise.all(
    providers.map((provider) => {
      const method = OPTIONAL_METHODS.find((item) => item.provider === provider);
      if (!method) return Promise.resolve();
      if (provider === 'mcp') {
        return createMcpEndpoint({
          project_id: projectId,
          path: scope.path,
          name: getAccessProviderLabel(provider),
          accesses: [{ path: scope.path, json_path: '', readonly: scope.max_mode !== 'rw' }],
        });
      }
      return createAccessSurface(projectId, {
        target: scope.target,
        kind: provider,
        direction: method.direction,
        name: getAccessProviderLabel(provider),
        config: {},
        trigger: { type: 'manual' },
      });
    }),
  );
}

function formatPath(path: string): string {
  const parts = normalizePath(path).split('/').filter(Boolean);
  return parts.length === 0 ? 'Root' : ['Root', ...parts].join(' / ');
}

function defaultScopeName(path: string): string {
  if (!path) return 'Project files';
  const last = path.split('/').filter(Boolean).at(-1) ?? path;
  return last
    .replace(/[-_]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/\b\w/g, (ch) => ch.toUpperCase());
}

function errorMessage(err: unknown): string {
  if (err instanceof Error && err.message) return err.message;
  return 'Could not create access. Please try again.';
}

const secondaryButtonStyle: CSSProperties = {
  height: 32,
  padding: '0 12px',
  borderRadius: 6,
  border: `1px solid ${T.border}`,
  background: 'transparent',
  color: T.text2,
  fontSize: ACCESS_MODAL_TYPE.body,
  fontWeight: 500,
  fontFamily: T.fontSans,
  lineHeight: 1,
  cursor: 'pointer',
};

const primaryButtonStyle: CSSProperties = {
  height: 32,
  padding: '0 13px',
  borderRadius: 6,
  border: '1px solid var(--po-accent)',
  background: 'var(--po-accent)',
  color: 'var(--po-text-inverse)',
  fontSize: ACCESS_MODAL_TYPE.body,
  fontWeight: 600,
  fontFamily: T.fontSans,
  lineHeight: 1,
  display: 'inline-flex',
  alignItems: 'center',
  justifyContent: 'center',
  gap: 6,
};
