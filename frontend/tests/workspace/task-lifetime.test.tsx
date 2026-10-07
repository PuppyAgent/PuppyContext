import React, { StrictMode, type ReactNode } from 'react';
import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { SWRConfig } from 'swr';
import { TaskProvider, useTaskActions } from '@/contexts/TaskProvider';
import { useProjectImportJobs } from '@/lib/hooks/useImportJobs';
import { getProjectImportJobs, type ImportJob, type ImportJobListResponse } from '@/lib/importApi';

const identity = vi.hoisted(() => ({ userId: 'account-a' as string | null }));
vi.mock('@/contexts/SupabaseAuthProvider', () => ({ useAuth: () => ({ userId: identity.userId }) }));
vi.mock('@/lib/importApi', async original => ({ ...await original<object>(), getProjectImportJobs: vi.fn() }));

const empty = { jobs: [], total: 0 };
const job = (id: string, projectId = 'p') => ({ id, project_id: projectId, status: 'running' }) as ImportJob;
function wrapper() {
  const cache = new Map();
  return function Wrapper({ children }: { children: ReactNode }) {
    // Match the application's dedupe window, including in-flight reads on return.
    return <StrictMode><SWRConfig value={{ provider: () => cache, dedupingInterval: 5000, errorRetryCount: 0 }}><TaskProvider>{children}</TaskProvider></SWRConfig></StrictMode>;
  };
}
function deferred() {
  let resolve!: (value: ImportJobListResponse) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<ImportJobListResponse>((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}
function mount() {
  return renderHook(({ project }) => useProjectImportJobs(project, identity.userId, useTaskActions().isActive), {
    initialProps: { project: 'p' }, wrapper: wrapper(),
  });
}
beforeEach(() => {
  identity.userId = 'account-a';
  sessionStorage.clear();
  vi.mocked(getProjectImportJobs).mockReset().mockResolvedValue(empty);
});

describe('Import query identity lifetime', () => {
  it('does not let a late creation callback write to the newly selected project', async () => {
    const hook = mount();
    await waitFor(() => expect(hook.result.current.isLoading).toBe(false));
    const previous = hook.result.current;
    hook.rerender({ project: 'q' });
    await waitFor(() => expect(hook.result.current.isLoading).toBe(false));
    // Leave the next read pending so it cannot hide an incorrect optimistic write.
    vi.mocked(getProjectImportJobs).mockReturnValue(deferred().promise);
    act(() => { void previous.upsertJob(job('old-project')); });
    expect(hook.result.current.jobs).toEqual([]);
    expect(getProjectImportJobs).toHaveBeenCalledTimes(2);
  });

  it('rejects callbacks from a previous account lifetime even after switching back', async () => {
    const hook = mount();
    await waitFor(() => expect(hook.result.current.isLoading).toBe(false));
    const previous = hook.result.current;
    identity.userId = 'account-b';
    hook.rerender({ project: 'p' });
    await waitFor(() => expect(hook.result.current.isLoading).toBe(false));
    identity.userId = 'account-a';
    hook.rerender({ project: 'p' });
    await waitFor(() => expect(hook.result.current.isLoading).toBe(false));
    const reads = vi.mocked(getProjectImportJobs).mock.calls.length;
    vi.mocked(getProjectImportJobs).mockReturnValue(deferred().promise);
    act(() => { void previous.upsertJob(job('old-account')); void previous.refresh(); });
    expect(hook.result.current.jobs).toEqual([]);
    expect(getProjectImportJobs).toHaveBeenCalledTimes(reads);
  });

  it('does not reuse a previous account lifetime\'s in-flight response on return', async () => {
    const old = deferred();
    vi.mocked(getProjectImportJobs).mockReturnValueOnce(old.promise).mockResolvedValue(empty);
    const hook = mount();
    identity.userId = 'account-b';
    hook.rerender({ project: 'p' });
    await waitFor(() => expect(hook.result.current.isLoading).toBe(false));
    identity.userId = 'account-a';
    hook.rerender({ project: 'p' });
    await act(async () => old.resolve({ jobs: [job('old-read')], total: 1 }));
    await waitFor(() => expect(hook.result.current.isLoading).toBe(false));
    expect(hook.result.current.jobs).toEqual([]);
    expect(getProjectImportJobs).toHaveBeenCalledTimes(3);
  });

  it('cannot seed the cache after its query owner unmounts', async () => {
    const Wrapper = wrapper();
    const first = renderHook(() => useProjectImportJobs('p', identity.userId, useTaskActions().isActive), { wrapper: Wrapper });
    await waitFor(() => expect(first.result.current.isLoading).toBe(false));
    const previous = first.result.current;
    first.unmount();
    await act(async () => { await previous.upsertJob(job('unmounted')); });
    const next = renderHook(() => useProjectImportJobs('p', identity.userId, useTaskActions().isActive), { wrapper: Wrapper });
    expect(next.result.current.jobs).toEqual([]);
  });

  it('ignores a late failed read after returning to the original account', async () => {
    const old = deferred();
    vi.mocked(getProjectImportJobs).mockReturnValueOnce(old.promise).mockResolvedValue(empty);
    const hook = mount();
    identity.userId = 'account-b';
    hook.rerender({ project: 'p' });
    await waitFor(() => expect(hook.result.current.isLoading).toBe(false));
    identity.userId = 'account-a';
    hook.rerender({ project: 'p' });
    await waitFor(() => expect(hook.result.current.isLoading).toBe(false));
    await act(async () => old.reject(new Error('previous account request failed')));
    expect(hook.result.current.error).toBeUndefined();
    expect(hook.result.current.jobs).toEqual([]);
  });

  it('still accepts current creation callbacks and surfaces current read failures', async () => {
    const hook = mount();
    await waitFor(() => expect(hook.result.current.isLoading).toBe(false));
    const latest = deferred();
    vi.mocked(getProjectImportJobs).mockReturnValue(latest.promise);
    let creation!: ReturnType<typeof hook.result.current.upsertJob>;
    act(() => { creation = hook.result.current.upsertJob(job('current')); });
    expect(hook.result.current.jobs.map(item => item.id)).toEqual(['current']);
    await act(async () => { latest.reject(new Error('current read failed')); await creation; });
    expect(hook.result.current.error?.message).toBe('current read failed');
    expect(hook.result.current.jobs.map(item => item.id)).toEqual(['current']);
  });

  it('retains a project snapshot when its read fails after navigating away', async () => {
    vi.mocked(getProjectImportJobs).mockResolvedValueOnce({ jobs: [job('retained')], total: 1 });
    const hook = mount();
    await waitFor(() => expect(hook.result.current.jobs).toHaveLength(1));
    const old = deferred();
    vi.mocked(getProjectImportJobs).mockReturnValueOnce(old.promise).mockResolvedValue(empty);
    act(() => { void hook.result.current.refresh(); });
    hook.rerender({ project: 'q' });
    await waitFor(() => expect(hook.result.current.isLoading).toBe(false));
    await act(async () => old.reject(new Error('p refresh failed')));
    vi.mocked(getProjectImportJobs).mockReturnValue(deferred().promise);
    hook.rerender({ project: 'p' });
    expect(hook.result.current.jobs.map(item => item.id)).toEqual(['retained']);
    expect(hook.result.current.error?.message).toBe('p refresh failed');
  });
});
