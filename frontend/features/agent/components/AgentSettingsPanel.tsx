'use client';

import React, { useCallback, useEffect, useState } from 'react';
import { useAgent } from '@/contexts/AgentContext';
import { AgentChatBrand } from '@/components/chat/AgentChatChrome';
import { Dots } from '@/components/loading';
import chatStyles from '@/components/chat/AgentChatSurface.module.css';
import { AgentResourceEditor } from './AgentResourceEditor';

export function AgentSettingsPanel() {
  const { currentAgentId, savedAgents, draftResources, setDraftResources,
    deleteAgent, updateAgentInfo, updateAgentResources } = useAgent();
  const currentAgent = savedAgents.find(agent => agent.id === currentAgentId);
  // 编辑 agent 信息
  const [editingName, setEditingName] = useState('');

  // 保存资源配置的状态
  const [isSavingResources, setIsSavingResources] = useState(false);

  // 检测资源是否有更改
  const hasResourceChanges = React.useMemo(() => {
    if (!currentAgent?.resources) return draftResources.length > 0;
    if (draftResources.length !== currentAgent.resources.length) return true;
    return draftResources.some((draft, i) => {
      const original = currentAgent.resources![i];
      return draft.path !== original.path ||
             (draft.readonly ?? true) !== (original.readonly ?? true);
    });
  }, [draftResources, currentAgent?.resources]);

  // 保存资源权限
  const handleSaveResources = useCallback(async () => {
    if (!currentAgentId || !hasResourceChanges) return;
    setIsSavingResources(true);
    try {
      await updateAgentResources(currentAgentId, draftResources);
    } catch (error) {
      console.error('Failed to save resources:', error);
      alert('Failed to save resource permissions. Please try again.');
    } finally {
      setIsSavingResources(false);
    }
  }, [currentAgentId, draftResources, hasResourceChanges, updateAgentResources]);

  // 当展开设置面板时，初始化编辑值
  useEffect(() => {
    if (currentAgent) {
      setEditingName(currentAgent.name);
      // 同步资源数据到 draftResources，以便编辑
      if (currentAgent.resources) {
        setDraftResources([...currentAgent.resources]);
      } else {
        setDraftResources([]);
      }
    }
  }, [currentAgent, setDraftResources]);


  if (!currentAgent) return null;
  return (
<div className={chatStyles.settings} style={{
          padding: '12px 16px',
          background: 'var(--po-panel-raised)',
          borderBottom: '1px solid var(--po-border-subtle)',
          display: 'flex',
          flexDirection: 'column',
          gap: 12,
        }}>
          {/* 编辑名字和图标 */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            {/* Type icon — fixed, non-editable */}
            <span style={{
              width: 32, height: 32, borderRadius: '50%',
              background: 'var(--po-panel)', border: '1px solid var(--po-border)',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              flexShrink: 0, color: 'var(--po-text-subtle)',
            }}>
              <AgentChatBrand />
            </span>

            {/* 名字输入 */}
            <input
              type="text"
              value={editingName}
              onChange={(e) => setEditingName(e.target.value)}
              style={{
                flex: 1,
                height: 32,
                background: 'var(--po-panel)',
                border: '1px solid var(--po-border)',
                borderRadius: 4,
                padding: '0 10px',
                color: 'var(--po-text)',
                fontSize: 14,
                outline: 'none',
              }}
              onFocus={e => e.currentTarget.style.borderColor = 'var(--po-success)'}
              onBlur={e => e.currentTarget.style.borderColor = 'var(--po-border)'}
            />

            {/* 保存按钮 */}
            <button
              onClick={async () => {
                if (currentAgentId && editingName.trim()) {
                  await updateAgentInfo(currentAgentId, editingName.trim(), currentAgent.icon || '');
                }
              }}
              disabled={!editingName.trim() || editingName === currentAgent.name}
              style={{
                height: 32,
                padding: '0 12px',
                background: editingName.trim() && editingName !== currentAgent.name ? 'var(--po-success)' : 'var(--po-border)',
                color: editingName.trim() && editingName !== currentAgent.name ? 'var(--po-text-inverse)' : 'var(--po-text-disabled)',
                border: 'none',
                borderRadius: 4,
                cursor: editingName.trim() && editingName !== currentAgent.name ? 'pointer' : 'not-allowed',
                fontSize: 13,
                fontWeight: 500,
                transition: 'all 0.15s',
              }}
            >
              Save
            </button>
          </div>

          <AgentResourceEditor />

          {/* Save Resources Button - 只在有更改时显示 */}
          {hasResourceChanges && (
            <button
              onClick={handleSaveResources}
              disabled={isSavingResources}
              style={{
                marginTop: 4,
                height: 30,
                padding: '0 12px',
                background: isSavingResources ? 'var(--po-border)' : 'var(--po-success)',
                border: 'none',
                borderRadius: 4,
                color: isSavingResources ? 'var(--po-text-disabled)' : 'var(--po-text-inverse)',
                fontSize: 12,
                fontWeight: 500,
                cursor: isSavingResources ? 'not-allowed' : 'pointer',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                gap: 6,
                transition: 'all 0.15s',
              }}
            >
              {isSavingResources ? (
                <>
                  <Dots size="xs" />
                  Saving…
                </>
              ) : (
                <>
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <polyline points="20 6 9 17 4 12"></polyline>
                  </svg>
                  Save Changes
                </>
              )}
            </button>
          )}

          {/* Delete Button */}
          <button
            onClick={() => {
              if (confirm(`Delete "${currentAgent.name}"? This cannot be undone.`)) {
                deleteAgent(currentAgent.id);
              }
            }}
            style={{
              marginTop: 4,
              height: 30,
              padding: '0 10px',
              background: 'transparent',
              border: '1px solid var(--po-border)',
              borderRadius: 4,
              color: 'var(--po-text-disabled)',
              fontSize: 11,
              cursor: 'pointer',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              gap: 6,
              transition: 'all 0.15s',
            }}
            onMouseEnter={e => {
              e.currentTarget.style.borderColor = 'var(--po-danger)';
              e.currentTarget.style.color = 'var(--po-danger)';
              e.currentTarget.style.background = 'color-mix(in srgb, var(--po-danger) 8%, transparent)';
            }}
            onMouseLeave={e => {
              e.currentTarget.style.borderColor = 'var(--po-border)';
              e.currentTarget.style.color = 'var(--po-text-disabled)';
              e.currentTarget.style.background = 'transparent';
            }}
          >
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <polyline points="3 6 5 6 21 6"></polyline>
              <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
            </svg>
            Delete Access
          </button>
        </div>
  );
}
