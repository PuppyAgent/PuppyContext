'use client';

import React, { useState, useRef, useEffect, useCallback } from 'react';
import BotMessage from '@/components/chat/BotMessage';
import UserMessage from '@/components/chat/UserMessage';
import ChatInputArea, {
  ChatInputAreaRef,
  type AccessOption,
} from '@/components/chat/ChatInputArea';
import { useAgentWorkspace } from '@/features/agent/runtime/useAgentWorkspace';
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
}: ChatRuntimeViewProps) {
  const { currentAgentId } = useAgent();

  const { completeStep } = useOnboarding();

  // --- Local State ---
  const [isSettingsExpanded, setIsSettingsExpanded] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputAreaRef = useRef<ChatInputAreaRef>(null);
  const isInitialScrollRef = useRef(true); // Track if this is initial load or agent switch

  const resolvedProjectId = projectId === undefined ? undefined : String(projectId);
  const handlePublication = useCallback(() => {
    if (!resolvedProjectId) return;
    void refreshAllContentNodes(resolvedProjectId);
    void refreshProjectHistory(resolvedProjectId);
    onDataUpdate?.(undefined);
  }, [resolvedProjectId, onDataUpdate]);
  const { controller, state } = useAgentWorkspace(resolvedProjectId, currentAgentId, handlePublication, active);
  const inputValue = state.draft;
  const setInputValue = useCallback((value: string) => controller?.setDraft(value), [controller]);
  const messages = React.useMemo(() => projectRunMessages(state.runs), [state.runs]);
  const { sessionId: currentSessionId, loading: messagesLoading } = state;
  const currentRun = state.runs.at(-1);
  const isLoading = state.submitting || runActive(currentRun);
  const mention = useMention({ data: tableData });

  useEffect(() => { isInitialScrollRef.current = true; }, [controller, currentSessionId]);

  const lastMessage = messages.at(-1);
  const lastContent = lastMessage?.content;
  const lastPartCount = lastMessage?.parts?.length;

  // Auto-scroll
  useEffect(() => {
    if (isInitialScrollRef.current) {
      // Initial load or agent switch: jump instantly without animation
      messagesEndRef.current?.scrollIntoView({ behavior: 'instant' });
      // After first scroll, use smooth scrolling for subsequent updates
      if (messages.length > 0) {
        isInitialScrollRef.current = false;
      }
    } else {
      // New message arrived: smooth scroll
      messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }
  }, [
    messages.length,
    lastContent,
    lastPartCount,
  ]);

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
    <div className={chatStyles.surface} data-seamless-header={seamlessHeader || undefined}>
      <AgentRuntimeHeader controller={controller} state={state} onClose={onClose} onBack={onBack}
        onSettings={() => setIsSettingsExpanded(value => !value)} />

      {isSettingsExpanded && <AgentSettingsPanel />}

      <AgentRunNotice controller={controller} state={state} />

      {/* The empty state and transcript share the same flexible region. */}
      <div className={`${chatStyles.conversation}${isEmpty ? ` ${chatStyles.emptyConversation}` : ''}`}>
        {isEmpty ? <AgentChatEmptyState /> : (
          <div className={chatStyles.transcript} role='log' aria-label='Chat messages' aria-busy={isLoading || state.loading || Boolean(state.pending)}>
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
            <div ref={messagesEndRef} style={{ height: 1, flexShrink: 0 }} />
          </div>
        )}
      </div>

      {/* Input Area */}
      <ChatInputArea
        ref={inputAreaRef}
        inputValue={inputValue}
        onInputChange={handleInputChange}
        onKeyDown={handleKeyDown}
        onSend={handleSend}
        isLoading={isLoading}
        disabled={!controller || state.loading || Boolean(state.pending)}
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
