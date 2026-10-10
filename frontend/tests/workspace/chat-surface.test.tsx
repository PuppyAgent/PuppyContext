import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { AgentChatHeader } from '@/components/chat/AgentChatChrome';
import ChatInputArea from '@/components/chat/ChatInputArea';
import { ChatRuntimeView } from '@/features/agent/components/ChatRuntimeView';
import { makeRun, eventResponse } from '../agent/fixtures';
import type { AgentRun } from '@/features/agent/runtime/types';

const chat = vi.hoisted(() => ({
  sessions: [] as { id: string; title: string; agent_id: string; mode: null; created_at: string; updated_at: string }[],
  request: vi.fn(), stream: vi.fn(),
  run: null as AgentRun | null, count: 0,
  updateAgentInfo: vi.fn(), setDraftResources: vi.fn(),
  capabilities: new Set<string>(),
}));
vi.mock('@/contexts/AgentContext', () => ({ useAgent: () => ({
  currentAgentId: 'agent-1', savedAgents: [{ id: 'agent-1', name: 'Project Agent', type: 'chat', resources: [] }],
  selectedCapabilities: chat.capabilities, draftResources: [], updateAgentInfo: chat.updateAgentInfo, setDraftResources: chat.setDraftResources,
}) }));
vi.mock('@/lib/hooks/useOnboarding', () => ({ useOnboarding: () => ({ completeStep: vi.fn() }) }));
vi.mock('@/contexts/SupabaseAuthProvider', () => ({ useAuth: () => ({ userId: 'user-1', isAuthReady: true }) }));
vi.mock('@/lib/apiClient', () => ({ apiRequest: chat.request, apiStreamRequest: chat.stream }));
vi.mock('@/lib/hooks/useData', () => ({ refreshAllContentNodes: vi.fn(), refreshProjectHistory: vi.fn() }));

beforeEach(() => {
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
  Element.prototype.scrollIntoView = vi.fn();
  chat.sessions = [];
  chat.capabilities.clear();
  sessionStorage.clear(); chat.run = null; chat.count = 0;
  chat.request.mockImplementation(async (path: string, options?: RequestInit) => {
    if (path.includes('/agents/sessions?')) return [];
    if (path === '/api/v1/agents/runs' && options?.method === 'POST') {
      const input = JSON.parse(options.body as string);
      chat.count++;
      chat.run = makeRun({ ...input, id: `run-${chat.count}`, session_id: input.session_id ?? `session-${chat.count}` });
      return chat.run;
    }
    if (path.includes('/agents/runs/')) return chat.run;
    if (path.includes('/runs?')) return chat.run ? [chat.run] : [];
    throw new Error(`Unexpected API ${path}`);
  });
  chat.stream.mockImplementation(async () => {
    chat.run = { ...chat.run!, sequence: 1, state: 'succeeded', snapshot: { text: 'Ready to help.' } };
    return eventResponse([`id: 1\nevent: reset\ndata: ${JSON.stringify(chat.run)}\n\n`]);
  });
});

describe('Agent chat chrome', () => {
  it('restores a conversation from history and dismisses the popover', () => {
    const select = vi.fn();
    render(<AgentChatHeader title='New chat' onSelectSession={select} sessions={[{
      id: 'session-1', title: 'Review the project', agent_id: 'agent-1', mode: null,
      created_at: '2026-09-19T00:00:00Z', updated_at: '2026-09-19T00:00:00Z',
    }]} />);
    fireEvent.click(screen.getByRole('button', { name: 'Chat history' }));
    fireEvent.click(screen.getByRole('button', { name: /Review the project/ }));
    expect(select).toHaveBeenCalledWith('session-1');
    expect(screen.queryByText('Review the project')).toBeNull();
  });

  it('closes history with Escape and returns keyboard focus', () => {
    render(<AgentChatHeader title='New chat' onSelectSession={vi.fn()} />);
    const trigger = screen.getByRole('button', { name: 'Chat history' });
    fireEvent.click(trigger);
    expect(screen.getByText('No chat history yet')).toBeTruthy();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByText('No chat history yet')).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it('keeps the rename form open and reports a failed save', async () => {
    const rename = vi.fn().mockRejectedValue(new Error('offline'));
    render(<AgentChatHeader title='Agent' agentName='Agent' onRename={rename} />);
    fireEvent.click(screen.getByRole('button', { name: 'Chat actions' }));
    fireEvent.click(screen.getByRole('button', { name: 'Rename agent' }));
    fireEvent.change(screen.getByRole('textbox', { name: 'Agent name' }), { target: { value: ' Research ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save agent name' }));
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('Could not rename'));
    expect(rename).toHaveBeenCalledWith('Research');
    expect(screen.getByRole('textbox', { name: 'Agent name' })).toBeTruthy();
  });

  it('prevents switching conversations during a response while allowing the panel to close', () => {
    const close = vi.fn();
    const newChat = vi.fn();
    render(<AgentChatHeader title='Agent' busy onNewChat={newChat} onSelectSession={vi.fn()} onClose={close} />);
    expect((screen.getByRole('button', { name: 'New chat' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: 'Chat history' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: 'Close chat panel' }));
    expect(close).toHaveBeenCalledOnce();
    expect(newChat).not.toHaveBeenCalled();
  });
});

describe('Agent composer', () => {
  const props = { inputValue: 'Draft', onInputChange: vi.fn(), onKeyDown: vi.fn(), onSend: vi.fn(),
    isLoading: false, showMentionMenu: false, filteredMentionOptions: [], mentionIndex: 0,
    onMentionSelect: vi.fn(), onMentionIndexChange: vi.fn() };

  it('does not allow sending from a disabled composer even with a draft', () => {
    render(<ChatInputArea {...props} disabled />);
    fireEvent.click(screen.getByRole('button', { name: 'Send message' }));
    expect(props.onSend).not.toHaveBeenCalled();
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).disabled).toBe(true);
  });

  it('preserves path selection through the mention menu', () => {
    render(<ChatInputArea {...props} showMentionMenu filteredMentionOptions={['project.name', 'project.files']} />);
    fireEvent.click(screen.getByRole('option', { name: '@project.files' }));
    expect(props.onMentionSelect).toHaveBeenCalledWith('project.files');
  });

  it('submits through durable runs, renders the reply, and reuses the session for follow-ups', async () => {
    render(<ChatRuntimeView availableTools={[]} projectId='project-1' />);
    const input = screen.getByRole('textbox', { name: 'Message Agent' });
    await waitFor(() => expect((input as HTMLTextAreaElement).disabled).toBe(false));
    fireEvent.change(input, { target: { value: 'Summarize this project' } });
    fireEvent.keyDown(input, { key: 'Enter', isComposing: true });
    fireEvent.keyDown(input, { key: 'Enter', shiftKey: true });
    expect(chat.count).toBe(0);
    fireEvent.keyDown(input, { key: 'Enter' });
    await waitFor(() => expect(screen.getByText('Ready to help.')).toBeTruthy());
    const post = chat.request.mock.calls.find(([path, init]) => path === '/api/v1/agents/runs' && init?.method === 'POST');
    expect(JSON.parse(post![1].body)).toMatchObject({ project_id: 'project-1', agent_id: 'agent-1', prompt: 'Summarize this project' });
    expect(JSON.parse(post![1].body).session_id).toBeUndefined();
    expect((input as HTMLTextAreaElement).value).toBe('');
    fireEvent.change(input, { target: { value: 'Continue' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send message' }));
    await waitFor(() => expect(chat.count).toBe(2));
    const posts = chat.request.mock.calls.filter(([path, init]) => path === '/api/v1/agents/runs' && init?.method === 'POST');
    expect(JSON.parse(posts[1][1].body).session_id).toBe('session-1');
    expect(chat.request.mock.calls.some(([path]) => path.includes('/chat/'))).toBe(false);
  });

  it('retains the existing Agent settings surface while separating it from execution state', async () => {
    render(<ChatRuntimeView availableTools={[]} projectId='project-1' />);
    fireEvent.click(screen.getByRole('button', { name: 'Chat actions' }));
    fireEvent.click(screen.getByRole('button', { name: 'Agent settings' }));
    expect(screen.getByText("Agent's bash access")).toBeTruthy();
    expect(screen.getByDisplayValue('Project Agent')).toBeTruthy();
    expect(screen.getByText('Drag items into this')).toBeTruthy();
    expect(chat.setDraftResources).toHaveBeenCalledWith([]);
    expect(chat.count).toBe(0);
  });

  it('provides an explicit stop action while leaving the input blocked until server confirmation', () => {
    const stop = vi.fn();
    render(<ChatInputArea {...props} inputValue='' isLoading onStop={stop} />);
    fireEvent.click(screen.getByRole('button', { name: 'Stop Agent' }));
    expect(stop).toHaveBeenCalledOnce();
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).disabled).toBe(true);
  });
});
