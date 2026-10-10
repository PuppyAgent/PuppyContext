import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Workbench } from '@/features/agent/workbench/AgentWorkbench';
import { AgentWorkbenchController } from '@/features/agent/workbench/controller';
import { WebAgentClient } from '@/features/agent/runtime/client';
import type { AgentEvent, AgentRun, AgentSession, Submission } from '@/features/agent/runtime/types';
import '@/app/globals.css';

const sessions: AgentSession[] = ['Research notes', 'Organize project files', 'Review the writing plan'].map((title, index) => ({
  id: `fixture-session-${index}`, title, agent_id: 'agent-1', mode: 'cloud_pi',
  created_at: '2026-10-01T01:00:00Z', updated_at: `2026-10-${String(10 - index).padStart(2, '0')}T04:30:00Z`,
}));
const runs = new Map<string, AgentRun>();
for (const session of sessions) {
  runs.set(session.id, { id: session.id, project_id: 'fixture-project', agent_id: 'agent-1',
    session_id: session.id, request_id: session.id, prompt: session.title ?? '', state: 'succeeded', sequence: 1,
    stop_requested: false, snapshot: { text: 'I reviewed the project files.\n\n- **Herb.md** — research notes\n- **Archive/** — previous drafts\n\nWhat would you like to work on next?' },
    publication: null, tools: [], created_at: session.created_at, updated_at: session.updated_at,
  });
}
const metrics = { historyRequests: 0, runRequests: 0, submits: 0, stops: 0 };
class FixtureClient extends WebAgentClient {
  async sessions() { metrics.historyRequests++; return [...sessions]; }
  async runs(session: string) { metrics.runRequests++; return [...runs.values()].filter(run => run.session_id === session); }
  async snapshot(id: string) { return runs.get(id)!; }
  async submit(input: Submission) {
    metrics.submits++;
    const run: AgentRun = { ...runs.values().next().value!, id: crypto.randomUUID(), request_id: input.request_id,
      session_id: input.session_id ?? crypto.randomUUID(), prompt: input.prompt, state: 'running', sequence: 0,
      snapshot: { text: '' }, created_at: new Date().toISOString(), updated_at: new Date().toISOString() };
    runs.set(run.id, run); return run;
  }
  async *events(id: string, _after: number, signal: AbortSignal): AsyncGenerator<AgentEvent[]> {
    await new Promise(resolve => setTimeout(resolve, 1500));
    if (signal.aborted) return;
    const run: AgentRun = { ...runs.get(id)!, sequence: 1, state: 'succeeded', snapshot: { text: 'Your draft is preserved in this conversation.\n\nYou can switch tabs while another Agent is working.' } };
    runs.set(id, run); yield [{ id: 1, event: 'reset', data: run }];
  }
  async stop(id: string) { metrics.stops++; const run = { ...runs.get(id)!, state: 'stopped' as const }; runs.set(id, run); return run; }
}
const store = new AgentWorkbenchController('browser-fixture', 'fixture-project', () => new FixtureClient('fixture-project', 'agent-1'));
store.setAgents([{ id: 'agent-1', name: 'Built-in Agent' }]);
Object.assign(window, { agentWorkbenchFixture: { store, metrics } });

function Fixture() {
  const [width, setWidth] = useState(460);
  const [active, setActive] = useState(true);
  return <main style={{ display: 'grid', minHeight: '100dvh', placeItems: 'center', background: 'var(--po-canvas)', padding: 24 }}>
    <div style={{ display: 'flex', gap: 12, alignItems: 'center', color: 'var(--po-text)', fontSize: 13 }}>
      <label>Panel width <select aria-label='Panel width' value={width} onChange={event => setWidth(Number(event.target.value))}>{[280, 320, 380, 460, 640].map(size => <option key={size}>{size}</option>)}</select></label>
      <button onClick={() => document.documentElement.classList.toggle('dark')}>Toggle theme</button>
      <button onClick={() => setActive(value => !value)}>Toggle panel</button>
    </div>
    <div id='agent-fixture-panel' style={{ width, maxWidth: 'calc(100vw - 48px)', height: 'min(740px, calc(100dvh - 100px))', border: '1px solid var(--po-divider)', display: active ? 'block' : 'none' }}>
      <Workbench store={store} active={active} />
    </div>
  </main>;
}
createRoot(document.getElementById('root')!).render(<Fixture />);
