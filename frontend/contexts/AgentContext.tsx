'use client';

import React, { createContext, useContext, useState, useCallback, useEffect, type ReactNode } from 'react';
import { type SavedAgent, type AgentType, type TriggerType, type TriggerConfig, type ExternalConfig } from '@/components/AgentRail';
import { post, get, put, del } from '@/lib/apiClient';
import { bootstrapSynchronizeBindings } from '@/lib/synchronizeApi';
import { stat } from '@/lib/contentTreeApi';

/**
 * Sidebar state machine:
 *
 *   closed ──[+ Access]──▸ setting ──[Save]──▸ deployed
 *     ▴                        │                  │
 *     └───[close]──────────────┘                  │
 *                                                 │
 *   deployed ──[⚙️ edit]──▸ editing ──[Save]──▸ deployed
 *       ▴                      │
 *       └───[cancel / X]───────┘
 *
 *   From ANY state:
 *     [+ Access]  → setting  (interrupts editing / deployed)
 *     [agent chip] → deployed (selects that agent)
 *
 * 'setting'  — creating a brand-new access point  (+ Access highlighted)
 * 'editing'  — editing an existing access point   (agent chip highlighted)
 * 'deployed' — viewing agent runtime              (agent chip highlighted)
 * 'closed'   — sidebar hidden
 */
export type SidebarMode = 'closed' | 'setting' | 'editing' | 'deployed';

interface NodeInfo {
  id: string;
  name: string;
  type: 'folder' | 'json' | 'markdown' | 'image' | 'pdf' | 'video' | 'file';
}

async function fetchNodeInfoBatch(nodeIds: string[], projectId: string): Promise<Map<string, NodeInfo>> {
  const nodeMap = new Map<string, NodeInfo>();
  if (nodeIds.length === 0 || !projectId) return nodeMap;

  const uniqueIds = [...new Set(nodeIds)];

  const results = await Promise.allSettled(
    uniqueIds.map(async (nodeId) => {
      try {
        const s = await stat(projectId, nodeId);
        if (s.exists) {
          return { id: nodeId, name: s.name, type: s.type as NodeInfo['type'] };
        }
        return null;
      } catch {
        return null;
      }
    })
  );
  for (const result of results) {
    if (result.status === 'fulfilled' && result.value) {
      const node = result.value;
      nodeMap.set(node.id, {
        id: node.id,
        name: node.name,
        type: node.type,
      });
    }
  }

  return nodeMap;
}

/**
 * 将后端节点类型映射到前端 AccessResource 的 nodeType
 */
function mapNodeType(backendType: string): 'folder' | 'json' | 'file' {
  if (backendType === 'folder') return 'folder';
  if (backendType === 'json') return 'json';
  return 'file'; // markdown, image, pdf, video, file 等都归类为 'file'
}

// Bash 访问资源模型（新版简化结构）
export interface AccessResource {
  path: string;
  nodeName: string;
  nodeType: 'folder' | 'json' | 'file';
  readonly: boolean;
}

interface AgentContextValue {
  // Agent 状态
  savedAgents: SavedAgent[];
  currentAgentId: string | null; 
  hoveredAgentId: string | null;
  setHoveredAgentId: (id: string | null) => void;
  
  // 配置态状态 (Draft)
  draftType: AgentType;
  draftCapabilities: Set<string>;
  draftResources: AccessResource[];
  
  // Sync frequency mode
  draftSyncMode: 'manual' | 'scheduled';
  setDraftSyncMode: (mode: 'manual' | 'scheduled') => void;

  // Schedule Agent draft 状态
  draftTriggerType: TriggerType;
  draftTriggerConfig: TriggerConfig | null;
  draftTaskContent: string;
  draftTaskNodeId: string | null;
  draftExternalConfig: ExternalConfig | null;
  
  // 运行时状态 (Playground or Deployed)
  selectedCapabilities: Set<string>;
  
  // Actions
  refreshAgents: () => Promise<void>;
  selectAgent: (agentId: string | null) => void;
  saveAgent: (name: string, icon: string, capabilities: string[]) => void;
  deleteAgent: (agentId: string) => void;
  updateAgentInfo: (agentId: string, name: string, icon: string) => Promise<void>;
  updateAgentResources: (agentId: string, resources: AccessResource[]) => Promise<void>;
  
  openSetting: () => void;
  openSyncSetting: (provider: string, preBindResource?: AccessResource) => void;
  pendingSyncProvider: string | null;
  editAgent: (agentId: string) => void;
  editingAgentId: string | null;
  cancelSetting: () => void;
  deployAgent: (name: string, icon: string) => Promise<string | null>;
  deploySyncEndpoint: (params: {
    provider: string;
    direction: string;
    config?: Record<string, unknown>;
    credentialsRef?: string;
    syncMode?: 'manual' | 'scheduled';
    trigger?: { type: string; schedule?: string; timezone?: string };
    uiMode?: 'sidebar' | 'inline';
  }) => Promise<void>;
  setDraftType: (type: AgentType) => void;
  toggleDraftCapability: (id: string) => void;
  
  // 资源管理
  addDraftResource: (resource: AccessResource) => void;
  updateDraftResource: (path: string, updates: Partial<AccessResource>) => void;
  removeDraftResource: (path: string) => void;
  setDraftResources: (resources: AccessResource[]) => void;
  
  // Schedule Agent 新增 setters
  setDraftTriggerType: (type: TriggerType) => void;
  setDraftTriggerConfig: (config: TriggerConfig | null) => void;
  setDraftTaskContent: (content: string) => void;
  setDraftTaskNodeId: (nodeId: string | null) => void;
  setDraftExternalConfig: (config: ExternalConfig | null) => void;
  
  // Runtime Actions
  toggleCapability: (id: string) => void;
  
  // Sync sidebar
  selectedSyncId: string | null;
  selectedSyncNodeId: string | null;
  hoveredSyncNodeId: string | null;
  setHoveredSyncNodeId: (nodeId: string | null) => void;
  selectSync: (syncId: string | null, nodeId?: string | null) => void;

  setSelectedCapabilities: (caps: Set<string>) => void;
}

const AgentContext = createContext<AgentContextValue | null>(null);

interface AgentProviderProps {
  children: ReactNode;
  projectId?: string;  // 可选，用于按项目过滤 agents
}

export function AgentProvider({ children, projectId }: AgentProviderProps) {
  // 初始为空，从数据库加载
  const [savedAgents, setSavedAgents] = useState<SavedAgent[]>([]);
  const [currentAgentId, setCurrentAgentId] = useState<string | null>(null);
  const [hoveredAgentId, setHoveredAgentId] = useState<string | null>(null);
  
  // Sidebar State
  const [sidebarMode, setSidebarMode] = useState<SidebarMode>('closed');
  
  // Draft State (for Setting Mode)
  const [draftType, setDraftType] = useState<AgentType>('chat');
  const [draftCapabilities, setDraftCapabilities] = useState<Set<string>>(new Set());
  const [draftResources, setDraftResources] = useState<AccessResource[]>([]);
  const [editingAgentId, setEditingAgentId] = useState<string | null>(null);
  
  // Sync frequency mode
  const [draftSyncMode, setDraftSyncMode] = useState<'manual' | 'scheduled'>('manual');

  // Schedule Agent 新增 draft 状态
  const [draftTriggerType, setDraftTriggerType] = useState<TriggerType>('manual');
  const [draftTriggerConfig, setDraftTriggerConfig] = useState<TriggerConfig | null>(null);
  const [draftTaskContent, setDraftTaskContent] = useState<string>('');
  const [draftTaskNodeId, setDraftTaskNodeId] = useState<string | null>(null);
  const [draftExternalConfig, setDraftExternalConfig] = useState<ExternalConfig | null>(null);
  
  // Sync sidebar state
  const [selectedSyncId, setSelectedSyncId] = useState<string | null>(null);
  const [pendingSyncProvider, setPendingSyncProvider] = useState<string | null>(null);

  // Runtime State (for Deployed/Playground Mode)
  const [selectedCapabilities, setSelectedCapabilities] = useState<Set<string>>(new Set());

  // Legacy isChatOpen computed from sidebarMode (internal only)
  const isChatOpen = sidebarMode !== 'closed';

  const refreshAgents = useCallback(async () => {
    // 必须有 projectId 才能加载 agents
    if (!projectId) {
      setSavedAgents([]);
      return;
    }

    try {
      const agents = await get<Array<{
        id: string;
        name: string;
        icon: string;
        type: string;
        status?: string;
        mcp_api_key?: string;
        trigger_type?: string;
        trigger_config?: TriggerConfig;
        task_content?: string;
        task_path?: string;
        external_config?: ExternalConfig;
        bash_accesses?: Array<{
          id: string;
          path: string;
          readonly: boolean;
          // PERFORMANCE (P-5): backend now inlines node metadata to avoid a
          // second round-trip. Older backends omit these fields, in which
          // case we fall back to fetchNodeInfoBatch below.
          node_name?: string;
          node_type?: string;
        }>;
      }>>(`/api/v1/agent-config/?project_id=${projectId}`);

      const hasInlineNodeInfo = agents.every(a =>
        (a.bash_accesses || []).every(b => b.node_name !== undefined)
      );

      // Only fall back to the legacy batch resolver when the server didn't
      // inline node metadata (e.g. during a staged backend rollout).
      let nodeInfoMap = new Map<string, NodeInfo>();
      if (!hasInlineNodeInfo) {
        const allNodeIds = agents.flatMap(a => (a.bash_accesses || []).map(b => b.path));
        nodeInfoMap = await fetchNodeInfoBatch(allNodeIds, projectId);
      }

      const loadedAgents: SavedAgent[] = agents.map(a => {
        const bashAccesses = a.bash_accesses || [];

        const resources: AccessResource[] = bashAccesses.map(bash => {
          // Prefer inline node metadata; fall back to the lookup map.
          const inlineName = bash.node_name;
          const inlineType = bash.node_type;
          const nodeInfo = nodeInfoMap.get(bash.path);
          const name = inlineName ?? nodeInfo?.name;
          const type = (inlineType ?? nodeInfo?.type) as NodeInfo['type'] | undefined;
          return {
            path: bash.path,
            nodeName: name || (bash.path ? `${bash.path.substring(0, 8)}...` : 'Root'),
            nodeType: type ? mapNodeType(type) : 'folder',
            readonly: bash.readonly,
          };
        });

        return {
          id: a.id,
          name: a.name,
          icon: a.icon,
          type: (a.type as AgentType) || 'chat',
          capabilities: resources.map(r => `resource:${r.path}`),
          mcp_api_key: a.mcp_api_key,
          status: a.status === 'paused' ? 'paused' : 'active',
          // Schedule Agent 新字段
          trigger_type: (a.trigger_type as TriggerType) || 'manual',
          trigger_config: a.trigger_config,
          task_content: a.task_content,
          task_path: a.task_path,
          external_config: a.external_config,
          resources,
        };
      });

      setSavedAgents(loadedAgents);
      console.log('Loaded agents from database:', loadedAgents.length, 'for project:', projectId);
      // Auto-complete onboarding step when agents exist
      if (loadedAgents.length > 0 && typeof window !== 'undefined') {
        try {
          const KEY = 'puppyone_onboarding_v1';
          const state = JSON.parse(localStorage.getItem(KEY) || '{"hasSeenWelcome":true,"completedSteps":[],"dismissedChecklist":false}');
          if (!state.completedSteps.includes('agent')) {
            state.completedSteps.push('agent');
            localStorage.setItem(KEY, JSON.stringify(state));
          }
        } catch {}
      }
    } catch (error) {
      console.error('Failed to load agents:', error);
    }
  }, [projectId]);

  // 页面加载时从数据库获取 agents（按 project_id 过滤）
  useEffect(() => {
    refreshAgents();
  }, [refreshAgents]);

  // Select agent — callable from ANY state → always goes to deployed
  const selectAgent = useCallback((agentId: string | null) => {
    setEditingAgentId(null);
    setSelectedSyncId(null);

    if (!agentId) {
      setCurrentAgentId(null);
      setSelectedCapabilities(new Set());
      setSidebarMode('closed');
      return;
    }

    setCurrentAgentId(agentId);
    const agent = savedAgents.find(a => a.id === agentId);
    setSelectedCapabilities(new Set(agent?.capabilities ?? []));
    setSidebarMode('deployed');
  }, [savedAgents]);

  const [selectedSyncNodeId, setSelectedSyncNodeId] = useState<string | null>(null);
  const [hoveredSyncNodeId, setHoveredSyncNodeId] = useState<string | null>(null);

  const selectSync = useCallback((syncId: string | null, nodeId?: string | null) => {
    setEditingAgentId(null);
    if (!syncId) {
      setSelectedSyncId(null);
      setSelectedSyncNodeId(null);
      setSidebarMode('closed');
      return;
    }
    setCurrentAgentId(null);
    setSelectedSyncId(syncId);
    setSelectedSyncNodeId(nodeId ?? null);
    setSidebarMode('deployed');
  }, []);

  const resetDraftState = useCallback(() => {
    setEditingAgentId(null);
    setDraftType('chat');
    setDraftCapabilities(new Set());
    setDraftResources([]);
    setDraftTriggerType('manual');
    setDraftTriggerConfig(null);
    setDraftTaskContent('');
    setDraftTaskNodeId(null);
    setDraftExternalConfig(null);
  }, []);

  // Create new access — callable from ANY state
  const openSetting = useCallback(() => {
    resetDraftState();
    setPendingSyncProvider(null);
    setSidebarMode('setting');
  }, [resetDraftState]);

  const openSyncSetting = useCallback((provider: string, preBindResource?: AccessResource) => {
    resetDraftState();
    if (preBindResource) {
      setDraftResources([preBindResource]);
    }
    setPendingSyncProvider(provider);
    setSidebarMode('setting');
  }, [resetDraftState]);

  // 编辑已有 Agent
  const editAgent = useCallback(async (agentId: string) => {
    // 先从本地 state 查找
    const agent = savedAgents.find(a => a.id === agentId);
    if (agent) {
      setSidebarMode('editing'); // stays "inside" the agent, doesn't jump to new-access flow
      setEditingAgentId(agentId);
      setDraftType(agent.type || 'chat');
      setDraftCapabilities(new Set(agent.capabilities.filter(c => !c.startsWith('resource:'))));
      
      // 加载 Schedule Agent 字段
      setDraftTriggerType(agent.trigger_type || 'manual');
      setDraftTriggerConfig(agent.trigger_config || null);
      setDraftTaskContent(agent.task_content || '');
      setDraftTaskNodeId(agent.task_path || null);
      setDraftExternalConfig(agent.external_config || null);
      
      // 如果有 resources，直接使用（名称已在 loadAgents 时解析）
      if (agent.resources && agent.resources.length > 0) {
        setDraftResources(agent.resources);
      } else {
        // 尝试从后端加载
        try {
          const data = await get<{
            id: string;
            name: string;
            icon: string;
            type: string;
            bash_accesses?: Array<{
              id: string;
              path: string;
              readonly: boolean;
            }>;
          }>(`/api/v1/agent-config/${agentId}`);
          
          const bashAccesses = data.bash_accesses || [];
          
          const nodeIds = bashAccesses.map(b => b.path);
          const nodeInfoMap = await fetchNodeInfoBatch(nodeIds, projectId || '');
          
          const resources: AccessResource[] = bashAccesses.map(bash => {
            const nodeInfo = nodeInfoMap.get(bash.path);
            return {
              path: bash.path,
              nodeName: nodeInfo?.name || bash.path.substring(0, 8) + '...',
              nodeType: nodeInfo ? mapNodeType(nodeInfo.type) : 'folder',
              readonly: bash.readonly,
            };
          });
          setDraftResources(resources);
        } catch (error) {
          console.error('Failed to load agent config:', error);
          setDraftResources([]);
        }
      }
    }
  }, [savedAgents]);

  // 部署 (保存) Agent
  const deployAgent = useCallback(async (name: string, icon: string) => {
    try {
      const bashAccesses = draftResources.map(r => ({
        path: r.path,
        readonly: r.readonly ?? true,
      }));

      let agentId: string;

      if (editingAgentId) {
        await put<unknown>(`/api/v1/agent-config/${editingAgentId}`, {
          name,
          icon,
          type: draftType,
          trigger_type: draftTriggerType,
          trigger_config: draftTriggerConfig,
          task_content: draftTaskContent,
          task_path: draftTaskNodeId,
          external_config: draftExternalConfig,
        });
        await put<unknown>(`/api/v1/agent-config/${editingAgentId}/bash`, bashAccesses);
        agentId = editingAgentId;
        
        // 更新本地 state
        setSavedAgents(prev => prev.map(a => 
          a.id === editingAgentId 
            ? { 
                ...a, 
                name, 
                icon, 
                type: draftType, 
                resources: draftResources,
                trigger_type: draftTriggerType,
                trigger_config: draftTriggerConfig ?? undefined,
                task_content: draftTaskContent ?? undefined,
                task_path: draftTaskNodeId ?? undefined,
                external_config: draftExternalConfig ?? undefined,
              }
            : a
        ));
        console.log('Agent updated:', agentId);
      } else {
        // 新建模式：创建新 Agent（必须有 projectId）
        if (!projectId) {
          throw new Error('projectId is required to create agent');
        }
        const response = await post<{
          id: string;
          name: string;
          icon: string;
          type: string;
          mcp_api_key?: string;
          trigger_type?: string;
          trigger_config?: TriggerConfig;
          task_content?: string;
          task_path?: string;
          external_config?: ExternalConfig;
          bash_accesses: Array<{ id: string; path: string }>;
        }>('/api/v1/agent-config/', {
          name,
          icon,
          type: draftType,
          project_id: projectId,
          bash_accesses: bashAccesses,
          trigger_type: draftTriggerType,
          trigger_config: draftTriggerConfig,
          task_content: draftTaskContent,
          task_path: draftTaskNodeId,
          external_config: draftExternalConfig,
        });
        agentId = response.id;

        // 将 draftResources 转换为 capabilities（用于兼容旧的数据结构）
        const capabilitiesFromResources = draftResources.map(r => `resource:${r.path}`);
        
        const newAgent: SavedAgent = {
          id: response.id,
          name,
          icon,
          type: draftType,
          capabilities: [...Array.from(draftCapabilities), ...capabilitiesFromResources],
          resources: draftResources,
          mcp_api_key: response.mcp_api_key,
          trigger_type: draftTriggerType,
          trigger_config: draftTriggerConfig ?? undefined,
          task_content: draftTaskContent ?? undefined,
          task_path: draftTaskNodeId ?? undefined,
          external_config: draftExternalConfig ?? undefined,
        };
        setSavedAgents(prev => [...prev, newAgent]);
        console.log('Agent created:', agentId, 'MCP Key:', response.mcp_api_key);
      }
      
      // Switch to this agent
      setCurrentAgentId(agentId);
      setSelectedCapabilities(new Set(draftResources.map(r => `resource:${r.path}`)));
      setSidebarMode('deployed');
      setEditingAgentId(null);
      return agentId;
    } catch (error) {
      console.error('Failed to save agent:', error);
      alert('Failed to save agent. Please try again.');
      return null;
    }
  }, [draftType, draftCapabilities, draftResources, editingAgentId, draftTriggerType, draftTriggerConfig, draftTaskContent, draftTaskNodeId, draftExternalConfig, projectId]);

  // Deploy a sync endpoint (creates a sync record, NOT an agent)
  const deploySyncEndpoint = useCallback(async (params: {
    provider: string;
    direction: string;
    config?: Record<string, unknown>;
    credentialsRef?: string;
    syncMode?: 'manual' | 'scheduled';
    trigger?: { type: string; schedule?: string; timezone?: string };
    uiMode?: 'sidebar' | 'inline';
  }) => {
    if (!projectId) {
      throw new Error('projectId is required to create sync endpoint');
    }
    const targetNode = draftResources[0];
    if (!targetNode) {
      throw new Error('target node is required');
    }

    try {
      let syncId: string | null = null;
      let nodePath: string = targetNode.path;

      if (params.provider === 'mcp') {
        const result = await post<{ id: string }>('/api/v1/mcp-endpoints', {
          project_id: projectId,
          path: nodePath,
          name: (params.config?.name as string) || 'MCP Endpoint',
          description: (params.config?.description as string) || null,
          accesses: [{ path: nodePath, json_path: '', readonly: false }],
        });
        syncId = result.id || null;
      } else if (params.provider === 'sandbox') {
        const result = await post<{ id: string }>('/api/v1/sandbox-endpoints', {
          project_id: projectId,
          path: nodePath,
          name: (params.config?.name as string) || 'Sandbox',
          description: (params.config?.description as string) || null,
          mounts: [{ path: nodePath, mount_path: '/workspace', permissions: { read: true, write: true, exec: false } }],
        });
        syncId = result.id || null;
      } else {
        const triggerPayload = params.syncMode === 'scheduled' && params.trigger
          ? { type: 'scheduled', schedule: params.trigger.schedule, timezone: params.trigger.timezone }
          : params.syncMode === 'manual'
            ? { type: 'manual' }
            : undefined;

        await bootstrapSynchronizeBindings({
          project_id: projectId,
          provider: params.provider,
          config: params.config || {},
          target_path: nodePath,
          credentials_ref: params.credentialsRef,
          direction: params.direction,
          conflict_strategy: 'three_way_merge',
          sync_mode: params.syncMode || 'manual',
          trigger: triggerPayload,
        });
      }

      if (params.uiMode !== 'inline') {
        if (syncId) {
          selectSync(syncId, nodePath);
        }
        setSidebarMode('deployed');
      }
      setDraftResources([]);
      setEditingAgentId(null);
    } catch (error: any) {
      // Rethrow so the caller (SyncConfigPanel) can show inline UI
      throw error;
    }
  }, [projectId, draftResources]);

  // Legacy saveAgent (maps to deploy with current selected capabilities if possible, or simple save)
  const saveAgent = useCallback((name: string, icon: string, capabilities: string[]) => {
    const newAgent: SavedAgent = {
      id: `agent-${Date.now()}`,
      name,
      icon,
      type: 'chat', // Default to chat for legacy calls
      capabilities,
    };
    setSavedAgents(prev => [...prev, newAgent]);
    setCurrentAgentId(newAgent.id);
    setSelectedCapabilities(new Set(capabilities));
    setSidebarMode('deployed');
  }, []);

  // 删除 Agent
  const deleteAgent = useCallback(async (agentId: string) => {
    try {
      // 调用后端 API 删除
      await del(`/api/v1/agent-config/${agentId}`);
      
      // 更新前端状态
      setSavedAgents(prev => {
        const filtered = prev.filter(a => a.id !== agentId);
        // 如果删除的是当前 agent，切换到第一个或关闭
        if (currentAgentId === agentId) {
          if (filtered.length > 0) {
            selectAgent(filtered[0].id);
          } else {
            selectAgent(null);
            setSidebarMode('closed');
          }
        }
        return filtered;
      });
      
    } catch (error) {
      console.error('Failed to delete agent:', error);
      alert('Failed to delete agent. Please try again.');
    }
  }, [currentAgentId, selectAgent]);

  // 更新 Agent 名字和图标
  const updateAgentInfo = useCallback(async (agentId: string, name: string, icon: string) => {
    try {
      // 调用后端 API 更新
      await put(`/api/v1/agent-config/${agentId}`, { name, icon });
      
      // 更新前端状态
      setSavedAgents(prev => prev.map(a => 
        a.id === agentId ? { ...a, name, icon } : a
      ));
      
    } catch (error) {
      console.error('Failed to update agent info:', error);
      alert('Failed to update agent. Please try again.');
    }
  }, []);

  // 更新 Agent 资源权限（从 ChatRuntimeView 的设置面板调用）
  const updateAgentResources = useCallback(async (agentId: string, resources: AccessResource[]) => {
    try {
      // 构建后端需要的 bash 数据
      const bashAccesses = resources.map(r => ({
        path: r.path,
        readonly: r.readonly ?? true,
      }));
      
      // 调用后端 API 更新资源权限
      await put<unknown>(`/api/v1/agent-config/${agentId}/bash`, bashAccesses);
      
      // 更新前端状态
      setSavedAgents(prev => prev.map(a => 
        a.id === agentId ? { ...a, resources } : a
      ));
      
      console.log('Agent resources updated:', agentId, resources.length, 'resources');
    } catch (error) {
      console.error('Failed to update agent resources:', error);
      throw error; // 让调用者处理错误
    }
  }, []);

  // Cancel / X — go back to the previous sensible state
  const cancelSetting = useCallback(() => {
    if (sidebarMode === 'editing' && editingAgentId) {
      // Editing → back to deployed view of the same agent
      setEditingAgentId(null);
      setCurrentAgentId(editingAgentId);
      setSidebarMode('deployed');
    } else if (sidebarMode === 'setting' && currentAgentId) {
      // Creating new but an agent was already selected → back to it
      setSidebarMode('deployed');
    } else {
      // Creating new with nothing selected → close
      setSidebarMode('closed');
    }
  }, [sidebarMode, editingAgentId, currentAgentId]);

  // Close Sidebar (internal — used by cancelSetting workflow)
  const closeSidebar = useCallback(() => {
    setSidebarMode('closed');
    setSelectedSyncId(null);
    setSelectedSyncNodeId(null);
  }, []);

  // Toggle Draft Capability
  const toggleDraftCapability = useCallback((id: string) => {
    setDraftCapabilities(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  // 新：添加资源
  const addDraftResource = useCallback((resource: AccessResource) => {
    setDraftResources(prev => {
      if (prev.some(r => r.path === resource.path)) {
        return prev;
      }
      return [...prev, resource];
    });
  }, []);

  const updateDraftResource = useCallback((path: string, updates: Partial<AccessResource>) => {
    setDraftResources(prev => 
      prev.map(r => r.path === path ? { ...r, ...updates } : r)
    );
  }, []);

  const removeDraftResource = useCallback((path: string) => {
    setDraftResources(prev => prev.filter(r => r.path !== path));
  }, []);

  // Toggle Runtime Capability
  const toggleCapability = useCallback((id: string) => {
    setSelectedCapabilities(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      
      return next;
    });
  }, [currentAgentId]);

  return (
    <AgentContext.Provider
      value={{
        savedAgents,
        currentAgentId,
        hoveredAgentId,
        setHoveredAgentId,
        draftType,
        draftCapabilities,
        draftResources,
        selectedCapabilities,
        refreshAgents,
        
        // Sync frequency mode
        draftSyncMode,
        setDraftSyncMode,

        // Schedule Agent draft states
        draftTriggerType,
        draftTriggerConfig,
        draftTaskContent,
        draftTaskNodeId,
        draftExternalConfig,
        
        selectedSyncId,
        selectedSyncNodeId,
        hoveredSyncNodeId,
        setHoveredSyncNodeId,
        selectSync,
        selectAgent,
        openSetting,
        openSyncSetting,
        pendingSyncProvider,
        editAgent,
        editingAgentId,
        cancelSetting,
        deployAgent,
        deploySyncEndpoint,
        saveAgent,
        deleteAgent,
        updateAgentInfo,
        updateAgentResources,
        
        setDraftType,
        toggleDraftCapability,
        addDraftResource,
        updateDraftResource,
        removeDraftResource,
        setDraftResources,
        toggleCapability,
        
        // Schedule Agent setters
        setDraftTriggerType,
        setDraftTriggerConfig,
        setDraftTaskContent,
        setDraftTaskNodeId,
        setDraftExternalConfig,
        
        setSelectedCapabilities,
      }}
    >
      {children}
    </AgentContext.Provider>
  );
}

export function useAgent() {
  const context = useContext(AgentContext);
  if (!context) {
    throw new Error('useAgent must be used within an AgentProvider');
  }
  return context;
}

// 导出类型供其他组件使用
export type { SavedAgent, AgentType, TriggerType, TriggerConfig, ExternalConfig } from '@/components/AgentRail';
