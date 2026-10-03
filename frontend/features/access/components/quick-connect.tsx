'use client';

import { useWorkspaceRouter } from '@/features/workspace/navigation';

/**
 * Quick-Connect — per-provider "how do I use this access point?" body.
 *
 * Different connector kinds expose fundamentally different surfaces.
 * The data view's `ConnectMethods` already encoded this for us; we
 * mirror its split here:
 *
 *   • CLI / Git Remote  → primary AI-agent setup prompt, with manual
 *     terminal commands tucked behind a secondary disclosure.
 *   • agent             → ActivationCard (Activate / Open chat) — agents
 *     are Puppyone's in-app chat, never an externally-pasted prompt.
 *   • mcp / sandbox / 3p → just the connect URL / endpoint with copy
 *     buttons. No fake "prompt for an AI agent" — those connectors are
 *     configured elsewhere, not driven by prompt-pasting.
 *
 * All 5 Body components live in this single file because they're a
 * family of mutually-exclusive branches behind `ConnectorAccessPanel`,
 * the one router that picks the right one. Reading them side-by-side
 * makes it trivial to spot drift between providers. CLI uses an explicit
 * one-time credential issuance panel; other providers share the neutral
 * SubSectionLabel + KvBlock idiom.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';

import { CountBadge } from '@/components/ui/CountBadge';
import { buildGitSyncPrompt, buildMcpSetupPrompt, buildTerminalCliPrompt } from '@/lib/accessPointCliPrompt';
import { activateAccessSurfaceAgent, type AccessSurface, type RepositoryView } from '@/lib/repoApi';
import { canonicalGitUrlForTarget } from '@/lib/gitRemote';
import {
  getAccessProviderLabel,
  isAgentProvider,
  isCliProvider,
  isGitRemoteProvider,
  isMcpProvider,
  isSandboxProvider,
  normalizeConnectorProvider,
} from '@/lib/accessProviderRegistry';
import { AI_AGENT_ENABLED } from '@/lib/featureFlags';
import { T } from '@/features/access/lib/tokens';
import {
  getApiBase,
  profileSlug,
  scopePathToDataUrl,
} from '@/features/access/lib/format';
import {
  CommandBlock,
  KvBlock,
  PromptBlock,
  SubSectionLabel,
} from '@/features/access/components/ui-blocks';
import { CliCredentialIssuePanel } from '@/features/files/components/access-points/connect-methods/CliCredentialIssuePanel';
import { GitCredentialIssuePanel } from '@/features/files/components/access-points/connect-methods/GitCredentialIssuePanel';

// ─── Per-provider access panel ───────────────────────────────────────

export function ConnectorAccessPanel({
  connector,
  scope,
}: {
  readonly connector: AccessSurface;
  readonly scope: RepositoryView | undefined;
}) {
  const apiBase = useMemo(() => getApiBase(), []);
  if (!scope) return null;

  const provider = normalizeConnectorProvider(connector.kind);
  if (isCliProvider(provider)) {
    return <TerminalCliBody connector={connector} scope={scope} apiBase={apiBase} />;
  }
  if (isGitRemoteProvider(provider)) {
    return <GitRemoteBody connector={connector} scope={scope} apiBase={apiBase} />;
  }
  if (isAgentProvider(provider)) {
    // Agent surface is gated on the AI_AGENT_ENABLED flag (see
    // `frontend/lib/featureFlags.ts`). When hidden, fall through to
    // an empty render — callers should be filtering agent connectors
    // out at the list level so this branch shouldn't even be hit,
    // but we treat it as a defensive null-render in case stale state
    // or a deep link still navigates here.
    if (!AI_AGENT_ENABLED) return null;
    return <AgentBody connector={connector} scope={scope} />;
  }
  if (isMcpProvider(provider)) {
    return <McpBody connector={connector} scope={scope} />;
  }
  if (isSandboxProvider(provider)) {
    return <SandboxBody scope={scope} />;
  }
  return <ThirdPartyBody connector={connector} scope={scope} />;
}

// ─── Body: Terminal CLI ──────────────────────────────────────────────

function TerminalCliBody({
  connector,
  scope,
  apiBase,
}: {
  readonly connector: AccessSurface;
  readonly scope: RepositoryView;
  readonly apiBase: string;
}) {
  const scopeName = scope.name || (scope.path === '' ? 'root' : scope.path);
  const profileName = profileSlug(scope.name || scope.path || 'root');

  return (
    <CliCredentialIssuePanel
      connectorId={connector.id}
      target={scope.target}
    >
      {(accessKey) => {
        const { installLine, loginLine, exploreLines, fileLines, prompt } = buildTerminalCliPrompt({
          apiBase,
          accessKey,
          profileName,
          scopeName,
        });
        const steps = [
          { title: 'Install once', lines: [installLine] },
          { title: 'Sign in to this scope', lines: [loginLine] },
          { title: 'Explore safely', lines: exploreLines },
          { title: 'Read & write files', lines: fileLines },
        ];
        return (
          <ConnectPathChooser
            prompt={prompt}
            steps={steps}
            agentDescription='Paste this setup prompt into Codex, Cursor, or Claude. The agent can install, sign in, and use this scope for you.'
            manualTitle='Set up in terminal'
            manualDescription='Run these commands yourself when you want direct CLI access without handing setup to an agent.'
          />
        );
      }}
    </CliCredentialIssuePanel>
  );
}

// ─── Body: Git Remote ────────────────────────────────────────────────

function GitRemoteBody({
  connector,
  scope,
  apiBase,
}: {
  readonly connector: AccessSurface;
  readonly scope: RepositoryView;
  readonly apiBase: string;
}) {
  const scopeName = scope.name || (scope.path === '' ? 'root' : scope.path);
  const gitUrl = canonicalGitUrlForTarget(apiBase, scope.target);
  const {
    cloneLines,
    existingFolderLines,
    workflowLines,
    prompt,
  } = buildGitSyncPrompt({ gitUrl, scopeName, directoryName: scopeName });
  const steps = [
    { title: 'Clone to a new folder', lines: cloneLines },
    { title: 'Publish an existing folder', lines: existingFolderLines },
    { title: 'Day-to-day workflow', lines: workflowLines },
  ];
  return (
    <>
      <GitCredentialIssuePanel
        connectorId={connector.id}
        gitUrl={gitUrl}
        scopeMode={scope.max_mode}
        target={scope.target}
      />
      <ConnectPathChooser
        prompt={prompt}
        steps={steps}
        agentDescription='Paste this Git setup prompt into your coding agent. It can clone this scope or publish an existing folder.'
        manualTitle='Show Git commands'
        manualDescription='Run these commands yourself when you want direct Git access.'
      />
    </>
  );
}

function ConnectPathChooser({
  prompt,
  steps,
  agentDescription,
  manualTitle,
  manualDescription,
}: {
  readonly prompt: string;
  readonly steps: ReadonlyArray<{ title: string; lines: readonly string[] }>;
  readonly agentDescription: string;
  readonly manualTitle: string;
  readonly manualDescription: string;
}) {
  const [manualOpen, setManualOpen] = useState(false);
  const lineStyle = {
    fontSize: 12,
    lineHeight: '18px',
    color: T.text2,
    fontFamily: T.fontSans,
  } as const;
  const strongInlineStyle = {
    fontWeight: 600,
  } as const;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <p
        style={{
          margin: 0,
          ...lineStyle,
        }}
      >
        <span style={strongInlineStyle}>Use an AI agent</span>
        <span> · {agentDescription}</span>
      </p>
      <PromptBlock prompt={prompt} />
      <div style={{ borderTop: `1px solid ${T.cardBorder}`, paddingTop: 2 }}>
        <button
          type='button'
          onClick={() => setManualOpen((v) => !v)}
          aria-expanded={manualOpen}
          style={{
            all: 'unset',
            cursor: 'pointer',
            minHeight: 32,
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            width: '100%',
            fontFamily: T.fontSans,
            color: T.text2,
          }}
        >
          <span
            aria-hidden
            style={{
              width: 10,
              height: 10,
              display: 'inline-flex',
              alignItems: 'center',
              justifyContent: 'center',
              color: T.text3,
              transform: manualOpen ? 'rotate(90deg)' : 'rotate(0deg)',
              transition: `transform 0.15s ${T.ease}`,
              flexShrink: 0,
            }}
          >
            <svg width={10} height={10} viewBox='0 0 12 12' fill='none' stroke='currentColor' strokeWidth={1.6} strokeLinecap='round' strokeLinejoin='round'>
              <path d='M4 2.5l3.5 3.5L4 9.5' />
            </svg>
          </span>
          <span
            style={{
              flex: 1,
              minWidth: 0,
              ...lineStyle,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
          >
            <span style={strongInlineStyle}>Or use it manually</span>
            <span> · {manualTitle} · {manualDescription}</span>
          </span>
        </button>
        {manualOpen ? (
          <div style={{ padding: '8px 0 2px 18px' }}>
            <ConnectionStepsList steps={steps} />
          </div>
        ) : null}
      </div>
    </div>
  );
}

function ConnectionStepsList({
  steps,
}: {
  readonly steps: ReadonlyArray<{ title: string; lines: readonly string[] }>;
}) {
  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        gap: 10,
        marginBottom: 12,
      }}
    >
      {steps.map((step, idx) => (
        <div key={step.title} style={{ display: 'flex', gap: 10 }}>
          <span style={{ marginTop: 1 }}>
            <CountBadge value={idx + 1} size="sm" tone="neutral" />
          </span>
          <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 6 }}>
            <span style={{ fontSize: 12, fontWeight: 600, color: T.text1, fontFamily: T.fontSans }}>
              {step.title}
            </span>
            <CommandBlock lines={step.lines} />
          </div>
        </div>
      ))}
    </div>
  );
}

// ─── Body: AI Agent ──────────────────────────────────────────────────

function AgentBody({
  connector,
  scope,
}: {
  readonly connector: AccessSurface;
  readonly scope: RepositoryView;
}) {
  const router = useWorkspaceRouter();
  // Activation is explicit state; repository target/view fields never double
  // as lifecycle flags.
  const [activated, setActivated] = useState<boolean>(connector.config?.activated === true);
  const [activating, setActivating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Re-sync when connector changes underneath us (e.g. SWR refetch).
  useEffect(() => {
    setActivated(connector.config?.activated === true);
  }, [connector.config?.activated]);

  const goToChat = useCallback(() => {
    // The actual chat runtime lives behind the data view's right panel.
    // Drop the user there — the page-level wiring opens the agent_chat
    // panel for this connector. Using `?ap=...` is enough; the data
    // page reads it on mount.
    router.push(scopePathToDataUrl(scope.project_id, scope.path) + `?ap=${connector.id}`);
  }, [router, connector, scope.path, scope.project_id]);

  const handleActivate = useCallback(async () => {
    if (activating) return;
    setActivating(true);
    setError(null);
    try {
      const updated = await activateAccessSurfaceAgent(scope.project_id, connector.id);
      setActivated(updated.config?.activated === true);
      // Then immediately route to chat — same flow as ConnectMethods.
      router.push(scopePathToDataUrl(scope.project_id, scope.path) + `?ap=${connector.id}`);
    } catch (err) {
      setError((err as Error).message || 'Failed to activate AI Agent');
    } finally {
      setActivating(false);
    }
  }, [activating, connector, scope.path, scope.project_id, router]);

  const scopeName = scope.name || (scope.path === '' ? 'root' : scope.path);

  if (!activated) {
    return (
      <ActivationCard
        title="Activate the AI Agent for this scope"
        body={`Creates an in-app chat agent bound to ${scopeName}. Permissions come from the scope's read/write mode — no separate folder picker.`}
        actionLabel={activating ? 'Activating…' : 'Activate'}
        disabled={activating}
        error={error}
        onAction={handleActivate}
      />
    );
  }

  return (
    <ActivationCard
      title="AI Agent is ready"
      body={`Open an in-app chat with read & write access to ${scopeName}. The chat runtime uses this scope directly; MCP setup belongs in the MCP method.`}
      actionLabel="Open chat"
      onAction={goToChat}
    />
  );
}

// ─── Body: MCP server ────────────────────────────────────────────────

function McpBody({
  connector,
  scope,
}: {
  readonly connector: AccessSurface;
  readonly scope: RepositoryView;
}) {
  const apiBase = useMemo(() => getApiBase(), []);
  const configKey = connector.config?.api_key;
  const apiKey = typeof configKey === 'string' ? configKey : '';
  const scopeName = scope.name || (scope.path === '' ? 'root' : scope.path);
  const setup = useMemo(
    () =>
      buildMcpSetupPrompt({
        apiBase,
        apiKey,
        scopeName,
        accessPointName: connector.name ?? undefined,
      }),
    [apiBase, apiKey, scopeName, connector.name],
  );

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      {!apiKey ? <McpKeyNotice /> : null}
      <p
        style={{
          margin: 0,
          color: T.text2,
          fontSize: 12,
          lineHeight: '18px',
          fontFamily: T.fontSans,
        }}
      >
        Add this MCP server to your client. The only required values are the server URL and API key; the JSON below includes both.
      </p>
      <div>
        <SubSectionLabel>MCP client JSON</SubSectionLabel>
        <CommandBlock lines={setup.config.split('\n')} />
      </div>
      <div>
        <SubSectionLabel>MCP connection</SubSectionLabel>
        <KvBlock
          rows={[
            { label: 'MCP server', value: setup.serverUrl, mono: true, copyable: true },
            { label: 'API key', value: apiKey || '<mcp-api-key>', mono: true, copyable: true },
            { label: 'Scope', value: scopeName },
            { label: 'File tools', value: 'Controlled by server policy' },
            { label: 'Shell/Bash', value: 'Not exposed' },
          ]}
        />
      </div>
    </div>
  );
}

function McpKeyNotice() {
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

// ─── Body: Sandbox ───────────────────────────────────────────────────

function SandboxBody({ scope }: { readonly scope: RepositoryView }) {
  const scopeName = scope.name || (scope.path === '' ? 'root' : scope.path);
  return (
    <>
      <SubSectionLabel>Sandbox mount</SubSectionLabel>
      <KvBlock
        rows={[
          { label: 'Scope', value: scopeName },
          { label: 'Mount target', value: '/workspace inside the container', mono: true },
          { label: 'Image', value: 'puppyone-sandbox:python3.11', mono: true, copyable: true },
        ]}
      />
    </>
  );
}

// ─── Body: third-party ───────────────────────────────────────────────

function ThirdPartyBody({
  connector,
  scope,
}: {
  readonly connector: AccessSurface;
  readonly scope: RepositoryView;
}) {
  const router = useWorkspaceRouter();
  const providerLabel = getAccessProviderLabel(connector.kind);

  const handleConfigure = useCallback(() => {
    router.push(scopePathToDataUrl(scope.project_id, scope.path) + `?ap=${connector.id}`);
  }, [router, connector, scope.path, scope.project_id]);

  return (
    <ActivationCard
      title={`Configure ${providerLabel}`}
      body={`OAuth, sync triggers, and field mapping for ${providerLabel} live in the data view's connector panel. Open it to authorize, set schedules, and edit mappings.`}
      actionLabel="Open in data view"
      onAction={handleConfigure}
    />
  );
}

// ─── ActivationCard ──────────────────────────────────────────────────
// Used by AgentBody and ThirdPartyBody — same shape, different copy.

function ActivationCard({
  title,
  body,
  actionLabel,
  disabled = false,
  error,
  onAction,
}: {
  readonly title: string;
  readonly body: string;
  readonly actionLabel: string;
  readonly disabled?: boolean;
  readonly error?: string | null;
  readonly onAction: () => void;
}) {
  return (
    <div
      style={{
        borderRadius: 8,
        border: `1px dashed ${T.cardBorder}`,
        background: T.cardBg,
        padding: 14,
        display: 'flex',
        flexDirection: 'column',
        gap: 10,
        marginBottom: 14,
        fontFamily: T.fontSans,
      }}
    >
      <div style={{ fontSize: 12, fontWeight: 600, color: T.text1, fontFamily: T.fontSans }}>{title}</div>
      <div style={{ fontSize: 12, color: T.text2, lineHeight: 1.6, fontFamily: T.fontSans }}>{body}</div>
      <button
        type="button"
        onClick={onAction}
        disabled={disabled}
        style={{
          alignSelf: 'flex-start',
          display: 'inline-flex',
          alignItems: 'center',
          justifyContent: 'center',
          height: 30,
          padding: '0 14px',
          fontSize: 12,
          fontWeight: 600,
          fontFamily: T.fontSans,
          color: disabled ? T.text3 : 'var(--po-text-inverse)',
          background: disabled ? 'var(--po-border-subtle)' : 'var(--po-text)',
          border: 'none',
          borderRadius: 999,
          cursor: disabled ? 'not-allowed' : 'pointer',
          boxShadow: disabled
            ? 'none'
            : '0 1px 2px var(--po-shadow), 0 0 0 1px var(--po-border-subtle)',
          transition: 'background 0.15s ease, color 0.15s ease, box-shadow 0.15s ease',
        }}
      >
        {actionLabel}
      </button>
      {error && (
        <div style={{ fontSize: 10, color: 'var(--po-danger)', lineHeight: 1.5, fontFamily: T.fontSans }}>{error}</div>
      )}
    </div>
  );
}
