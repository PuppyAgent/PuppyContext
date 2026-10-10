'use client';

import React, { useState, useRef, useCallback } from 'react';
import BotMessage from '@/components/chat/BotMessage';
import UserMessage from '@/components/chat/UserMessage';
import ChatInputArea, {
  ChatInputAreaRef,
  type AccessOption,
} from '@/components/chat/ChatInputArea';
import { useAgentWorkspace } from '@/features/agent/runtime/useAgentWorkspace';
import type { WebAgentController, AgentWorkspaceState } from '../runtime/controller';
import { projectRunMessages } from '@/features/agent/runtime/projection';
import { runActive, runStopping } from '@/features/agent/runtime/types';
import { AgentRunNotice } from '@/features/agent/components/AgentRunNotice';
import { refreshAllContentNodes, refreshProjectHistory } from '@/lib/hooks/useData';
import { useMention } from '@/lib/hooks/useMention';
import { useAgent } from '@/contexts/AgentContext';
import { useOnboarding } from '@/lib/hooks/useOnboarding';
import { AgentChatEmptyState } from '@/components/chat/AgentChatChrome';
import chatStyles from '@/components/chat/AgentChatSurface.module.css';
import { AgentRuntimeHeader } from './AgentRuntimeHeader';
import { AgentSettingsPanel } from './AgentSettingsPanel';
import { useConversationScroll } from './useConversationScroll';

import { type Tool as DbTool } from '@/lib/mcpApi';

interface ChatRuntimeViewProps {
  availableTools: AccessOption[];
  active?: boolean;
  tableData?: unknown;
  tableId?: number | string;
  projectId?: number | string;
  onDataUpdate?: (newData: unknown) => void;
  projectTools?: DbTool[];
  onClose?: () => void;
  onBack?: () => void;
  seamlessHeader?: boolean;
  hideHeader?: boolean;
  workspace?: { controller: WebAgentController; state: AgentWorkspaceState };
}

export function ChatRuntimeView({
  availableTools,
  active,
  tableData,
  tableId,
  projectId,
  onDataUpdate,
  projectTools,
  onClose,
  onBack,
  seamlessHeader = false,
  hideHeader = false,
  workspace,
}: ChatRuntimeViewProps) {
  const { currentAgentId } = useAgent();

  const { completeStep } = useOnboarding();

  // --- Local State ---
  const [isSettingsExpanded, setIsSettingsExpanded] = useState(false);
  const inputAreaRef = useRef<ChatInputAreaRef>(null);

  const resolvedProjectId = projectId === undefined ? undefined : String(projectId);
  const handlePublication = useCallback(() => {
    if (!resolvedProjectId) return;
    void refreshAllContentNodes(resolvedProjectId);
    void refreshProjectHistory(resolvedProjectId);
    onDataUpdate?.(undefined);
  }, [resolvedProjectId, onDataUpdate]);
  const ownWorkspace = useAgentWorkspace(workspace ? undefined : resolvedProjectId, currentAgentId, handlePublication, active);
  const { controller, state } = workspace ?? ownWorkspace;
  const inputValue = state.draft;
  const setInputValue = useCallback((value: string) => controller?.setDraft(value), [controller]);
  const messages = React.useMemo(() => projectRunMessages(state.runs), [state.runs]);
  const { sessionId: currentSessionId, loading: messagesLoading } = state;
  const currentRun = state.runs.at(-1);
  const isLoading = state.submitting || runActive(currentRun);
  const mention = useMention({ data: tableData });

  const lastMessage = messages.at(-1);
  const lastContent = lastMessage?.content;
  const lastPartCount = lastMessage?.parts?.length;
  const scroll = useConversationScroll(currentSessionId ?? controller, active !== false,
    JSON.stringify([messages.length, lastContent, lastPartCount]));

  const handleSend = useCallback(async () => {
    if (!controller || !inputValue.trim() || isLoading) return;
    if (await controller.submit(inputValue)) completeStep('chat');
  }, [controller, inputValue, isLoading, completeStep]);

  // Input handling with mention
  const handleInputChange = useCallback(
    (e: React.ChangeEvent<HTMLTextAreaElement>) => {
      mention.handleInputChange(e, inputValue, setInputValue);
    },
    [mention, inputValue, setInputValue]
  );

  const handleSelectMention = useCallback(
    (key: string) => {
      mention.handleSelectMention(key, inputValue, setInputValue, inputAreaRef.current);
    },
    [mention, inputValue, setInputValue]
  );

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.nativeEvent.isComposing) return;

      if (mention.showMentionMenu && mention.filteredMentionOptions.length > 0) {
        if (e.key === 'Enter' || e.key === 'Tab') {
          e.preventDefault();
          handleSelectMention(mention.filteredMentionOptions[mention.mentionIndex]);
          return;
        }
      }
      mention.handleKeyDown(e, handleSend);
    },
    [mention, handleSelectMention, handleSend]
  );

  const isEmpty = messages.length === 0 && !messagesLoading;

  return (
    <div className={chatStyles.surface} data-seamless-header={seamlessHeader || undefined} data-embedded={hideHeader || undefined}>
      {!hideHeader && <AgentRuntimeHeader controller={controller} state={state} onClose={onClose} onBack={onBack}
        onSettings={() => setIsSettingsExpanded(value => !value)} />}

      {isSettingsExpanded && <AgentSettingsPanel />}

      <div className={chatStyles.statusRegion}><AgentRunNotice controller={controller} state={state} /></div>

      {/* The empty state and transcript share the same flexible region. */}
      <div className={`${chatStyles.conversation}${isEmpty ? ` ${chatStyles.emptyConversation}` : ''}`}>
        <Conversation messages={messages} messagesLoading={messagesLoading} isLoading={isLoading}
          pending={Boolean(state.pending)} scroll={scroll} />
      </div>

      {/* Input Area */}
      <ChatInputArea
        ref={inputAreaRef}
        inputValue={inputValue}
        onInputChange={handleInputChange}
        onKeyDown={handleKeyDown}
        onSend={handleSend}
        isLoading={isLoading}
        disabled={composerDisabled(active, controller, state)}
        onStop={currentRun && runActive(currentRun) ? () => { void controller?.stop(); } : undefined}
        stopping={runStopping(currentRun, state.resolving)}
        placeholder={messages.length ? 'Send follow-up' : 'Ask about this project'}
        showMentionMenu={mention.showMentionMenu}
        filteredMentionOptions={mention.filteredMentionOptions}
        mentionIndex={mention.mentionIndex}
        onMentionSelect={handleSelectMention}
        onMentionIndexChange={mention.setMentionIndex}
        onBlur={() => setTimeout(() => mention.closeMentionMenu(), 150)}
      />

      <style jsx global>{`
        @keyframes shimmer {
          0% {
            transform: translateX(-100%);
          }
          100% {
            transform: translateX(100%);
          }
        }
        .skeleton-shimmer {
          position: absolute;
          top: 0;
          left: 0;
          right: 0;
          bottom: 0;
          background: linear-gradient(
            90deg,
            transparent,
            var(--po-border),
            transparent
          );
          animation: shimmer 1.5s infinite;
        }
      `}</style>
    </div>
  );
}

function composerDisabled(active: boolean | undefined, controller: WebAgentController | null, state: AgentWorkspaceState) {
  return active === false || !controller || state.loading || Boolean(state.pending);
}

function Conversation({ messages, messagesLoading, isLoading, pending, scroll }: {
  messages: ReturnType<typeof projectRunMessages>; messagesLoading: boolean;
  isLoading: boolean; pending: boolean; scroll: ReturnType<typeof useConversationScroll>;
}) {
  return <>
        {messages.length === 0 && !messagesLoading ? <AgentChatEmptyState /> : (
          <div ref={scroll.viewport} onScroll={scroll.onScroll} className={chatStyles.transcript} role='log' aria-label='Chat messages' aria-busy={isLoading || messagesLoading || pending}>
            {messagesLoading ? (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 20, padding: '10px 0' }}>
                <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
                  <div
                    style={{
                      width: '100%',
                      height: 36,
                      borderRadius: 8,
                      background: 'var(--po-hover)',
                      position: 'relative',
                      overflow: 'hidden',
                    }}
                  >
                    <div className='skeleton-shimmer' />
                  </div>
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                  {[90, 75, 60].map((w, i) => (
                    <div
                      key={i}
                      style={{
                        width: `${w}%`,
                        height: 14,
                        borderRadius: 4,
                        background: 'var(--po-hover)',
                        position: 'relative',
                        overflow: 'hidden',
                      }}
                    >
                      <div className='skeleton-shimmer' />
                    </div>
                  ))}
                </div>
              </div>
            ) : (
              messages.map((msg, idx) =>
                msg.role === 'user' ? (
                  <div key={msg.id || `user-${idx}`} className={chatStyles.message}>
                    <UserMessage message={{ content: msg.content, timestamp: msg.timestamp }} showAvatar={false} />
                  </div>
                ) : (
                  <div key={msg.id || `assistant-${idx}`} className={`${chatStyles.message} ${chatStyles.assistant}`}>
                    <BotMessage message={{ role: 'assistant', content: msg.content }} parts={msg.parts} isStreaming={msg.isStreaming} />
                  </div>
                )
              )
            )}
          </div>
        )}
  </>;
}
