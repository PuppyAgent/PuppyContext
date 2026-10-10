import { beforeEach, describe, expect, it, vi } from 'vitest';
import { WebAgentClient } from '@/features/agent/runtime/client';
import { makeRun, session, eventResponse } from './fixtures';
const api = vi.hoisted(() => ({ request: vi.fn(), stream: vi.fn() }));
vi.mock('@/lib/apiClient', () => ({ apiRequest: api.request, apiStreamRequest: api.stream }));
let client: WebAgentClient;
beforeEach(() => { client = new WebAgentClient('project-1', 'agent-1'); });
describe('Web Agent public protocol', () => {
  it('submits once to the new endpoint without separately creating a chat session', async () => {
    api.request.mockResolvedValue(makeRun());
    await client.submit({ request_id: 'request-1', prompt: 'Read the project', agent_id: 'agent-1' });
    expect(api.request).toHaveBeenCalledExactlyOnceWith('/api/v1/agents/runs', {
      method: 'POST', body: JSON.stringify({ request_id: 'request-1', prompt: 'Read the project', agent_id: 'agent-1', project_id: 'project-1' }),
    });
  });
  it.each([{ project_id: 'other' }, { agent_id: 'other' }, { request_id: 'other' }, { session_id: 'other' }])('rejects a foreign submission receipt %j', async patch => {
    api.request.mockResolvedValue(makeRun(patch));
    await expect(client.submit({ request_id: 'request-1', prompt: 'Read', agent_id: 'agent-1', session_id: 'session-1' })).rejects.toThrow('different');
  });
  it('filters old sessions and rejects foreign runs', async () => {
    api.request.mockResolvedValue([session, { ...session, id: 'old', mode: 'legacy' }, { ...session, id: 'other', agent_id: 'other' }]);
    expect(await client.sessions()).toEqual([session]);
    api.request.mockResolvedValue([makeRun({ session_id: 'foreign' })]);
    await expect(client.runs('session-1')).rejects.toThrow('different session');
  });
  it('uses a cursor and caller cancellation for the authenticated event stream', async () => {
    api.stream.mockResolvedValue(eventResponse(['id: 9\nevent: text\ndata: {"delta":"ok"}\n\n']));
    const signal = new AbortController().signal;
    const batches = [];
    for await (const batch of client.events('run-1', 8, signal)) batches.push(batch);
    expect(batches[0][0].id).toBe(9);
    expect(api.stream).toHaveBeenCalledWith('/api/v1/agents/runs/run-1/events?after=8', { signal, headers: { Accept: 'text/event-stream' }, cache: 'no-store' });
  });
  it('binds stop, approval and snapshot responses to the requested run', async () => {
    api.request.mockResolvedValue(makeRun({ id: 'foreign' }));
    await expect(client.stop('run-1')).rejects.toThrow('different run');
    await expect(client.approve('run-1', 'tool/1', 'decision', true)).rejects.toThrow('different run');
    await expect(client.snapshot('run-1')).rejects.toThrow('different run');
    expect(api.request).toHaveBeenCalledWith('/api/v1/agents/runs/run-1/approvals/tool%2F1', {
      method: 'POST', body: JSON.stringify({ decision_id: 'decision', allow: true }),
    });
  });
});
