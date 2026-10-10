'use client';

import { useEffect, useMemo, useRef, useSyncExternalStore } from 'react';
import { useAuth } from '@/contexts/SupabaseAuthProvider';
import { useAgent } from '@/contexts/AgentContext';
import { refreshAllContentNodes, refreshProjectHistory } from '@/lib/hooks/useData';
import { ChatRuntimeView } from '../components/ChatRuntimeView';
import { AgentWorkbenchController, type WorkbenchTab, type WorkbenchState } from './controller';
import { AgentWorkbenchHeader, panelId, tabId } from './AgentWorkbenchHeader';
import { AgentHistoryBrowser } from './AgentHistoryBrowser';
import { AgentLauncher } from './AgentLauncher';
import styles from './workbench.module.css';

export function AgentWorkbench({ projectId, active, tableData, onDataUpdate, onConfigure, request }: {
  projectId: string; active: boolean; tableData?: unknown;
  request?: { agentId?: string };
  onDataUpdate?: (data: unknown) => void; onConfigure?: () => void;
}) {
  const { userId, isAuthReady } = useAuth();
  const { savedAgents, currentAgentId, selectAgent } = useAgent();
  const agents = useMemo(() => savedAgents.filter(agent => agent.type === 'chat' && agent.status !== 'paused')
    .map(agent => ({ id: agent.id, name: agent.name })), [savedAgents]);
  const store = useMemo(() => isAuthReady && userId ? new AgentWorkbenchController(userId, projectId) : null,
    [userId, isAuthReady, projectId]);
  const handledRequest = useRef<typeof request>();
  useEffect(() => { store?.setAgents(agents, request?.agentId ?? currentAgentId); }, [store, agents, currentAgentId, request?.agentId]);
  useEffect(() => {
    if (!active || !store || !request?.agentId || handledRequest.current === request) return;
    if (!agents.some(agent => agent.id === request.agentId)) return;
    handledRequest.current = request;
    store.openAgent(request.agentId);
  }, [store, active, request, agents]);
  if (!store) return <div className={styles.workbench} role='status'>Connecting to Cloud…</div>;
  return <Workbench key={`${userId}:${projectId}`} store={store} active={active} tableData={tableData}
    currentAgentId={currentAgentId} selectAgent={selectAgent} onDataUpdate={onDataUpdate} onConfigure={onConfigure} />;
}

export function Workbench({ store, active, tableData, currentAgentId, selectAgent, onDataUpdate, onConfigure }: {
  store: AgentWorkbenchController; active: boolean; tableData?: unknown;
  currentAgentId?: string | null; selectAgent?: (id: string) => void;
  onDataUpdate?: (data: unknown) => void; onConfigure?: () => void;
}) {
  const state = useSyncExternalStore(store.subscribe, store.getSnapshot, store.getSnapshot);
  const publication = useRef(onDataUpdate); publication.current = onDataUpdate;
  useEffect(() => active ? store.observe(() => {
    void refreshAllContentNodes(store.projectId); void refreshProjectHistory(store.projectId); publication.current?.(undefined);
  }) : undefined, [store, active]);
  const selected = state.tabs.find(tab => tab.id === state.activeId);
  useEffect(() => {
    if (active && selected?.kind === 'chat' && selected.agentId !== currentAgentId) selectAgent?.(selected.agentId);
  }, [active, selected, currentAgentId, selectAgent]);
  return <div className={styles.workbench}>
    <AgentWorkbenchHeader store={store} state={state} />
    <div>{state.closeError && <div className={styles.notice} role='alert'>{state.closeError}</div>}</div>
    <div className={styles.body}>
      {state.tabs.length === 0 && <AgentLauncher store={store} state={state} onConfigure={onConfigure} />}
      {state.tabs.map(tab => <div key={tab.id} id={panelId(tab.id)} role='tabpanel' aria-labelledby={tabId(tab.id)}
        className={styles.panel} hidden={tab.id !== state.activeId}>
        <TabContent tab={tab} store={store} state={state} active={active} tableData={tableData} onConfigure={onConfigure} />
      </div>)}
    </div>
  </div>;
}

function TabContent({ tab, store, state, active, tableData, onConfigure }: {
  tab: WorkbenchTab; store: AgentWorkbenchController; state: WorkbenchState;
  active: boolean; tableData?: unknown; onConfigure?: () => void;
}) {
  if (tab.kind === 'launcher') {
    if (tab.history) return <AgentHistoryBrowser store={store} state={state} launcherId={tab.id} onBack={() => store.backToLauncher(tab.id)} />;
    return <AgentLauncher store={store} state={state} tab={tab.id} onConfigure={onConfigure} />;
  }
  if (!state.agents.some(agent => agent.id === tab.agentId)) return <div className={styles.notice} role='status'>This agent is unavailable.</div>;
  return <ChatRuntimeView projectId={store.projectId} availableTools={[]} tableData={tableData}
    active={active && tab.id === state.activeId} workspace={{ controller: store.actor(tab), state: store.actor(tab).getSnapshot() }} hideHeader />;
}
