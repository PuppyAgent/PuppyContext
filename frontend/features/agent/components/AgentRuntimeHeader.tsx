'use client';

import { useAgent } from '@/contexts/AgentContext';
import { AgentChatHeader } from '@/components/chat/AgentChatChrome';
import { runActive } from '../runtime/types';
import type { WebAgentController, AgentWorkspaceState } from '../runtime/controller';

export function AgentRuntimeHeader({ controller, state, onSettings, onClose, onBack }: {
  controller: WebAgentController | null; state: AgentWorkspaceState;
  onSettings: () => void; onClose?: () => void; onBack?: () => void;
}) {
  const { currentAgentId, savedAgents, updateAgentInfo } = useAgent();
  const currentAgent = savedAgents.find(agent => agent.id === currentAgentId);
  const agentName = currentAgent?.name ?? 'Agent';
  const { sessions, sessionId: currentSessionId } = state;
  return (
      <AgentChatHeader
        title={sessions.find(session => session.id === currentSessionId)?.title || agentName}
        agentName={agentName}
        sessions={sessions}
        currentSessionId={currentSessionId}
        busy={state.submitting || runActive(state.runs.at(-1)) || state.loading || Boolean(state.pending)}
        onSelectSession={controller ? id => { void controller.selectSession(id); } : undefined}
        onNewChat={controller?.newChat}
        onSettings={currentAgentId ? onSettings : undefined}
        onRename={currentAgentId ? name => updateAgentInfo(currentAgentId, name, currentAgent?.icon || '') : undefined}
        onClose={onClose}
        onBack={onBack}
      />


  );
}
