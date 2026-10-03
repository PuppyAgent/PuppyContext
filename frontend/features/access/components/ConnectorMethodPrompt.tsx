'use client';

import { useCallback, useMemo, useState, type CSSProperties, type MouseEvent } from 'react';
import { ExternalLink } from 'lucide-react';
import { AiHandoffButton } from '@/components/ui/AiHandoffButton';
import { buildGitSyncPrompt, buildMcpSetupPrompt, buildTerminalCliPrompt } from '@/lib/accessPointCliPrompt';
import type { AccessSurface, RepositoryView } from '@/lib/repoApi';
import { canonicalGitUrlForTarget } from '@/lib/gitRemote';
import { getAccessProviderPromptKind, isMcpProvider } from '@/lib/accessProviderRegistry';
import { T } from '@/features/access/lib/tokens';
import { getApiBase, profileSlug } from '@/features/access/lib/format';

export function ConnectorMethodPrompt({
  connector,
  scope,
  onConnect,
}: {
  readonly connector: AccessSurface;
  readonly scope: RepositoryView | undefined;
  readonly onConnect?: () => void;
}) {
  const mcpSetup = useMcpClientSetup(connector, scope);
  const prompt = useConnectorSetupPrompt(connector, scope);
  if (isMcpProvider(connector.kind)) {
    return <McpMethodSetupPreview setup={mcpSetup} onConnect={onConnect} />;
  }
  return <MethodPromptPreview prompt={prompt} />;
}

export function ConnectorMethodCopyButton({
  connector,
  scope,
  style,
}: {
  readonly connector: AccessSurface;
  readonly scope: RepositoryView | undefined;
  readonly style?: CSSProperties;
}) {
  const mcpSetup = useMcpClientSetup(connector, scope);
  const prompt = useConnectorSetupPrompt(connector, scope);
  if (isMcpProvider(connector.kind)) {
    return <McpConfigCopyButton config={mcpSetup.config} style={style} />;
  }
  return <PromptCopyButton prompt={prompt} style={style} />;
}

function useConnectorSetupPrompt(connector: AccessSurface, scope: RepositoryView | undefined): string {
  return useMemo(
    () => buildConnectorSetupPrompt(connector, scope),
    [connector, scope],
  );
}

type McpClientSetup = ReturnType<typeof buildMcpSetupPrompt> & {
  readonly apiKey: string;
};

function useMcpClientSetup(connector: AccessSurface, scope: RepositoryView | undefined): McpClientSetup {
  return useMemo(() => {
    const configKey = connector.config?.api_key;
    const apiKey = typeof configKey === 'string' ? configKey : '';
    if (!scope) {
      return {
        apiKey,
        serverUrl: '',
        authorizationLine: '',
        serverName: '',
        config: '',
        prompt: '',
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
}

function McpMethodSetupPreview({
  setup,
  onConnect,
}: {
  readonly setup: McpClientSetup;
  readonly onConnect?: () => void;
}) {
  const preview = setup.serverUrl || 'MCP setup is preparing.';
  return (
    <div
      onClick={(event) => {
        event.stopPropagation();
        onConnect?.();
      }}
      role={onConnect ? 'button' : undefined}
      tabIndex={onConnect ? 0 : undefined}
      onKeyDown={(event) => {
        if (!onConnect) return;
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          event.stopPropagation();
          onConnect();
        }
      }}
      style={{
        position: 'relative',
        minWidth: 0,
        minHeight: 96,
        borderRadius: 7,
        border: `1px solid ${T.cardBorder}`,
        background: 'color-mix(in srgb, var(--po-inset) 92%, var(--po-panel) 8%)',
        overflow: 'hidden',
        cursor: onConnect ? 'pointer' : 'default',
      }}
    >
      <pre
        aria-hidden
        style={{
          margin: 0,
          padding: '9px 10px 30px',
          color: 'color-mix(in srgb, var(--po-text) 58%, var(--po-text-muted) 42%)',
          fontFamily: T.fontMono,
          fontSize: 12,
          lineHeight: 1.55,
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
          maxHeight: 96,
          overflow: 'hidden',
        }}
      >
        {['MCP server', preview, '', `API key ${setup.apiKey ? 'configured' : 'missing'}`].join('\n')}
      </pre>
      <div
        aria-hidden
        style={{
          position: 'absolute',
          inset: 0,
          background: 'linear-gradient(180deg, color-mix(in srgb, var(--po-inset) 18%, transparent) 0%, color-mix(in srgb, var(--po-inset) 92%, var(--po-panel) 8%) 82%)',
          pointerEvents: 'none',
        }}
      />
      <McpConnectButton
        disabled={!setup.config}
        onClick={(event) => {
          event.stopPropagation();
          onConnect?.();
        }}
        style={{
          position: 'absolute',
          left: '50%',
          top: '50%',
          transform: 'translate(-50%, -50%)',
        }}
      />
    </div>
  );
}

function McpConnectButton({
  disabled,
  onClick,
  style,
}: {
  readonly disabled: boolean;
  readonly onClick: (event: MouseEvent<HTMLButtonElement>) => void;
  readonly style?: CSSProperties;
}) {
  return (
    <button
      type='button'
      disabled={disabled}
      onClick={onClick}
      style={{
        height: 32,
        borderRadius: 999,
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        gap: 8,
        padding: '0 14px',
        border: '1px solid var(--po-text)',
        background: 'var(--po-text)',
        color: 'var(--po-canvas)',
        fontSize: 12,
        lineHeight: 1,
        fontWeight: 600,
        fontFamily: 'var(--po-font-sans)',
        whiteSpace: 'nowrap',
        cursor: disabled ? 'not-allowed' : 'pointer',
        boxShadow: disabled
          ? 'none'
          : '0 10px 24px color-mix(in srgb, var(--po-shadow) 30%, transparent)',
        opacity: disabled ? 0.48 : 1,
        transition: 'background 0.14s ease, border-color 0.14s ease, color 0.14s ease, opacity 0.14s ease',
        ...style,
      }}
    >
      <span>View connection</span>
      <ExternalLink size={14} strokeWidth={1.75} />
    </button>
  );
}

function McpConfigCopyButton({
  config,
  style,
}: {
  readonly config: string;
  readonly style?: CSSProperties;
}) {
  const [copied, setCopied] = useState(false);

  const copyConfig = useCallback(async (event: MouseEvent<HTMLButtonElement>) => {
    event.stopPropagation();
    if (!config) return;
    try {
      await navigator.clipboard.writeText(config);
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    } catch {
      /* clipboard unavailable */
    }
  }, [config]);

  return (
    <AiHandoffButton
      disabled={!config}
      onClick={copyConfig}
      copied={copied}
      label='Copy config'
      copiedLabel='Copied'
      style={style}
    />
  );
}

function MethodPromptPreview({ prompt }: { readonly prompt: string }) {
  return (
    <div
      style={{
        position: 'relative',
        minWidth: 0,
        minHeight: 96,
        borderRadius: 7,
        border: `1px solid ${T.cardBorder}`,
        background: 'color-mix(in srgb, var(--po-inset) 92%, var(--po-panel) 8%)',
        overflow: 'hidden',
      }}
    >
      <pre
        aria-hidden
        style={{
          margin: 0,
          padding: '9px 10px 30px',
          color: 'color-mix(in srgb, var(--po-text) 58%, var(--po-text-muted) 42%)',
          fontFamily: T.fontMono,
          fontSize: 12,
          lineHeight: 1.55,
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
          maxHeight: 96,
          overflow: 'hidden',
        }}
      >
        {prompt || 'Access setup is preparing.'}
      </pre>
      <div
        aria-hidden
        style={{
          position: 'absolute',
          inset: 0,
          background: 'linear-gradient(180deg, color-mix(in srgb, var(--po-inset) 18%, transparent) 0%, color-mix(in srgb, var(--po-inset) 92%, var(--po-panel) 8%) 82%)',
          pointerEvents: 'none',
        }}
      />
      <PromptCopyButton
        prompt={prompt}
        style={{
          position: 'absolute',
          left: '50%',
          top: '50%',
          transform: 'translate(-50%, -50%)',
        }}
      />
    </div>
  );
}

function PromptCopyButton({
  prompt,
  style,
}: {
  readonly prompt: string;
  readonly style?: CSSProperties;
}) {
  const [copied, setCopied] = useState(false);

  const copyPrompt = useCallback(async (event: MouseEvent<HTMLButtonElement>) => {
    event.stopPropagation();
    if (!prompt) return;
    try {
      await navigator.clipboard.writeText(prompt);
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    } catch {
      /* clipboard unavailable */
    }
  }, [prompt]);

  return (
    <AiHandoffButton
      disabled={!prompt}
      onClick={copyPrompt}
      copied={copied}
      style={style}
    />
  );
}

function buildConnectorSetupPrompt(connector: AccessSurface, scope: RepositoryView | undefined): string {
  if (!scope) return '';
  const apiBase = getApiBase();
  const scopeName = scope.name || (scope.path === '' ? 'root' : scope.path || 'Root');
  // Ordinary Connector reads are deliberately secret-free. Credential-bearing
  // setup is rendered only by explicit one-time issuance panels.
  const accessKey = '';
  if (isMcpProvider(connector.kind)) {
    return '';
  }
  const promptKind = getAccessProviderPromptKind(connector.kind);
  if (promptKind === 'git_remote') {
    const gitUrl = canonicalGitUrlForTarget(apiBase, scope.target);
    return buildGitSyncPrompt({
      gitUrl,
      scopeName,
      directoryName: scopeName,
    }).prompt;
  }
  if (promptKind === 'terminal_cli') {
    // Plaintext CLI credentials are one-time reveal only. Never build a
    // copyable prompt with a fake placeholder after an ordinary redacted read;
    // the Connect panel owns explicit issuance.
    if (!accessKey) return '';
    return buildTerminalCliPrompt({
      apiBase,
      accessKey,
      profileName: profileSlug(scope.name || scope.path || 'root'),
      scopeName,
    }).prompt;
  }
  return '';
}
