'use client';

import { useCallback, useMemo, useState } from 'react';
import { DialogBody, DialogHeader, DialogRoot, DialogSurface } from '@/components/ui/Dialog';
import { CountBadge } from '@/components/ui/CountBadge';
import { buildGitSyncPrompt, buildMcpSetupPrompt } from '@/lib/accessPointCliPrompt';
import type { AccessSurface, RepositoryView } from '@/lib/repoApi';
import { canonicalGitUrlForTarget } from '@/lib/gitRemote';
import { isMcpProvider } from '@/lib/accessProviderRegistry';
import { T } from '@/features/access/lib/tokens';
import { getApiBase, isGitBuiltinProvider } from '@/features/access/lib/format';
import { CopyIcon, ProviderIcon } from '@/features/access/components/icons';
import { CommandBlock } from '@/features/access/components/ui-blocks';
import { ConnectorAccessPanel } from '@/features/access/components/quick-connect';
import { GitCredentialIssuePanel } from '@/features/files/components/access-points/connect-methods/GitCredentialIssuePanel';
import { formatScopePath } from '@/features/access/components/ScopeHeader';
import { getConnectorDisplayName, getProviderIconSize, getProviderTileSize, getProviderTileStyle } from '@/features/access/components/connectorVisuals';

export function ConnectorConnectDialog({
  connector,
  scope,
  onClose,
}: {
  readonly connector: AccessSurface;
  readonly scope: RepositoryView | undefined;
  readonly onClose: () => void;
}) {
  const name = getConnectorDisplayName(connector);
  const tile = getProviderTileStyle(connector.kind, false);
  const tileSize = getProviderTileSize(connector.kind);
  const iconSize = getProviderIconSize(connector.kind);
  const isGitRemote = isGitBuiltinProvider(connector.kind);
  const isMcp = isMcpProvider(connector.kind);

  return (
    <DialogRoot onClose={onClose}>
      <DialogSurface width={isMcp ? 620 : 680} maxHeight='min(760px, calc(100vh - 32px))'>
        <DialogHeader
          title={isMcp ? <McpDialogTitle /> : isGitRemote ? 'Git commands' : `Connect ${name}`}
          description={
            isGitRemote
              ? `${scope ? formatScopePath(scope) : 'Scope'} · terminal setup`
              : isMcp
                ? <McpDialogDescription scope={scope} />
              : undefined
          }
          onClose={onClose}
          style={{ padding: isMcp ? '20px 24px 8px' : '14px 20px 4px' }}
          leading={
            <div
              style={{
                width: isMcp ? 44 : tileSize,
                height: isMcp ? 44 : tileSize,
                borderRadius: isMcp ? 9 : isGitBuiltinProvider(connector.kind) ? 7 : 6,
                background: tile.background,
                border: `1px solid ${tile.border}`,
                color: tile.color,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                overflow: isGitBuiltinProvider(connector.kind) ? 'hidden' : undefined,
              }}
            >
              <ProviderIcon provider={connector.kind} size={isMcp ? 21 : iconSize} />
            </div>
          }
        />
        <DialogBody style={{ padding: isMcp ? '4px 24px 24px' : '4px 20px 20px' }}>
          {isGitRemote ? (
            <GitManualCommandsPanel connector={connector} scope={scope} />
          ) : isMcp ? (
            <McpConnectionPanel connector={connector} scope={scope} />
          ) : (
            <ConnectorAccessPanel
              connector={connector}
              scope={scope}
            />
          )}
        </DialogBody>
      </DialogSurface>
    </DialogRoot>
  );
}

function McpDialogTitle() {
  return (
    <span
      style={{
        color: T.text1,
        fontSize: 18,
        lineHeight: '24px',
        fontWeight: 600,
        fontFamily: T.fontSans,
      }}
    >
      Connect MCP Server
    </span>
  );
}

function McpDialogDescription({ scope }: { readonly scope: RepositoryView | undefined }) {
  return (
    <span
      style={{
        color: T.text2,
        fontSize: 13,
        lineHeight: '18px',
        fontFamily: T.fontSans,
      }}
    >
      {scope ? formatScopePath(scope) : 'Scope'} · URL and API key
    </span>
  );
}

function McpConnectionPanel({
  connector,
  scope,
}: {
  readonly connector: AccessSurface;
  readonly scope: RepositoryView | undefined;
}) {
  const setup = useMemo(() => {
    const configKey = connector.config?.api_key;
    const apiKey = typeof configKey === 'string' ? configKey : '';
    if (!scope) {
      return {
        apiKey,
        serverUrl: '',
        authorizationLine: '',
        serverName: '',
        config: '',
      };
    }
    const scopeName = scope.name || (scope.path === '' ? 'root' : scope.path || 'Root');
    return {
      ...buildMcpSetupPrompt({
        apiBase: getApiBase(),
        apiKey,
        scopeName,
        accessPointName: connector.name ?? undefined,
      }),
      apiKey,
    };
  }, [connector.config?.api_key, connector.name, scope]);

  if (!scope) {
    return (
      <div style={{ color: T.text3, fontSize: 12, lineHeight: '18px', fontFamily: T.fontSans }}>
        Select a scope before connecting this MCP server.
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
      {!setup.apiKey ? <McpNoKeyNotice /> : null}
      <div
        style={{
          color: T.text2,
          fontSize: 13,
          lineHeight: '20px',
          fontFamily: T.fontSans,
        }}
      >
        Add this server to an MCP client with the URL and bearer token. Tool permissions are controlled by this access point on the server.
      </div>
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: '1fr',
          gap: 10,
        }}
      >
        <McpConnectionField
          label='Server URL'
          value={setup.serverUrl}
        />
        <McpConnectionField
          label='API key'
          value={setup.apiKey || '<mcp-api-key>'}
          secret
        />
        <div
          style={{
            borderRadius: 7,
            border: `1px solid ${T.cardBorder}`,
            background: 'color-mix(in srgb, var(--po-control) 42%, transparent)',
            padding: '10px 12px',
            color: T.text3,
            fontSize: 12,
            lineHeight: '18px',
            fontFamily: T.fontSans,
          }}
        >
          The key is sent as <span style={{ fontFamily: T.fontMono, color: T.text2 }}>Authorization: Bearer ...</span>. The client JSON below already includes it.
        </div>
      </div>
      <div>
        <McpSectionHeader
          title='Client JSON'
          description='Use this when your MCP client accepts a JSON config block.'
        />
        <CommandBlock lines={(setup.config || '{}').split('\n')} />
      </div>
    </div>
  );
}

function McpConnectionField({
  label,
  value,
  secret = false,
}: {
  readonly label: string;
  readonly value: string;
  readonly secret?: boolean;
}) {
  const [copied, setCopied] = useState(false);
  const copyValue = useCallback(async () => {
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      /* clipboard unavailable */
    }
  }, [value]);

  return (
    <div
      style={{
        borderRadius: 8,
        border: `1px solid ${T.cardBorder}`,
        background: 'color-mix(in srgb, var(--po-inset) 88%, var(--po-panel) 12%)',
        padding: '12px 12px 12px 14px',
        display: 'grid',
        gridTemplateColumns: 'minmax(0, 1fr) 32px',
        gap: 12,
        alignItems: 'center',
      }}
    >
      <div style={{ minWidth: 0, display: 'flex', flexDirection: 'column', gap: 6 }}>
        <span
          style={{
            color: T.text3,
            fontSize: 11,
            lineHeight: '14px',
            fontWeight: 600,
            fontFamily: T.fontSans,
          }}
        >
          {label}
        </span>
        <span
          title={value}
          style={{
            color: T.text1,
            fontSize: 14,
            lineHeight: '20px',
            fontFamily: T.fontMono,
            whiteSpace: 'nowrap',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
          }}
        >
          {value}
        </span>
        {secret ? (
          <span
            style={{
              color: T.text3,
              fontSize: 11,
              lineHeight: '14px',
              fontFamily: T.fontSans,
            }}
          >
            Keep this key private.
          </span>
        ) : null}
      </div>
      <button
        type='button'
        aria-label={copied ? `Copied ${label}` : `Copy ${label}`}
        title={copied ? 'Copied' : 'Copy'}
        onClick={copyValue}
        style={{
          width: 32,
          height: 32,
          borderRadius: 7,
          border: `1px solid ${copied ? 'color-mix(in srgb, var(--po-success) 42%, var(--po-border-strong))' : T.border}`,
          background: copied
            ? 'color-mix(in srgb, var(--po-success) 12%, transparent)'
            : 'color-mix(in srgb, var(--po-control) 58%, var(--po-panel) 42%)',
          color: copied ? 'var(--po-success)' : T.text2,
          display: 'inline-flex',
          alignItems: 'center',
          justifyContent: 'center',
          cursor: 'pointer',
          transition: `background 0.12s ${T.ease}, border-color 0.12s ${T.ease}, color 0.12s ${T.ease}`,
        }}
      >
        <CopyIcon size={13} />
      </button>
    </div>
  );
}

function McpSectionHeader({
  title,
  description,
}: {
  readonly title: string;
  readonly description: string;
}) {
  return (
    <div style={{ marginBottom: 8, display: 'flex', flexDirection: 'column', gap: 2 }}>
      <span
        style={{
          color: T.text1,
          fontSize: 13,
          lineHeight: '18px',
          fontWeight: 600,
          fontFamily: T.fontSans,
        }}
      >
        {title}
      </span>
      <span
        style={{
          color: T.text3,
          fontSize: 12,
          lineHeight: '17px',
          fontFamily: T.fontSans,
        }}
      >
        {description}
      </span>
    </div>
  );
}

function McpNoKeyNotice() {
  return (
    <div
      style={{
        borderRadius: 6,
        border: `1px solid color-mix(in srgb, var(--po-warning) 25%, transparent)`,
        background: 'color-mix(in srgb, var(--po-warning) 6%, transparent)',
        color: 'var(--po-warning)',
        fontSize: 12,
        lineHeight: 1.5,
        padding: '8px 10px',
        fontFamily: T.fontSans,
      }}
    >
      This MCP access point has no API key yet. Regenerate the endpoint key before connecting a client.
    </div>
  );
}

function GitManualCommandsPanel({
  connector,
  scope,
}: {
  readonly connector: AccessSurface;
  readonly scope: RepositoryView | undefined;
}) {
  const steps = useMemo(() => {
    if (!scope) return [];
    const scopeName = scope.name || (scope.path === '' ? 'root' : scope.path || 'Root');
    const gitUrl = canonicalGitUrlForTarget(getApiBase(), scope.target);
    const guide = buildGitSyncPrompt({
      gitUrl,
      scopeName,
      directoryName: scopeName,
    });
    return [
      { title: 'Clone into a folder', lines: guide.cloneLines },
      { title: 'Connect an existing folder', lines: guide.existingFolderLines },
      { title: 'Daily workflow', lines: guide.workflowLines },
    ];
  }, [scope]);

  if (!scope) {
    return (
      <div style={{ color: T.text3, fontSize: 12, lineHeight: '18px', fontFamily: T.fontSans }}>
        Select a scope before using Git commands.
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      <GitCredentialIssuePanel
        connectorId={connector.id}
        gitUrl={canonicalGitUrlForTarget(getApiBase(), scope.target)}
        scopeMode={scope.max_mode}
        target={scope.target}
      />
      <div
        style={{
          color: T.text2,
          fontSize: 12,
          lineHeight: '18px',
          fontFamily: T.fontSans,
        }}
      >
        Run one of these command groups in Terminal. This remote is bound to {formatScopePath(scope)}.
      </div>
      <ManualCommandSteps steps={steps} />
    </div>
  );
}

function ManualCommandSteps({
  steps,
}: {
  readonly steps: ReadonlyArray<{ title: string; lines: readonly string[] }>;
}) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      {steps.map((step, index) => (
        <div key={step.title} style={{ display: 'flex', gap: 10 }}>
          <span style={{ marginTop: 1 }}>
            <CountBadge value={index + 1} size="sm" tone="neutral" />
          </span>
          <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 6 }}>
            <span
              style={{
                color: T.text1,
                fontSize: 12,
                lineHeight: '18px',
                fontWeight: 600,
                fontFamily: T.fontSans,
              }}
            >
              {step.title}
            </span>
            <CommandBlock lines={step.lines} />
          </div>
        </div>
      ))}
    </div>
  );
}
