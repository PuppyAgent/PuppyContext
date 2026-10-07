'use client';

/**
 * ConnectMethods — the default ways to access a scope.
 *
 * Renders stacked MethodCards for the per-scope built-ins:
 *
 *   1. Puppyone CLI — copyable terminal setup prompt
 *   2. Git Remote   — copyable Git clone/push prompt
 *   3. AI Agent     — in-app chat runtime (currently HIDDEN behind
 *                     the `AI_AGENT_ENABLED` feature flag in
 *                     `frontend/lib/featureFlags.ts`; the card and
 *                     all activation wiring is still here, just
 *                     not rendered)
 *
 * Puppyone CLI and Git Remote are exposure mechanisms — they hand
 * external clients (Claude Desktop, Cursor, MCP, your local Git
 * worktree) the credentials and setup prompts to read/write this
 * scope from outside Puppyone. AI Agent is a *consumer* of the same
 * data instead of an exposure path; it lives behind the feature flag
 * because the in-app chat surface conflicts with Puppyone's
 * "platform under every agent" positioning.
 *
 * Git and CLI are enabled explicitly for a repository target; Agent is a
 * separately created runtime. CLI and Git credentials are independently issued and
 * shown once; ordinary scope reads intentionally contain no plaintext key. Each connector's
 * `status` (`active` / `paused`) is the single source of truth for
 * whether that method is enabled for this scope, and the in-card toggle
 * routes through `pauseConnector` / `resumeConnector` to flip it.
 *
 * Body visibility is now a pure function of the connector's active
 * status: active → expanded, paused → collapsed. There is no
 * separate user-controlled expand/collapse — see `./connect-methods/
 * MethodCard.tsx` for the full reasoning.
 *
 * The visual primitives (MethodCard, PromptBlock, CommandBlock,
 * Disclosure, etc.) and the per-method bodies live in `./connect-methods/`.
 * This file is the thin section orchestrator that wires up state +
 * mounts the right body inside each MethodCard.
 */

import {
  AiAgentBody,
  GitRemoteBody,
  METHOD_META,
  MethodCard,
  SectionHeader,
  TerminalCliBody,
  profileSlug,
} from '@/features/files/components/access-points/connect-methods';
import { AI_AGENT_ENABLED } from '@/lib/featureFlags';
import { canonicalGitUrlForTarget } from '@/lib/gitRemote';
import {
  activateAccessSurfaceAgent,
  pauseAccessSurface,
  resumeAccessSurface,
  type AccessSurface,
  type RepositoryView,
} from '@/lib/repoApi';
import { useCallback, useEffect, useState, type ReactNode } from 'react';

interface ConnectMethodsBlockProps {
  readonly scope: RepositoryView;
  /** Target-bound CLI connector. Drives the Puppyone CLI card's
   *  on/off toggle. CLI setup explicitly rotates/reveals its own one-time
   *  credential; ordinary Scope reads never rehydrate plaintext. */
  readonly cliConnector: AccessSurface | undefined;
  /** Target-bound Git Remote connector. Drives the Git Remote card's
   *  on/off toggle and its independent Git credential issuance flow. */
  readonly gitRemoteConnector: AccessSurface | undefined;
  /** Target-bound agent connector — also drives the in-card toggle,
   *  separately from the activation flow (`config.activated` is set
   *  during activation, `status` is set by pause/resume; both must
   *  be in the right state for the chat runtime to launch). */
  readonly agentConnector: AccessSurface | undefined;
  readonly projectId: string;
  /** Backend base, e.g. `https://api.puppyone.com`. */
  readonly apiBase: string;
  /** Refresh repo scopes/connectors after agent activation OR a
   *  pause/resume so the parent SWR cache sees the new status. The
   *  inner local connector state is purely for optimistic UI between
   *  request start and the parent's revalidate landing — once the
   *  parent props update, we sync from those. */
  readonly onScopeMutated: () => Promise<unknown>;
  readonly onOpenAgentChat: (agentId: string, scopePath: string) => void;
}

export function ConnectMethodsBlock({
  scope,
  cliConnector,
  gitRemoteConnector,
  agentConnector,
  projectId,
  apiBase,
  onScopeMutated,
  onOpenAgentChat,
}: ConnectMethodsBlockProps) {
  // Local optimistic copies of each built-in connector. We keep them
  // here so a pause/resume click can flip the toggle UI in the same
  // frame as the click, without waiting for the round-trip + parent
  // revalidate to land. Each `useEffect` re-syncs from props once the
  // parent's data refreshes. Same pattern as the existing
  // `localAgentConnector` for activation — extended to cli + git_remote
  // now that those are togglable too.
  const [localCli, setLocalCli] = useState<AccessSurface | undefined>(cliConnector);
  const [localGitRemote, setLocalGitRemote] = useState<AccessSurface | undefined>(gitRemoteConnector);
  const [localAgent, setLocalAgent] = useState<AccessSurface | undefined>(agentConnector);

  useEffect(() => setLocalCli(cliConnector), [cliConnector]);
  useEffect(() => setLocalGitRemote(gitRemoteConnector), [gitRemoteConnector]);
  useEffect(() => setLocalAgent(agentConnector), [agentConnector]);

  const [pendingCli, setPendingCli] = useState(false);
  const [pendingGitRemote, setPendingGitRemote] = useState(false);
  const [pendingAgentToggle, setPendingAgentToggle] = useState(false);

  const [activatingAgent, setActivatingAgent] = useState(false);
  const [agentActivationError, setAgentActivationError] = useState<string | null>(null);

  useEffect(() => {
    setAgentActivationError(null);
  }, [agentConnector]);

  const gitUrl = canonicalGitUrlForTarget(apiBase, scope.target);
  const scopeName = scope.name || (scope.path === '' ? 'root' : scope.path);
  const profileName = profileSlug(scope.name || scope.path || 'root');

  // Active flags — drive both the toggle position AND the body
  // visibility on each MethodCard. The card's body is now a pure
  // function of `active` (no manual expand/collapse), so flipping
  // these flags is the single user-facing way to show or hide the
  // method's body.
  const cliActive = localCli?.status === 'active';
  const gitRemoteActive = localGitRemote?.status === 'active';
  const agentActive = localAgent?.status === 'active';
  const agentActivated = localAgent?.config?.activated === true;

  /**
   * Optimistic-toggle helper.
   *
   * Pause / resume on the backend is a single connector-row update that returns
   * in milliseconds. The perceived latency, when we made the user
   * wait, came almost entirely from the *full SWR revalidate* we ran
   * afterwards (re-fetching the scope + connector list pulls
   * hundreds of KB through the auth middleware → backend → Supabase
   * pipeline, ~1-3s round-trip).
   *
   * The UX contract for a toggle should be: click → instant flip,
   * server confirms in the background. So we:
   *
   *   1. Flip the local copy synchronously — the switch animates
   *      in the same frame as the click.
   *   2. Fire the pause/resume API in the background. We DO NOT
   *      await it from the React handler — the local state is
   *      already correct, so blocking on the network only adds
   *      perceived latency without buying anything.
   *   3. Fire-and-forget the parent revalidate so a stale list
   *      eventually reconciles, but again don't await — the local
   *      state already reflects the new status.
   *   4. On error, roll the local copy back to its previous value
   *      so the toggle bounces visibly. (We log the error too;
   *      future PR can add a toast.)
   *
   * The `pending` flag stays around purely to deduplicate concurrent
   * requests (rapid double-click on the same toggle). It no longer
   * dims the switch — the click feels instant and the server
   * confirmation happens silently.
   */
  const toggleConnector = useCallback(
    (
      connector: AccessSurface,
      setLocal: (c: AccessSurface | undefined) => void,
      setPending: (b: boolean) => void,
    ) => {
      const next: 'active' | 'paused' =
        connector.status === 'active' ? 'paused' : 'active';
      const previous = connector;
      setPending(true);
      setLocal({ ...connector, status: next });

      const request =
        next === 'paused'
          ? pauseAccessSurface(projectId, connector.id)
          : resumeAccessSurface(projectId, connector.id);

      request
        .then(() => {
          // Fire-and-forget revalidate. We don't `await` it — the
          // local optimistic state already matches the server, and
          // blocking on a list refetch (~1-3s for the scopes +
          // connectors round-trip) just to confirm a single-row
          // update would defeat the point of optimistic UI.
          void onScopeMutated();
        })
        .catch((err) => {
          console.error('Failed to toggle connector:', err);
          setLocal(previous);
        })
        .finally(() => {
          setPending(false);
        });
    },
    [projectId, onScopeMutated],
  );

  const handleToggleCli = useCallback(() => {
    if (!localCli || pendingCli) return;
    void toggleConnector(localCli, setLocalCli, setPendingCli);
  }, [localCli, pendingCli, toggleConnector]);

  const handleToggleGitRemote = useCallback(() => {
    if (!localGitRemote || pendingGitRemote) return;
    void toggleConnector(localGitRemote, setLocalGitRemote, setPendingGitRemote);
  }, [localGitRemote, pendingGitRemote, toggleConnector]);

  const handleToggleAgent = useCallback(() => {
    if (!localAgent || pendingAgentToggle) return;
    void toggleConnector(localAgent, setLocalAgent, setPendingAgentToggle);
  }, [localAgent, pendingAgentToggle, toggleConnector]);

  const handleActivateAgent = useCallback(async () => {
    if (!localAgent?.id || activatingAgent || !agentActive) return;
    setActivatingAgent(true);
    setAgentActivationError(null);
    try {
      const updated = await activateAccessSurfaceAgent(projectId, localAgent.id);
      setLocalAgent(updated);
      await onScopeMutated();
      onOpenAgentChat(updated.id, scope.path);
    } catch (err) {
      setAgentActivationError((err as Error).message || 'Failed to activate AI Agent');
    } finally {
      setActivatingAgent(false);
    }
  }, [
    localAgent,
    activatingAgent,
    agentActive,
    projectId,
    onScopeMutated,
    onOpenAgentChat,
    scope.path,
  ]);

  return (
    <section style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      <MethodSection title={METHOD_META.terminal.title}>
        <MethodCard
          meta={METHOD_META.terminal}
          active={cliActive}
          togglePending={pendingCli}
          onToggle={localCli ? handleToggleCli : undefined}
        >
          <TerminalCliBody
            apiBase={apiBase}
            connectorId={localCli?.id ?? ''}
            target={scope.target}
            profileName={profileName}
            scopeName={scopeName}
          />
        </MethodCard>
      </MethodSection>

      <MethodSection title={METHOD_META.sync.title}>
        <MethodCard
          meta={METHOD_META.sync}
          active={gitRemoteActive}
          togglePending={pendingGitRemote}
          onToggle={localGitRemote ? handleToggleGitRemote : undefined}
        >
          <GitRemoteBody
            connectorId={localGitRemote?.id ?? ''}
            gitUrl={gitUrl}
            scopeMode={scope.max_mode}
            scopeName={scopeName}
            target={scope.target}
          />
        </MethodCard>
      </MethodSection>

      {/* AI Agent MethodCard — gated on `AI_AGENT_ENABLED` feature
          flag (see `frontend/lib/featureFlags.ts` for the full
          rationale). The activation handler, toggle wiring,
          optimistic state, and the AiAgentBody component are all
          kept intact below the gate so flipping the flag back to
          `true` re-enables the surface without code changes. */}
      {AI_AGENT_ENABLED && (
        <MethodSection title={METHOD_META.agent.title}>
          <MethodCard
            meta={METHOD_META.agent}
            active={agentActive}
            togglePending={pendingAgentToggle}
            onToggle={localAgent ? handleToggleAgent : undefined}
          >
            <AiAgentBody
              scopeName={scopeName}
              activated={agentActivated}
              // While the connector is paused the activation flow is
              // disabled — the user has to flip the toggle on first
              // before the chat runtime can be wired up. Same gate
              // applies once activated: pausing freezes the "Open chat"
              // button so accidental access is blocked while the
              // connector is intentionally off.
              canActivate={Boolean(localAgent?.id) && agentActive}
              activating={activatingAgent}
              activationError={agentActivationError}
              onActivate={handleActivateAgent}
              onOpenChat={() => {
                if (localAgent?.id && agentActive) {
                  onOpenAgentChat(localAgent.id, scope.path);
                }
              }}
            />
          </MethodCard>
        </MethodSection>
      )}
    </section>
  );
}

function MethodSection({
  title,
  children,
}: {
  readonly title: string;
  readonly children: ReactNode;
}) {
  return (
    <section style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      <SectionHeader eyebrow={title} />
      {children}
    </section>
  );
}
