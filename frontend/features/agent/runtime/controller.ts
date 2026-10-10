import { WebAgentClient } from './client';
import { readConversation, saveConversation, type SavedConversation } from './persistence';
import { AgentProtocolError, runActive, type AgentRun, type AgentSession, type Submission, type AgentEvent } from './types';

export type AgentWorkspaceState = SavedConversation & {
  draft: string; loading: boolean; submitting: boolean; resolving: string | null;
  runs: AgentRun[]; sessions: AgentSession[]; error: Error | null; reconnecting: boolean; hasEarlier: boolean;
};
export const EMPTY_STATE: AgentWorkspaceState = { sessionId: null, pending: null, newChat: false,
  draft: '', loading: false, submitting: false, resolving: null, runs: [], sessions: [], error: null,
  reconnecting: false, hasEarlier: false };
const asError = (value: unknown) => value instanceof Error ? value : new Error(String(value));
const blocked = (error: unknown) => error instanceof AgentProtocolError || [401, 403, 404].includes(status(error) ?? 0);
const status = (error: unknown) => (error as { status?: number } | null)?.status;
function delay(ms: number, signal: AbortSignal) {
  return new Promise<void>(resolve => {
    if (signal.aborted) { resolve(); return; }
    const finish = () => { clearTimeout(timer); signal.removeEventListener('abort', finish); resolve(); };
    const timer = setTimeout(finish, ms);
    signal.addEventListener('abort', finish, { once: true });
  });
}

/** One actor/project/agent owns receipts; mounting a view only observes execution. */
export class WebAgentController {
  private listeners = new Set<() => void>();
  private revision = 0;
  private observer: AbortController | null = null;
  private published = new Set<string>();
  private decisions = new Map<string, { id: string; allow: boolean }>();
  private state: AgentWorkspaceState;
  constructor(readonly client: WebAgentClient, private storageKey: string, private options: {
    initial?: SavedConversation;
    sessions?: () => AgentSession[];
  } = {}) {
    const saved = readConversation(storageKey, client.agentId, options.initial);
    this.state = { ...EMPTY_STATE, ...saved, draft: saved.draft, loading: true };
  }
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  getSnapshot = () => this.state;
  private patch(values: Partial<AgentWorkspaceState>) {
    this.state = { ...this.state, ...values };
    for (const listener of this.listeners) listener();
  }
  private save() {
    const { sessionId, pending, newChat, draft } = this.state;
    saveConversation(this.storageKey, { sessionId, pending, newChat, draft });
  }
  setDraft = (draft: string) => { this.patch({ draft }); this.save(); };
  private accept(run: AgentRun) {
    this.client.owned(run);
    if (run.session_id !== this.state.sessionId) throw new AgentProtocolError('Cloud Agent returned a different session.');
    const old = this.state.runs.find(row => row.id === run.id);
    if (old && old.sequence > run.sequence) return;
    this.patch({ runs: [...this.state.runs.filter(row => row.id !== run.id), run]
      .sort((a, b) => a.created_at.localeCompare(b.created_at) || a.id.localeCompare(b.id)) });
  }
  async load(signal?: AbortSignal) {
    const revision = this.revision;
    const alive = () => revision === this.revision && !signal?.aborted;
    this.patch({ loading: true, error: null });
    try {
      if (this.state.pending) {
        const pending = this.state.pending;
        try { const run = await this.client.receipt(pending.request_id); if (alive()) this.confirm(run, pending); }
        catch (error) { if (status(error) !== 404) throw error; }
      }
      const sessions = this.options.sessions?.() ?? await this.client.sessions(signal);
      if (!alive()) return;
      this.patch({ sessions });
      if (!this.state.sessionId && !this.state.newChat && !this.state.pending && sessions[0]) {
        this.patch({ sessionId: sessions[0].id }); this.save();
      }
      const sessionId = this.state.sessionId;
      if (sessionId) {
        const runs = await this.client.runs(sessionId, signal);
        if (!alive()) return;
        this.patch({ hasEarlier: runs.length === 100 });
        for (const run of runs) this.accept(run);
        const last = this.state.runs.at(-1);
        if (last) { const next = await this.client.snapshot(last.id, signal); if (alive()) this.accept(next); }
      }
    } catch (cause) { if (alive()) this.patch({ error: asError(cause) }); }
    finally { if (alive()) this.patch({ loading: false }); }
  }
  private confirm(run: AgentRun, pending: Submission) {
    this.patch({ sessionId: run.session_id, pending: null, newChat: false,
      draft: this.state.draft === pending.prompt ? '' : this.state.draft, error: null,
      sessions: this.state.sessions.some(session => session.id === run.session_id) ? this.state.sessions : [{
        id: run.session_id, agent_id: run.agent_id, title: pending.prompt.slice(0, 80), mode: 'cloud_pi',
        created_at: run.created_at, updated_at: run.updated_at,
      }, ...this.state.sessions] });
    this.accept(run); this.save();
  }
  submit = async (prompt: string) => {
    if (this.state.loading || this.state.submitting || runActive(this.state.runs.at(-1)) || !prompt.trim()) return false;
    const pending = this.state.pending;
    if (pending && pending.prompt !== prompt) {
      this.patch({ error: new Error('Confirm the previous message before sending another.') }); return false;
    }
    const revision = this.revision;
    const input = pending ?? { request_id: crypto.randomUUID(), prompt, agent_id: this.client.agentId,
      ...(this.state.sessionId ? { session_id: this.state.sessionId } : {}) };
    this.patch({ pending: input, submitting: true, error: null }); this.save();
    try {
      let run: AgentRun;
      if (pending) {
        try { run = await this.client.receipt(input.request_id); }
        catch (error) { if (status(error) !== 404) throw error; run = await this.client.submit(input); }
      } else run = await this.client.submit(input);
      if (revision !== this.revision) return false;
      this.confirm(run, input); return true;
    } catch (cause) {
      if (revision === this.revision) {
        const rejected = status(cause);
        this.patch({ error: asError(cause), ...(rejected && rejected >= 400 && rejected < 500 ? { pending: null } : {}) });
        this.save();
      }
      return false;
    } finally { if (revision === this.revision) this.patch({ submitting: false }); }
  };
  newChat = () => {
    if (this.busy()) return;
    this.revision++;
    this.patch({ sessionId: null, runs: [], draft: '', error: null, loading: false, newChat: true, hasEarlier: false }); this.save();
  };
  private busy() { return this.state.submitting || Boolean(this.state.pending) || runActive(this.state.runs.at(-1)); }
  selectSession = async (id: string) => {
    if (this.busy() || !this.state.sessions.some(session => session.id === id)) return;
    this.revision++;
    this.patch({ sessionId: id, runs: [], draft: '', error: null, newChat: false }); this.save(); await this.load();
  };
  loadEarlier = async () => {
    if (!this.state.sessionId || this.state.loading || !this.state.hasEarlier) return;
    const revision = this.revision;
    this.patch({ loading: true });
    try {
      const rows = await this.client.runs(this.state.sessionId, undefined, this.state.runs[0]?.created_at);
      if (revision !== this.revision) return;
      for (const row of rows) this.accept(row);
      this.patch({ hasEarlier: rows.length === 100 });
    } catch (cause) { if (revision === this.revision) this.patch({ error: asError(cause) }); }
    finally { if (revision === this.revision) this.patch({ loading: false }); }
  };
  stop = async () => {
    const run = this.state.runs.at(-1);
    if (!run || !runActive(run) || run.stop_requested || this.state.resolving) return;
    await this.command('stop', () => this.client.stop(run.id));
  };
  approve = async (call: string, allow: boolean) => {
    const run = this.state.runs.at(-1);
    if (!run || !runActive(run) || this.state.resolving || !run.tools?.some(tool => tool.call_id === call && tool.state === 'waiting')) return;
    const key = `${run.id}:${call}`;
    const previous = this.decisions.get(key);
    if (previous && previous.allow !== allow) return;
    const decision = previous ?? { id: crypto.randomUUID(), allow };
    this.decisions.set(key, decision);
    await this.command(call, () => this.client.approve(run.id, call, decision.id, allow));
  };
  private async command(id: string, action: () => Promise<AgentRun>) {
    const revision = this.revision;
    this.patch({ resolving: id, error: null });
    try { const run = await action(); const next = await this.client.snapshot(run.id); if (revision === this.revision) this.accept(next); }
    catch (cause) { if (revision === this.revision) this.patch({ error: asError(cause) }); }
    finally { if (revision === this.revision) this.patch({ resolving: null }); }
  }
  private async applyBatch(runId: string, events: AgentEvent[], signal: AbortSignal) {
    let run = this.state.runs.find(row => row.id === runId);
    if (!run) return;
    for (const event of events) {
      if (event.event === 'reset') {
        const next = this.client.owned(event.data as AgentRun);
        if (next.id !== runId || next.sequence !== event.id) throw new AgentProtocolError('Cloud Agent reset identity changed.');
        run = next; continue;
      }
      if (event.id <= run.sequence) continue;
      if (event.id !== run.sequence + 1 || !['text', 'text_reset'].includes(event.event)) {
        run = await this.client.snapshot(runId, signal); continue;
      }
      const data = event.data as { delta?: string; text?: string };
      if (!data || typeof data[event.event === 'text' ? 'delta' : 'text'] !== 'string') {
        run = await this.client.snapshot(runId, signal); continue;
      }
      run = { ...run, sequence: event.id, snapshot: { ...run.snapshot,
        text: event.event === 'text_reset' ? data.text ?? '' : ((run.snapshot.text ?? '') + (data.delta ?? '')).slice(-200000) } };
    }
    if (!signal.aborted) this.accept(run);
  }
  observe(onPublication: () => void) {
    this.observer?.abort();
    const observer = new AbortController(); this.observer = observer;
    const signal = observer.signal;
    const publication = () => {
      const run = this.state.runs.at(-1);
      if (run?.publication?.status === 'committed' && !this.published.has(run.id)) {
        this.published.add(run.id); onPublication();
      }
    };
    void (async () => {
      await this.load(signal);
      let failures = 0;
      while (!signal.aborted) {
        publication();
        const run = this.state.runs.at(-1);
        if (this.state.loading || blocked(this.state.error) || !run || !runActive(run)) { await delay(200, signal); continue; }
        const revision = this.revision;
        try {
          for await (const batch of this.client.events(run.id, run.sequence, signal)) {
            if (signal.aborted || revision !== this.revision) break;
            await this.applyBatch(run.id, batch, signal);
            publication(); this.patch({ reconnecting: false }); failures = 0;
          }
          if (signal.aborted || revision !== this.revision) continue;
          // EOF is transport state, never a run terminal fact.
          this.accept(await this.client.snapshot(run.id, signal)); publication();
          this.patch({ reconnecting: false }); failures = 0;
          await delay(250, signal);
        } catch (cause) {
          if (signal.aborted) break;
          if (blocked(cause)) { this.patch({ error: asError(cause), reconnecting: false }); continue; }
          this.patch({ reconnecting: true });
          await delay(Math.min(30_000, 1000 * 2 ** Math.min(failures++, 5)), signal);
        }
      }
    })();
    return () => observer.abort(); // no implicit server Stop
  }
}
