import { WebAgentClient } from '../runtime/client';
import { WebAgentController } from '../runtime/controller';
import { EMPTY_SAVED, readConversation } from '../runtime/persistence';
import type { AgentSession } from '../runtime/types';

export type WorkbenchAgent = { id: string; name: string };
export type ChatTab = { id: string; kind: 'chat'; agentId: string };
export type LauncherTab = { id: string; kind: 'launcher'; history: boolean };
export type WorkbenchTab = ChatTab | LauncherTab;
export type WorkbenchState = {
  tabs: WorkbenchTab[]; activeId: string | null; agents: WorkbenchAgent[];
  sessions: AgentSession[]; historyLoading: boolean; historyError: string | null;
  historyLimited: boolean; closeError: string | null;
};

/** Owns tab identity and observers; each tab owns an independent durable-run actor.
 * Switching/closing a view never sends Stop or creates a remote chat/sandbox.
 */
export class AgentWorkbenchController {
  private listeners = new Set<() => void>();
  private actors = new Map<string, WebAgentController>();
  private subscriptions = new Map<string, () => void>();
  private observations = new Map<string, () => void>();
  private onPublication: (() => void) | null = null;
  private historyRequest: AbortController | null = null;
  private initialized: boolean;
  private key: string;
  private state: WorkbenchState;

  constructor(readonly userId: string, readonly projectId: string,
    private clientFor = (agentId: string) => new WebAgentClient(projectId, agentId)) {
    this.key = JSON.stringify(['web-agent-workbench-v1', userId, projectId]);
    let tabs: WorkbenchTab[] = [], activeId: string | null = null;
    this.initialized = false;
    try {
      const raw = sessionStorage.getItem(this.key);
      if (raw) {
        const saved = JSON.parse(raw);
        const ids = new Set<string>();
        tabs = Array.isArray(saved.tabs) ? saved.tabs.filter((tab: WorkbenchTab) => {
          if (!tab || typeof tab.id !== 'string' || ids.has(tab.id)) return false;
          if (tab.kind !== 'launcher' && (tab.kind !== 'chat' || typeof tab.agentId !== 'string')) return false;
          ids.add(tab.id); return true;
        }) : [];
        activeId = tabs.some(tab => tab.id === saved.activeId) ? saved.activeId : tabs[0]?.id ?? null;
        this.initialized = true;
      }
    } catch { /* Storage may be unavailable; the workbench remains usable. */ }
    this.state = { tabs, activeId, agents: [], sessions: [], historyLoading: false,
      historyError: null, historyLimited: false, closeError: null };
  }

  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  getSnapshot = () => this.state;
  private patch(update: Partial<WorkbenchState> = {}) {
    this.state = { ...this.state, ...update };
    for (const listener of this.listeners) listener();
  }
  private save() {
    try { sessionStorage.setItem(this.key, JSON.stringify({ tabs: this.state.tabs, activeId: this.state.activeId })); }
    catch { /* No loss of in-memory receipts when browser storage is full. */ }
  }
  setAgents = (agents: WorkbenchAgent[], preferredId?: string | null) => {
    const catalogBecameAvailable = this.state.agents.length === 0 && agents.length > 0;
    this.patch({ agents });
    if (!this.initialized && agents.length) {
      this.initialized = true;
      const agent = agents.find(row => row.id === preferredId) ?? agents[0];
      const legacy = readConversation(JSON.stringify(['web-cloud-agent-v1', this.userId, this.projectId, agent.id]), agent.id,
        { ...EMPTY_SAVED, newChat: true });
      this.addChat(agent.id, legacy.sessionId, undefined, legacy);
    }
    for (const tab of this.state.tabs) {
      if (tab.kind === 'chat' && agents.some(agent => agent.id === tab.agentId)) this.actor(tab);
    }
    this.syncObservations();
    if (catalogBecameAvailable && this.onPublication && this.historyVisible()) void this.refreshHistory();
  };
  actor(tab: ChatTab) {
    let actor = this.actors.get(tab.id);
    if (actor) return actor;
    actor = new WebAgentController(this.clientFor(tab.agentId), `${this.key}:${tab.id}`, {
      initial: { ...EMPTY_SAVED, newChat: true },
      sessions: () => this.state.sessions.filter(row => row.agent_id === tab.agentId),
    });
    this.attach(tab, actor);
    return actor;
  }
  private attach(tab: ChatTab, actor: WebAgentController) {
    this.actors.set(tab.id, actor);
    this.subscriptions.set(tab.id, actor.subscribe(() => {
      const snapshot = actor.getSnapshot();
      const session = snapshot.sessions.find(row => row.id === snapshot.sessionId);
      const sessions = session ? [session, ...this.state.sessions.filter(row => row.id !== session.id)] : this.state.sessions;
      this.patch({ sessions });
    }));
  }
  private addChat(agentId: string, sessionId: string | null, replaceId?: string,
    initial = { ...EMPTY_SAVED, sessionId, newChat: !sessionId }) {
    const tab: ChatTab = { id: crypto.randomUUID(), kind: 'chat', agentId };
    const actor = new WebAgentController(this.clientFor(agentId), `${this.key}:${tab.id}`, {
      initial, sessions: () => this.state.sessions.filter(row => row.agent_id === agentId),
    });
    // Persist the initial locator even if the view is reloaded before observation.
    actor.setDraft(initial.draft);
    this.attach(tab, actor);
    const replace = this.state.tabs.find(row => row.id === replaceId && row.kind === 'launcher');
    this.patch({ tabs: replace ? this.state.tabs.map(row => row.id === replaceId ? tab : row) : [...this.state.tabs, tab],
      activeId: tab.id, closeError: null });
    this.save(); this.syncObservations();
    return tab.id;
  }
  newChat = (agentId: string, replaceId?: string) => {
    if (!this.state.agents.some(agent => agent.id === agentId)) return;
    return this.addChat(agentId, null, replaceId);
  };
  openAgent = (agentId: string) => {
    const open = this.state.tabs.find(tab => tab.id === this.state.activeId && tab.kind === 'chat' && tab.agentId === agentId)
      ?? this.state.tabs.find(tab => tab.kind === 'chat' && tab.agentId === agentId);
    if (open) { this.activate(open.id); return open.id; }
    return this.newChat(agentId);
  };
  newTab = () => {
    const tab: LauncherTab = { id: crypto.randomUUID(), kind: 'launcher', history: false };
    this.patch({ tabs: [...this.state.tabs, tab], activeId: tab.id, closeError: null }); this.save();
    return tab.id;
  };
  activate = (id: string) => {
    if (this.state.tabs.some(tab => tab.id === id)) { this.patch({ activeId: id, closeError: null }); this.save(); }
  };
  close = (id: string) => {
    const state = this.actors.get(id)?.getSnapshot();
    if (state?.submitting || state?.pending) {
      this.patch({ activeId: id, closeError: 'Confirm the pending message before closing this tab.' }); return false;
    }
    const index = this.state.tabs.findIndex(tab => tab.id === id);
    if (index < 0) return false;
    const tabs = this.state.tabs.filter(tab => tab.id !== id);
    this.observations.get(id)?.(); this.observations.delete(id);
    this.subscriptions.get(id)?.(); this.subscriptions.delete(id); this.actors.delete(id);
    try { sessionStorage.removeItem(`${this.key}:${id}`); } catch { /* In-memory close remains available. */ }
    this.patch({ tabs, activeId: this.state.activeId === id ? (tabs[index] ?? tabs[index - 1])?.id ?? null : this.state.activeId, closeError: null });
    this.save(); return true;
  };
  move = (id: string, beforeId: string) => {
    if (id === beforeId) return;
    const tab = this.state.tabs.find(row => row.id === id);
    if (!tab || !this.state.tabs.some(row => row.id === beforeId)) return;
    const tabs = this.state.tabs.filter(row => row.id !== id);
    tabs.splice(tabs.findIndex(row => row.id === beforeId), 0, tab);
    this.patch({ tabs }); this.save();
  };
  showHistory = (id?: string) => {
    const tabId = id ?? this.newTab();
    this.patch({ tabs: this.state.tabs.map(tab => tab.id === tabId && tab.kind === 'launcher' ? { ...tab, history: true } : tab) });
    this.save(); void this.refreshHistory();
  };
  backToLauncher = (id: string) => {
    this.patch({ tabs: this.state.tabs.map(tab => tab.id === id && tab.kind === 'launcher' ? { ...tab, history: false } : tab) }); this.save();
  };
  openSession = (session: AgentSession, launcherId?: string) => {
    if (!this.state.agents.some(agent => agent.id === session.agent_id)) return;
    const open = this.state.tabs.find(tab => tab.kind === 'chat' && tab.agentId === session.agent_id && this.actor(tab).getSnapshot().sessionId === session.id);
    if (open) { this.activate(open.id); return open.id; }
    return this.addChat(session.agent_id, session.id, launcherId);
  };
  refreshHistory = async () => {
    if (this.historyRequest || !this.state.agents.length) return;
    const request = new AbortController(); this.historyRequest = request;
    this.patch({ historyLoading: true, historyError: null });
    try {
      const pages = await Promise.all(this.state.agents.map(agent => this.clientFor(agent.id).sessions(request.signal)));
      if (request.signal.aborted) return;
      // A just-confirmed submission may not have been present at query start.
      const merged = new Map(this.state.sessions.map(row => [row.id, row]));
      for (const row of pages.flat()) merged.set(row.id, row);
      this.patch({ sessions: [...merged.values()].sort((a, b) => b.updated_at.localeCompare(a.updated_at)),
        historyLimited: pages.some(page => page.length === 200) });
    } catch (cause) {
      if (!request.signal.aborted) this.patch({ historyError: cause instanceof Error ? cause.message : 'Could not load chat history.' });
    } finally {
      if (this.historyRequest === request) { this.historyRequest = null; this.patch({ historyLoading: false }); }
    }
  };
  private historyVisible() {
    return this.state.tabs.some(tab => tab.id === this.state.activeId && tab.kind === 'launcher' && tab.history);
  }
  private syncObservations() {
    if (!this.onPublication) return;
    for (const [id, stop] of this.observations) {
      const tab = this.state.tabs.find(row => row.id === id);
      if (tab?.kind === 'chat' && this.state.agents.some(agent => agent.id === tab.agentId)) continue;
      stop(); this.observations.delete(id);
    }
    for (const tab of this.state.tabs) {
      if (tab.kind !== 'chat' || this.observations.has(tab.id) || !this.state.agents.some(agent => agent.id === tab.agentId)) continue;
      this.observations.set(tab.id, this.actor(tab).observe(() => this.onPublication?.()));
    }
  }
  observe(onPublication: () => void) {
    this.onPublication = onPublication; this.syncObservations();
    if (this.historyVisible()) void this.refreshHistory();
    return () => {
      this.onPublication = null;
      for (const stop of this.observations.values()) stop();
      this.observations.clear(); this.historyRequest?.abort();
    };
  }
}
