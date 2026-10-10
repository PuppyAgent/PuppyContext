'use client';

import dynamic from 'next/dynamic';
import { useEffect, useMemo } from 'react';
import type { SavedAgent } from '@/components/AgentRail';
import { AgentChatEmptyState, AgentChatHeader } from '@/components/chat/AgentChatChrome';
import ChatInputArea from '@/components/chat/ChatInputArea';
import chatStyles from '@/components/chat/AgentChatSurface.module.css';
import { useProjectSession } from '@/features/workspace/session';
import { useAgent } from '@/contexts/AgentContext';
import { PageLoading } from '@/components/loading';
import { useActiveFile } from '@/features/workspace/activeFile';

const ChatRuntimeView = dynamic(
  () =>
    import('@/features/agent/components/ChatRuntimeView').then(module => ({
      default: module.ChatRuntimeView,
    })),
  {
    ssr: false,
    loading: () => <PageLoading variant='fill' label='Loading chat' />,
  }
);

function selectWorkspaceAgent(
  savedAgents: SavedAgent[],
  currentAgentId: string | null,
) {
  const current = currentAgentId
    ? savedAgents.find(
        agent =>
          agent.id === currentAgentId &&
          agent.type === 'chat' &&
          agent.status !== 'paused',
      )
    : null;
  return (
    current ??
    savedAgents.find(
      agent => agent.type === 'chat' && agent.status !== 'paused',
    ) ??
    null
  );
}

/** Chat body hosted by the single project-level auxiliary sidebar. */
export function ProjectChatPanel({
  projectId,
  active,
  onClose,
}: {
  projectId: string;
  active: boolean;
  onClose: () => void;
}) {
  const { savedAgents, currentAgentId, selectAgent } = useAgent();
  const activeFile = useActiveFile();
  const openPanel = useProjectSession(state => state.openPanel);
  const configureAgent = () => openPanel({ type: 'access_list', view: 'overview' });
  const chatAgent = useMemo(
    () => selectWorkspaceAgent(savedAgents, currentAgentId),
    [currentAgentId, savedAgents],
  );

  useEffect(() => {
    if (!active || !chatAgent || currentAgentId === chatAgent.id) return;
    selectAgent(chatAgent.id);
  }, [active, chatAgent, currentAgentId, selectAgent]);

  return (
    <>
      {chatAgent ? (
        <ChatRuntimeView
          active={active}
          {...activeFile}
          availableTools={(chatAgent.resources ?? []).map(resource => ({
            id: `bash:${resource.path}`,
            label: `${resource.nodeName || resource.path} · Bash${resource.readonly ? ' (Read-only)' : ''}`,
            type: 'bash' as const,
            tableId: resource.path,
            tableName: resource.nodeName || resource.path,
          }))}
          projectId={projectId}
          projectTools={[]}
          onClose={onClose}
          seamlessHeader
        />
      ) : (
        <div className={chatStyles.surface}>
          <AgentChatHeader title='New chat' onClose={onClose} onNewChat={configureAgent} />
          <div className={`${chatStyles.conversation} ${chatStyles.emptyConversation}`}>
            <AgentChatEmptyState onConfigure={configureAgent} />
          </div>
          <ChatInputArea
            inputValue='' disabled isLoading={false}
            placeholder='Set up an agent to start chatting'
            onInputChange={() => {}} onKeyDown={() => {}} onSend={() => {}}
            showMentionMenu={false} filteredMentionOptions={[]} mentionIndex={0}
            onMentionSelect={() => {}} onMentionIndexChange={() => {}}
          />
        </div>
      )}
    </>
  );
}
