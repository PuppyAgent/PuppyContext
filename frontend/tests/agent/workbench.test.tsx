import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { AgentWorkbenchController, type ChatTab } from '@/features/agent/workbench/controller';
import { AgentWorkbench, Workbench } from '@/features/agent/workbench/AgentWorkbench';
import { useConversationScroll } from '@/features/agent/components/useConversationScroll';
import { WebAgentClient } from '@/features/agent/runtime/client';
import { resolveWorkbenchSessionHeaderLayout } from '@/features/agent/workbench/tabLayout';
import { makeRun } from './fixtures';

const context = vi.hoisted(() => ({ currentAgentId: 'agent-1', selectAgent: vi.fn(), savedAgents: [
  { id: 'agent-1', name: 'Built-in Agent', type: 'chat', status: 'active' },
  { id: 'agent-2', name: 'Research Agent', type: 'chat', status: 'active' },
] }));
vi.mock('@/contexts/AgentContext', () => ({ useAgent: () => context }));
vi.mock('@/contexts/SupabaseAuthProvider', () => ({ useAuth: () => ({ userId: 'user-1', isAuthReady: true }) }));
vi.mock('@/lib/hooks/useOnboarding', () => ({ useOnboarding: () => ({ completeStep: vi.fn() }) }));
vi.mock('@/lib/hooks/useData', () => ({ refreshAllContentNodes: vi.fn(), refreshProjectHistory: vi.fn() }));

const agent = { id: 'agent-1', name: 'Built-in Agent' };
const history = [{ id: 'session-1', agent_id: agent.id, mode: 'cloud_pi', title: 'Research notes',
  created_at: '2026-10-01T01:00:00Z', updated_at: '2026-10-10T01:00:00Z' }];
function setup(user = 'user-1', project = 'project-1') {
  const client = new WebAgentClient(project, agent.id);
  vi.spyOn(client, 'sessions').mockResolvedValue(history);
  vi.spyOn(client, 'runs').mockResolvedValue([makeRun({ state: 'succeeded', snapshot: { text: 'Saved response' } })]);
  vi.spyOn(client, 'snapshot').mockResolvedValue(makeRun({ state: 'succeeded', snapshot: { text: 'Saved response' } }));
  vi.spyOn(client, 'submit').mockImplementation(async input => makeRun({ ...input, session_id: input.session_id ?? 'new-session', state: 'succeeded' }));
  vi.spyOn(client, 'stop');
  const store = new AgentWorkbenchController(user, project, () => client);
  store.setAgents([agent]);
  return { store, client, tab: store.getSnapshot().tabs[0] as ChatTab };
}
afterEach(() => vi.unstubAllGlobals());
beforeEach(() => {
  sessionStorage.clear();
  Element.prototype.scrollIntoView = vi.fn();
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
});

it('isolates drafts per tab and restores order, active tab, and drafts after reload', async () => {
  const { store, tab } = setup();
  store.actor(tab).setDraft('First draft');
  const id = store.newChat(agent.id)!;
  const second = store.getSnapshot().tabs[1] as ChatTab;
  store.actor(second).setDraft('Second draft'); store.move(id, tab.id);
  store.activate(tab.id);
  const restored = setup().store;
  expect(restored.getSnapshot().tabs.map(row => row.id)).toEqual([id, tab.id]);
  expect(restored.getSnapshot().activeId).toBe(tab.id);
  expect(restored.actor(tab).getSnapshot().draft).toBe('First draft');
  expect(restored.actor(second).getSnapshot().draft).toBe('Second draft');
  expect(setup('other-user').store.actor(setup('other-user').tab).getSnapshot().draft).toBe('');
  expect(setup('user-1', 'other-project').store.getSnapshot().tabs[0].id).not.toBe(tab.id);
});

it('creates empty tabs locally, fetching history once only when explicitly requested', async () => {
  const { store, client } = setup();
  const stop = store.observe(vi.fn());
  for (let n = 0; n < 8; n++) store.newChat(agent.id);
  const launcher = store.newTab();
  expect(client.sessions).not.toHaveBeenCalled(); expect(client.submit).not.toHaveBeenCalled();
  expect(client.runs).not.toHaveBeenCalled();
  store.showHistory(launcher);
  await waitFor(() => expect(store.getSnapshot().historyLoading).toBe(false));
  expect(client.sessions).toHaveBeenCalledOnce(); expect(client.runs).not.toHaveBeenCalled();
  store.openSession(history[0], launcher);
  await waitFor(() => expect(client.runs).toHaveBeenCalledOnce());
  expect(client.sessions).toHaveBeenCalledOnce(); expect(client.submit).not.toHaveBeenCalled();
  const count = store.getSnapshot().tabs.length;
  store.openSession(history[0]);
  expect(store.getSnapshot().tabs).toHaveLength(count);
  stop();
});

it('can switch, create and close tabs while another run is active without stopping it', async () => {
  const { store, tab, client } = setup();
  vi.mocked(client.submit).mockResolvedValue(makeRun({ state: 'running' }));
  await store.actor(tab).load(); await store.actor(tab).submit('Run in background');
  const second = store.newChat(agent.id)!;
  store.activate(tab.id); expect(store.getSnapshot().activeId).toBe(tab.id);
  store.activate(second); expect(store.getSnapshot().activeId).toBe(second);
  expect(store.close(tab.id)).toBe(true);
  expect(client.stop).not.toHaveBeenCalled();
  expect(store.getSnapshot().sessions.some(row => row.id === 'session-1')).toBe(true);
});

it('retains an uncertain submission receipt when closing is requested and on reload', async () => {
  const { store, tab, client } = setup();
  vi.mocked(client.submit).mockRejectedValue(new Error('Network disconnected'));
  await store.actor(tab).load(); await store.actor(tab).submit('Do not duplicate');
  const pending = store.actor(tab).getSnapshot().pending;
  expect(pending).not.toBeNull();
  expect(store.close(tab.id)).toBe(false);
  expect(store.getSnapshot().closeError).toContain('Confirm');
  expect(setup().store.actor(tab).getSnapshot().pending).toEqual(pending);
});

it('restores a session after closing its tab without creating another session', async () => {
  const { store, client, tab } = setup();
  await store.actor(tab).load(); await store.actor(tab).submit('Remember this conversation');
  const session = store.getSnapshot().sessions[0];
  store.close(tab.id); const restoredId = store.openSession(session)!;
  const restored = store.getSnapshot().tabs.find(row => row.id === restoredId) as ChatTab;
  expect(store.actor(restored).getSnapshot().sessionId).toBe('new-session');
  expect(client.submit).toHaveBeenCalledOnce(); expect(client.stop).not.toHaveBeenCalled();
});

it('keeps independent real composer DOM and drafts when tabs change', async () => {
  const { store } = setup();
  render(<Workbench store={store} active />);
  const input = await screen.findByRole('textbox', { name: 'Message Agent' });
  await waitFor(() => expect((input as HTMLTextAreaElement).disabled).toBe(false));
  fireEvent.change(input, { target: { value: 'Keep this draft' } });
  const firstTab = screen.getByRole('tab', { name: 'Built-in Agent' });
  fireEvent.click(screen.getByRole('button', { name: 'New tab' }));
  fireEvent.click(screen.getByRole('button', { name: 'Built-in Agent' }));
  await waitFor(() => expect(screen.getByRole('textbox', { name: 'Message Agent' })).not.toBe(input));
  fireEvent.click(firstTab);
  expect(screen.getByRole('textbox', { name: 'Message Agent' })).toBe(input);
  expect((input as HTMLTextAreaElement).value).toBe('Keep this draft');
  fireEvent.keyDown(firstTab, { key: 'End' });
  expect(screen.getAllByRole('tab').at(-1)?.getAttribute('aria-selected')).toBe('true');
});

it('opens full-pane history, supports search/Escape/back and opens history as a tab', async () => {
  const { store, client } = setup();
  render(<Workbench store={store} active />);
  fireEvent.click(screen.getByRole('button', { name: 'New tab' }));
  fireEvent.click(screen.getByRole('button', { name: 'Chat history' }));
  const historyView = await screen.findByRole('region', { name: 'Chat history' });
  await within(historyView).findByRole('button', { name: 'Open Research notes' });
  expect(client.runs).not.toHaveBeenCalled();
  fireEvent.click(within(historyView).getByRole('button', { name: 'Search chats' }));
  fireEvent.change(within(historyView).getByRole('searchbox'), { target: { value: 'no such title' } });
  expect(within(historyView).getByText('No matching conversations')).toBeTruthy();
  fireEvent.keyDown(within(historyView).getByRole('searchbox'), { key: 'Escape' });
  expect(within(historyView).queryByRole('searchbox')).toBeNull();
  fireEvent.click(within(historyView).getByRole('button', { name: 'Back to new tab' }));
  expect(screen.getByRole('button', { name: 'Built-in Agent' })).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Chat history' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Open Research notes' }));
  await waitFor(() => expect(screen.getByText('Saved response')).toBeTruthy());
  expect(screen.getAllByRole('tab')).toHaveLength(2);
  expect(screen.getByRole('tab', { name: 'Research notes' }).getAttribute('aria-selected')).toBe('true');
  expect(client.submit).not.toHaveBeenCalled();
});

it('preserves opened conversations when history refresh fails', async () => {
  const { store, client } = setup();
  await store.refreshHistory();
  vi.mocked(client.sessions).mockRejectedValue(new Error('offline'));
  await store.refreshHistory();
  expect(store.getSnapshot().sessions).toEqual(history);
  expect(store.getSnapshot().historyError).toBe('offline');
});

it('uses Desktop density thresholds and always retains the active tab in overflow', () => {
  const ids = Array.from({ length: 20 }, (_, n) => `tab-${n}`);
  for (const width of [220, 320, 400, 480, 800]) {
    const layout = resolveWorkbenchSessionHeaderLayout({ sessionIds: ids, activeSessionId: 'tab-19', availableWidth: width });
    expect(layout.visibleSessionIds).toContain('tab-19');
    expect(layout.tabsWidth + 31).toBeLessThanOrEqual(width);
    expect(new Set([...layout.visibleSessionIds, ...layout.hiddenSessionIds]).size).toBe(20);
  }
  const full = resolveWorkbenchSessionHeaderLayout({ sessionIds: ids.slice(0, 2), activeSessionId: ids[0], availableWidth: 400 });
  expect(full.mode).toBe('full'); expect(full.activeTabWidth).toBe(144);
});

it('keeps a live run observer attached across tab switches and tears it down on hide', async () => {
  const { store, tab } = setup();
  const observer = vi.spyOn(store.actor(tab), 'observe');
  const { rerender } = render(<Workbench store={store} active />);
  act(() => { store.newChat(agent.id); store.activate(tab.id); });
  expect(observer).toHaveBeenCalledOnce();
  rerender(<Workbench store={store} active={false} />);
  rerender(<Workbench store={store} active />);
  expect(observer).toHaveBeenCalledTimes(2);
});


it('routes explicit agent entry to an existing tab and opens a different agent locally', () => {
  const { store, tab, client } = setup();
  store.setAgents([agent, { id: 'agent-2', name: 'Research Agent' }]);
  const other = store.openAgent('agent-2');
  expect(store.getSnapshot().activeId).toBe(other);
  expect(store.openAgent(agent.id)).toBe(tab.id);
  expect(store.getSnapshot().tabs).toHaveLength(2);
  expect(client.submit).not.toHaveBeenCalled(); expect(client.sessions).not.toHaveBeenCalled();
});

it('reloads a restored history tab once agents become available', async () => {
  const { store } = setup();
  store.showHistory();
  await waitFor(() => expect(store.getSnapshot().historyLoading).toBe(false));
  const client = new WebAgentClient('project-1', agent.id);
  vi.spyOn(client, 'sessions').mockResolvedValue(history);
  const restored = new AgentWorkbenchController('user-1', 'project-1', () => client);
  const stop = restored.observe(vi.fn());
  expect(client.sessions).not.toHaveBeenCalled();
  restored.setAgents([agent]);
  await waitFor(() => expect(restored.getSnapshot().sessions).toEqual(history));
  expect(client.sessions).toHaveBeenCalledOnce(); stop();
});

it('stops observing an agent when it is removed from the available catalog', () => {
  const { store, tab } = setup();
  const detach = vi.fn();
  vi.spyOn(store.actor(tab), 'observe').mockReturnValue(detach);
  const stop = store.observe(vi.fn());
  store.setAgents([]);
  expect(detach).toHaveBeenCalledOnce();
  stop(); expect(detach).toHaveBeenCalledOnce();
});

it('opens the requested agent once without overriding later manual tab selection', async () => {
  const request = { agentId: 'agent-2' };
  const ui = render(<AgentWorkbench projectId='connected-project' active request={request} />);
  await waitFor(() => expect(screen.getByRole('tab', { name: 'Research Agent' }).getAttribute('aria-selected')).toBe('true'));
  fireEvent.click(screen.getByRole('button', { name: 'New tab' }));
  fireEvent.click(screen.getByRole('button', { name: 'Built-in Agent' }));
  ui.rerender(<AgentWorkbench projectId='connected-project' active request={request} />);
  expect(screen.getByRole('tab', { name: 'Built-in Agent' }).getAttribute('aria-selected')).toBe('true');
  ui.rerender(<AgentWorkbench projectId='connected-project' active request={{ agentId: 'agent-2' }} />);
  expect(screen.getByRole('tab', { name: 'Research Agent' }).getAttribute('aria-selected')).toBe('true');
  expect(screen.getAllByRole('tab')).toHaveLength(2);
});

it('retains a readers scroll position across tab switches and background messages', () => {
  function Reader({ active, revision }: { active: boolean; revision: string }) {
    const scroll = useConversationScroll('session-1', active, revision);
    return <div ref={scroll.viewport} onScroll={scroll.onScroll} data-testid='scroll' />;
  }
  const ui = render(<Reader active revision='1' />);
  const element = screen.getByTestId('scroll');
  Object.defineProperties(element, { scrollHeight: { value: 1200 }, clientHeight: { value: 400 } });
  element.scrollTop = 100; fireEvent.scroll(element);
  ui.rerender(<Reader active={false} revision='1' />);
  ui.rerender(<Reader active={false} revision='2' />);
  element.scrollTop = 0;
  ui.rerender(<Reader active revision='2' />);
  expect(element.scrollTop).toBe(100);
  element.scrollTop = 800; fireEvent.scroll(element);
  ui.rerender(<Reader active revision='3' />);
  expect(element.scrollTop).toBe(1200);
});
