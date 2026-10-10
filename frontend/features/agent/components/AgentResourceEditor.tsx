'use client';

import { useAgent, type AccessResource } from '@/contexts/AgentContext';

export function AgentResourceEditor() {
  const { draftResources, addDraftResource } = useAgent();
  return <>
          {/* Agent's bash access - 和 AgentSettingView 保持一致 */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 4, marginBottom: 6 }}>
            <span style={{ fontSize: 13, fontWeight: 500, color: 'var(--po-text-subtle)' }}>{"Agent's bash access"}</span>
          </div>
          <div
            style={{
              minHeight: 88,
              background: 'transparent',
              border: '1px dashed var(--po-border)',
              borderRadius: 6,
              transition: 'all 0.15s',
            }}
            onDragOver={(e) => {
              e.preventDefault();
              e.currentTarget.style.borderColor = 'var(--po-success)';
              e.currentTarget.style.background = 'color-mix(in srgb, var(--po-success) 4%, transparent)';
            }}
            onDragLeave={(e) => {
              e.currentTarget.style.borderColor = 'var(--po-border)';
              e.currentTarget.style.background = 'transparent';
            }}
            onDrop={(e) => {
              e.preventDefault();
              e.currentTarget.style.borderColor = 'var(--po-border)';
              e.currentTarget.style.background = 'transparent';
              try {
                const data = e.dataTransfer.getData('application/json');
                if (data) {
                  const node = JSON.parse(data);
                  let nodeType: 'folder' | 'json' | 'file' = 'file';
                  if (node.type === 'folder') nodeType = 'folder';
                  if (node.type === 'json') nodeType = 'json';
                  addDraftResource({
                    path: node.nodeId || node.id,
                    nodeName: node.name,
                    nodeType,
                    readonly: false, // 默认 Write 模式
                  });
                }
              } catch (err) {
                console.error('Drop failed', err);
              }
            }}
          >
            {/* 文件列表 */}
            <div style={{ padding: draftResources.length > 0 ? 6 : 0, display: 'flex', flexDirection: 'column', gap: 4 }}>
              {draftResources.map(resource => <AgentResourceRow key={resource.path} resource={resource} />)}
            </div>

            {/* 拖拽提示 */}
            <div style={{
              minHeight: draftResources.length > 0 ? 32 : 88,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              color: 'var(--po-text-disabled)',
            }}>
              <span style={{ fontSize: 12 }}>
                {draftResources.length > 0 ? 'Drag more' : 'Drag items into this'}
              </span>
            </div>
          </div>


  </>;
}

function AgentResourceRow({ resource }: { resource: AccessResource }) {
  const { updateDraftResource, removeDraftResource } = useAgent();
  const isReadonly = resource.readonly ?? true;
  return (
<div
                    key={resource.path}
                    style={{
                      height: 32,
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'space-between',
                      padding: '0 10px',
                      borderRadius: 4,
                      background: 'var(--po-panel-raised)',
                      border: '1px solid var(--po-border-strong)',
                      transition: 'all 0.1s',
                    }}
                    onMouseEnter={e => { e.currentTarget.style.background = 'var(--po-hover)'; e.currentTarget.style.borderColor = 'var(--po-border-strong)'; }}
                    onMouseLeave={e => { e.currentTarget.style.background = 'var(--po-panel-raised)'; e.currentTarget.style.borderColor = 'var(--po-border-strong)'; }}
                  >
                    {/* 左侧：名称 */}
                    <span style={{ fontSize: 14, color: 'var(--po-text)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', flex: 1, minWidth: 0 }}>
                      {resource.nodeName}
                    </span>

                    {/* 右侧：权限切换 + 删除 */}
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0 }}>
                      {/* Segmented Control: Read | Write */}
                      <div style={{
                        display: 'flex',
                        background: 'var(--po-panel)',
                        border: '1px solid var(--po-border)',
                        borderRadius: 4,
                        padding: 2,
                        gap: 1,
                      }}>
                        <button
                          onClick={() => updateDraftResource(resource.path, { readonly: true })}
                          style={{
                            background: isReadonly ? 'var(--po-border-strong)' : 'transparent',
                            border: 'none',
                            borderRadius: 3,
                            color: isReadonly ? 'var(--po-text)' : 'var(--po-text-disabled)',
                            cursor: 'pointer',
                            fontSize: 11,
                            height: 30,
                            padding: '0 8px',
                            fontWeight: 500,
                            transition: 'all 0.1s',
                          }}
                        >
                          Read
                        </button>
                        <button
                          onClick={() => updateDraftResource(resource.path, { readonly: false })}
                          style={{
                            background: !isReadonly ? 'color-mix(in srgb, var(--po-warning) 15%, transparent)' : 'transparent',
                            border: 'none',
                            borderRadius: 3,
                            color: !isReadonly ? 'var(--po-warning)' : 'var(--po-text-disabled)',
                            cursor: 'pointer',
                            fontSize: 11,
                            height: 30,
                            padding: '0 8px',
                            fontWeight: 500,
                            transition: 'all 0.1s',
                          }}
                        >
                          Write
                        </button>
                      </div>

                      <button
                        onClick={() => removeDraftResource(resource.path)}
                        style={{
                          display: 'flex', alignItems: 'center', justifyContent: 'center',
                          width: 20, height: 20, borderRadius: 4,
                          background: 'transparent',
                          border: 'none',
                          color: 'var(--po-text-disabled)',
                          cursor: 'pointer',
                          transition: 'all 0.1s',
                        }}
                        onMouseEnter={e => { e.currentTarget.style.background = 'var(--po-border)'; e.currentTarget.style.color = 'var(--po-danger)'; }}
                        onMouseLeave={e => { e.currentTarget.style.background = 'transparent'; e.currentTarget.style.color = 'var(--po-text-disabled)'; }}
                      >
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                          <line x1="18" y1="6" x2="6" y2="18"></line>
                          <line x1="6" y1="6" x2="18" y2="18"></line>
                        </svg>
                      </button>
                    </div>
                  </div>
  );
}
