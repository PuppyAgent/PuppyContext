import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { useAgentWorkspace } from '@/features/agent/runtime/useAgentWorkspace';
import { WebAgentClient } from '@/features/agent/runtime/client';
import { makeRun } from './fixtures';
const identity = vi.hoisted(() => ({ userId: 'actor-1', isAuthReady: true }));
vi.mock('@/contexts/SupabaseAuthProvider', () => ({ useAuth: () => identity }));
beforeEach(() => {
  sessionStorage.clear(); identity.userId = 'actor-1'; identity.isAuthReady = true;
  vi.spyOn(WebAgentClient.prototype, 'sessions').mockResolvedValue([]);
  vi.spyOn(WebAgentClient.prototype, 'submit').mockImplementation(async input => makeRun({ request_id: input.request_id }));
});
it('isolates project, agent and actor drafts, aborting old subscriptions on navigation', async () => {
  const { result, rerender } = renderHook(({ project, agent }) => useAgentWorkspace(project, agent, vi.fn()),
    { initialProps: { project: 'project-1', agent: 'agent-1' } });
  await waitFor(() => expect(result.current.state.loading).toBe(false));
  act(() => result.current.controller!.setDraft('Private draft'));
  const original = result.current.controller;
  rerender({ project: 'project-2', agent: 'agent-1' });
  await waitFor(() => expect(result.current.state.loading).toBe(false));
  expect(result.current.state.draft).toBe(''); expect(result.current.controller).not.toBe(original);
  rerender({ project: 'project-1', agent: 'agent-2' }); expect(result.current.state.draft).toBe('');
  identity.userId = 'actor-2'; rerender({ project: 'project-1', agent: 'agent-1' }); expect(result.current.state.draft).toBe('');
  identity.userId = 'actor-1'; rerender({ project: 'project-1', agent: 'agent-1' }); expect(result.current.state.draft).toBe('Private draft');
  expect(WebAgentClient.prototype.submit).not.toHaveBeenCalled();
});
it('does not enable a client before authentication or project identity is available', async () => {
  identity.isAuthReady = false;
  const { result, rerender } = renderHook(({ project }) => useAgentWorkspace(project, 'agent-1', vi.fn()),
    { initialProps: { project: undefined as string | undefined } });
  expect(result.current.controller).toBeNull(); identity.isAuthReady = true; rerender({ project: undefined });
  expect(result.current.controller).toBeNull(); expect(WebAgentClient.prototype.sessions).not.toHaveBeenCalled();
});
it('suspends observation when a retained sidebar is hidden, preserving its draft', async () => {
  const { result, rerender } = renderHook(({ active }) => useAgentWorkspace('project-1', 'agent-1', vi.fn(), active),
    { initialProps: { active: true } });
  await waitFor(() => expect(result.current.state.loading).toBe(false));
  act(() => result.current.controller!.setDraft('Keep my draft'));
  const signal = vi.mocked(WebAgentClient.prototype.sessions).mock.calls[0][0]!;
  rerender({ active: false }); expect(signal.aborted).toBe(true); expect(result.current.state.draft).toBe('Keep my draft');
  expect(WebAgentClient.prototype.sessions).toHaveBeenCalledOnce();
  rerender({ active: true }); await waitFor(() => expect(WebAgentClient.prototype.sessions).toHaveBeenCalledTimes(2));
  expect(result.current.state.draft).toBe('Keep my draft'); expect(WebAgentClient.prototype.submit).not.toHaveBeenCalled();
});
