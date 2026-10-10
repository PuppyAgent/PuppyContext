import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { WebAgentClient } from '@/features/agent/runtime/client';
import { WebAgentController } from '@/features/agent/runtime/controller';
import type { AgentEvent } from '@/features/agent/runtime/types';
import { makeRun, session } from './fixtures';

let client: WebAgentClient;
let controller: WebAgentController;
const disposers: Array<() => void> = [];
const missing = Object.assign(new Error('not found'), { status: 404 });
beforeEach(() => {
  sessionStorage.clear();
  vi.useFakeTimers();
  client = new WebAgentClient('project-1', 'agent-1');
  vi.spyOn(client, 'sessions').mockResolvedValue([]);
  vi.spyOn(client, 'runs').mockResolvedValue([]);
  vi.spyOn(client, 'snapshot').mockResolvedValue(makeRun());
  vi.spyOn(client, 'receipt').mockRejectedValue(missing);
  vi.spyOn(client, 'submit').mockImplementation(async input => makeRun({ request_id: input.request_id, prompt: input.prompt }));
  vi.spyOn(client, 'events').mockImplementation(async function* (_run, _after, signal) {
    await new Promise<void>(resolve => { if (signal.aborted) resolve(); else signal.addEventListener('abort', () => resolve(), { once: true }); });
  });
  controller = new WebAgentController(client, 'test-actor-project-agent');
});
afterEach(() => { for (const dispose of disposers.splice(0)) dispose(); vi.useRealTimers(); });
function observe(callback = vi.fn()) { disposers.push(controller.observe(callback)); return callback; }
async function settled() { await vi.advanceTimersByTimeAsync(300); }

describe('durable Web Agent conversations', () => {
  it('does not create anything when opening the panel', async () => {
    await controller.load(); expect(client.sessions).toHaveBeenCalledOnce(); expect(client.submit).not.toHaveBeenCalled();
  });
  it('recovers a lost submit receipt without creating another run, preserving the original UUID', async () => {
    await controller.load(); controller.setDraft('Write note');
    vi.mocked(client.submit).mockRejectedValueOnce(new Error('connection lost'));
    expect(await controller.submit('Write note')).toBe(false);
    const input = controller.getSnapshot().pending!;
    expect(controller.getSnapshot().draft).toBe('Write note');
    vi.mocked(client.receipt).mockResolvedValue(makeRun({ request_id: input.request_id, prompt: input.prompt }));
    expect(await controller.submit('Write note')).toBe(true);
    expect(client.submit).toHaveBeenCalledOnce(); expect(client.receipt).toHaveBeenCalledWith(input.request_id);
    expect(controller.getSnapshot().sessionId).toBe('session-1');
  });
  it('retries a definitively missing receipt using the same request body', async () => {
    await controller.load(); vi.mocked(client.submit).mockRejectedValueOnce(new Error('lost'));
    await controller.submit('Write'); const input = controller.getSnapshot().pending;
    await controller.submit('Write'); expect(vi.mocked(client.submit).mock.calls[1][0]).toEqual(input);
  });
  it('restores a pending receipt across reload and isolates another actor', async () => {
    await controller.load(); vi.mocked(client.submit).mockRejectedValueOnce(new Error('lost')); await controller.submit('Write');
    const restored = new WebAgentController(client, 'test-actor-project-agent');
    expect(restored.getSnapshot().pending).toEqual(controller.getSnapshot().pending);
    expect(new WebAgentController(client, 'another-actor').getSnapshot().pending).toBeNull();
    vi.mocked(client.receipt).mockResolvedValue(makeRun({ request_id: restored.getSnapshot().pending!.request_id }));
    await restored.load(); expect(restored.getSnapshot().pending).toBeNull(); expect(client.submit).toHaveBeenCalledOnce();
  });
  it('blocks simultaneous submits and does not permit changing a pending prompt', async () => {
    await controller.load(); let reject!: (error: Error) => void;
    vi.mocked(client.submit).mockReturnValueOnce(new Promise((_resolve, r) => { reject = r; }));
    const first = controller.submit('One'); expect(await controller.submit('One')).toBe(false);
    reject(new Error('lost')); await first;
    expect(await controller.submit('Two')).toBe(false); expect(client.submit).toHaveBeenCalledOnce();
  });
  it('clears a definite rejection but keeps the draft', async () => {
    await controller.load(); controller.setDraft('Write');
    vi.mocked(client.submit).mockRejectedValue(Object.assign(new Error('forbidden'), { status: 403 }));
    await controller.submit('Write'); expect(controller.getSnapshot().pending).toBeNull(); expect(controller.getSnapshot().draft).toBe('Write');
  });
  it('keeps the same session for a follow-up and starts a new one only on explicit New chat', async () => {
    vi.mocked(client.sessions).mockResolvedValue([session]);
    vi.mocked(client.runs).mockResolvedValue([makeRun({ state: 'succeeded' })]);
    vi.mocked(client.snapshot).mockResolvedValue(makeRun({ state: 'succeeded' }));
    await controller.load(); await controller.submit('Follow-up');
    expect(vi.mocked(client.submit).mock.calls[0][0].session_id).toBe('session-1');
    // Restore confirmed terminal state before the user requests a new conversation.
    await controller.load(); controller.newChat(); await controller.load(); await controller.submit('New task');
    expect(vi.mocked(client.submit).mock.calls[1][0].session_id).toBeUndefined();
  });
  it('applies ordered text once without one snapshot request per token', async () => {
    observe(); await settled(); await controller.submit('Read');
    const events: AgentEvent[] = Array.from({ length: 60 }, (_, i) => ({ id: i + 1, event: 'text', data: { delta: 'x' } }));
    vi.mocked(client.events).mockImplementationOnce(async function* (_run, _after, signal) {
      yield [...events, events[59]];
      await new Promise<void>(resolve => signal.addEventListener('abort', () => resolve(), { once: true }));
    });
    await settled(); expect(controller.getSnapshot().runs[0].snapshot.text).toBe('x'.repeat(60));
    expect(client.snapshot).not.toHaveBeenCalled();
  });
  it('recovers a cursor gap from one snapshot instead of appending incomplete text', async () => {
    observe(); await settled(); await controller.submit('Read');
    vi.mocked(client.snapshot).mockResolvedValue(makeRun({ sequence: 5, state: 'succeeded', snapshot: { text: 'Authoritative reply' } }));
    vi.mocked(client.events).mockImplementationOnce(async function* () { yield [{ id: 5, event: 'text', data: { delta: 'wrong' } }]; });
    await settled(); expect(controller.getSnapshot().runs[0].snapshot.text).toBe('Authoritative reply');
    expect(controller.getSnapshot().runs[0].state).toBe('succeeded');
  });
  it('reconnects after premature EOF without claiming the run completed', async () => {
    observe(); await settled(); await controller.submit('Read');
    vi.mocked(client.events).mockImplementationOnce(async function* () {});
    vi.mocked(client.snapshot).mockResolvedValue(makeRun({ state: 'running', sequence: 4 }));
    await settled(); expect(controller.getSnapshot().runs[0].state).toBe('running');
    await settled(); expect(client.events).toHaveBeenLastCalledWith('run-1', 4, expect.any(AbortSignal));
  });
  it('accepts reset and text_reset, and invalidates publication only once', async () => {
    const published = observe(); await settled(); await controller.submit('Read');
    const reset = makeRun({ state: 'running', sequence: 20, snapshot: { text: 'old' } });
    vi.mocked(client.events).mockImplementationOnce(async function* () {
      yield [{ id: 20, event: 'reset', data: reset }, { id: 21, event: 'text_reset', data: { text: 'new' } }];
    });
    vi.mocked(client.snapshot).mockResolvedValue(makeRun({ sequence: 21, state: 'succeeded', snapshot: { text: 'new' }, publication: { status: 'committed' } }));
    await settled(); expect(controller.getSnapshot().runs[0].snapshot.text).toBe('new');
    await settled(); expect(published).toHaveBeenCalledOnce();
  });
  it('unmount cancels observation without stopping the sandbox run', async () => {
    const stop = vi.spyOn(client, 'stop'); const dispose = controller.observe(vi.fn()); disposers.push(dispose);
    await settled(); await controller.submit('Read'); await settled();
    const signal = vi.mocked(client.events).mock.calls[0][2]; dispose();
    expect(signal.aborted).toBe(true); expect(stop).not.toHaveBeenCalled();
  });
  it('holds stopped/publishing/conflict facts until the server confirms them', async () => {
    vi.mocked(client.sessions).mockResolvedValue([session]);
    vi.mocked(client.runs).mockResolvedValue([makeRun({ state: 'publishing' })]);
    vi.mocked(client.snapshot).mockResolvedValue(makeRun({ state: 'publishing', stop_requested: true }));
    vi.spyOn(client, 'stop').mockResolvedValue(makeRun({ state: 'publishing', stop_requested: true }));
    await controller.load(); await controller.stop(); expect(controller.getSnapshot().runs[0].state).toBe('publishing');
    expect(await controller.submit('Next')).toBe(false); controller.newChat(); expect(controller.getSnapshot().sessionId).toBe('session-1');
  });
  it('reuses an approval decision UUID after uncertain transport and rejects opposite retries', async () => {
    const waiting = makeRun({ state: 'waiting_approval', tools: [{ call_id: 'call-1', name: 'bash', input: { command: 'ls' }, state: 'waiting' }] });
    vi.mocked(client.sessions).mockResolvedValue([session]); vi.mocked(client.runs).mockResolvedValue([waiting]); vi.mocked(client.snapshot).mockResolvedValue(waiting);
    const approve = vi.spyOn(client, 'approve').mockRejectedValue(new Error('lost'));
    await controller.load(); await controller.approve('call-1', true); await controller.approve('call-1', true); await controller.approve('call-1', false);
    expect(approve).toHaveBeenCalledTimes(2); expect(approve.mock.calls[0]).toEqual(approve.mock.calls[1]);
  });
  it('stops subscriptions after access revocation instead of retrying forever', async () => {
    observe(); await settled(); await controller.submit('Read');
    vi.mocked(client.events).mockImplementationOnce(async function* () { throw Object.assign(new Error('revoked'), { status: 403 }); });
    await settled(); await vi.advanceTimersByTimeAsync(30_000);
    expect(controller.getSnapshot().error?.message).toBe('revoked'); expect(client.events).toHaveBeenCalledOnce();
  });
});

it('restores a draft after closing and reopening the pane', async () => {
  await controller.load(); controller.setDraft('Unsent draft');
  const next = new WebAgentController(client, 'test-actor-project-agent');
  expect(next.getSnapshot().draft).toBe('Unsent draft'); expect(client.submit).not.toHaveBeenCalled();
});
it('loads a full page of history with one latest snapshot, not one query per historical run', async () => {
  vi.mocked(client.sessions).mockResolvedValue([session]);
  vi.mocked(client.runs).mockResolvedValue(Array.from({ length: 100 }, (_, i) => makeRun({ id: `run-${i}`, state: 'succeeded', created_at: new Date(i * 1000).toISOString() })));
  vi.mocked(client.snapshot).mockResolvedValue(makeRun({ id: 'run-99', state: 'succeeded', created_at: new Date(99_000).toISOString() }));
  await controller.load(); expect(controller.getSnapshot().runs).toHaveLength(100);
  expect(client.sessions).toHaveBeenCalledOnce(); expect(client.runs).toHaveBeenCalledOnce(); expect(client.snapshot).toHaveBeenCalledOnce();
  expect(controller.getSnapshot().hasEarlier).toBe(true);
});
it('can resume observation after a user retries following permission restoration', async () => {
  observe(); await settled(); await controller.submit('Read');
  vi.mocked(client.events).mockImplementationOnce(async function* () { throw Object.assign(new Error('revoked'), { status: 403 }); });
  await settled(); expect(controller.getSnapshot().error?.message).toBe('revoked');
  vi.mocked(client.sessions).mockResolvedValue([session]); vi.mocked(client.runs).mockResolvedValue([makeRun({ state: 'running' })]);
  await controller.load(); await settled(); expect(controller.getSnapshot().error).toBeNull(); expect(client.events).toHaveBeenCalledTimes(2);
});
it('fails closed on a foreign reset instead of displaying data or retrying forever', async () => {
  observe(); await settled(); await controller.submit('Read');
  vi.mocked(client.events).mockImplementationOnce(async function* () { yield [{ id: 2, event: 'reset', data: makeRun({ project_id: 'foreign', sequence: 2, snapshot: { text: 'private' } }) }]; });
  await settled(); await vi.advanceTimersByTimeAsync(30_000);
  expect(controller.getSnapshot().runs[0].snapshot.text).toBeUndefined(); expect(controller.getSnapshot().error?.message).toContain('different');
  expect(client.events).toHaveBeenCalledOnce();
});
it('cancels old loads during Strict Mode replay without clearing the new observer state', async () => {
  let resolve!: (value: typeof session[]) => void;
  vi.mocked(client.sessions).mockReturnValueOnce(new Promise(r => { resolve = r; }));
  const first = controller.observe(vi.fn()); first(); observe(); await settled();
  resolve([session]); await settled(); expect(controller.getSnapshot().sessions).toEqual([]); expect(controller.getSnapshot().loading).toBe(false);
});
