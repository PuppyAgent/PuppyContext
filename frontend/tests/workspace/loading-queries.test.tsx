import type { ReactNode } from 'react';
import { act, renderHook, waitFor } from '@testing-library/react';
import { SWRConfig } from 'swr';
import { beforeEach, expect, it, vi } from 'vitest';
import { useProjects, useTreeDir } from '@/lib/hooks/useData';
import { useFileWorkspaceQueries } from '@/features/files/useFileWorkspaceQueries';
import { readState } from '@/lib/queryState';
import { getProjects, type ProjectInfo } from '@/lib/projectsApi';
import { listDir, type NodeInfo } from '@/lib/contentTreeApi';
import { get } from '@/lib/apiClient';
import { getToolsByProjectId } from '@/lib/mcpApi';
import { getRepoIdentity, listScopes, listAccessSurfaces } from '@/lib/repoApi';
import { listMcpEndpoints } from '@/lib/mcpEndpointsApi';
import { listSandboxEndpoints } from '@/lib/sandboxEndpointsApi';

vi.mock('@/lib/projectsApi', () => ({ getProjects: vi.fn(), getProject: vi.fn() }));
vi.mock('@/lib/contentTreeApi', async original => ({ ...await original<object>(), listDir: vi.fn() }));
vi.mock('@/lib/apiClient', () => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), del: vi.fn() }));
vi.mock('@/lib/mcpApi', () => ({ getToolsByProjectId: vi.fn(), getToolsByPath: vi.fn() }));
vi.mock('@/lib/repoApi', () => ({ getRepoIdentity: vi.fn(), listScopes: vi.fn(), listAccessSurfaces: vi.fn() }));
vi.mock('@/lib/mcpEndpointsApi', () => ({ listMcpEndpoints: vi.fn() }));
vi.mock('@/lib/sandboxEndpointsApi', () => ({ listSandboxEndpoints: vi.fn() }));

function cache() {
  const data = new Map();
  return function Wrapper({ children }: { children: ReactNode }) {
    return <SWRConfig value={{ provider: () => data, shouldRetryOnError: false }}>{children}</SWRConfig>;
  };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}
const listing = (names: string[]) => ({ nodes: names.map(name => ({ id: name, path: name, name: name.split('/').at(-1), type: 'file' } as NodeInfo)), path: '', total: names.length });
const decorations = [get, getToolsByProjectId, getRepoIdentity, listScopes, listAccessSurfaces, listMcpEndpoints, listSandboxEndpoints];
beforeEach(() => {
  vi.mocked(getProjects).mockReset();
  vi.mocked(listDir).mockReset();
  for (const request of decorations) vi.mocked(request).mockReset().mockResolvedValue([] as never);
});

it('distinguishes disabled, pending, successful empty and error snapshots', () => {
  expect(readState(false, undefined, undefined)).toBe('idle');
  expect(readState(true, undefined, undefined)).toBe('loading');
  expect(readState(true, [], undefined)).toBe('ready');
  expect(readState(true, undefined, new Error())).toBe('error');
  expect(readState(true, ['cached'], new Error())).toBe('ready');
});

it('does not report an unrequested org as a loaded empty list', async () => {
  const pending = deferred<ProjectInfo[]>();
  vi.mocked(getProjects).mockReturnValue(pending.promise);
  const hook = renderHook(({ org }) => useProjects(org), { initialProps: { org: null as string | null }, wrapper: cache() });
  expect(hook.result.current.status).toBe('idle');
  expect(hook.result.current.hasLoaded).toBe(false);
  expect(getProjects).not.toHaveBeenCalled();
  hook.rerender({ org: 'a' });
  expect(hook.result.current.status).toBe('loading');
  await act(async () => pending.resolve([]));
  expect(hook.result.current.status).toBe('ready');
  expect(hook.result.current.projects).toEqual([]);
});

it('never shows the previous org under the new identity and ignores a late old response', async () => {
  const second = deferred<ProjectInfo[]>();
  const third = deferred<ProjectInfo[]>();
  vi.mocked(getProjects).mockResolvedValueOnce([{ id: 'a', name: 'A' }]).mockReturnValueOnce(second.promise).mockReturnValueOnce(third.promise);
  const hook = renderHook(({ org }) => useProjects(org), { initialProps: { org: 'a' }, wrapper: cache() });
  await waitFor(() => expect(hook.result.current.projects).toHaveLength(1));
  hook.rerender({ org: 'b' });
  expect(hook.result.current.projects).toEqual([]);
  expect(hook.result.current.isLoading).toBe(true);
  hook.rerender({ org: 'c' });
  await act(async () => second.resolve([{ id: 'b', name: 'B' }]));
  expect(hook.result.current.status).toBe('loading');
  expect(hook.result.current.projects).toEqual([]);
  await act(async () => third.resolve([{ id: 'c', name: 'C' }]));
  expect(hook.result.current.projects[0].id).toBe('c');
});

it('shares the directory request and retains the same snapshot during a refresh', async () => {
  const first = deferred<Awaited<ReturnType<typeof listDir>>>();
  vi.mocked(listDir).mockReturnValueOnce(first.promise);
  const hook = renderHook(() => ({ explorer: useTreeDir('p', ''), pane: useTreeDir('p', '') }), { wrapper: cache() });
  expect(listDir).toHaveBeenCalledTimes(1);
  expect(hook.result.current.explorer.isLoading).toBe(true);
  await act(async () => first.resolve(listing(['a.md'])));
  expect(hook.result.current.pane.nodes[0].name).toBe('a.md');
  const next = deferred<Awaited<ReturnType<typeof listDir>>>();
  vi.mocked(listDir).mockReturnValueOnce(next.promise);
  let refresh!: Promise<unknown>;
  act(() => { refresh = hook.result.current.explorer.refresh(); });
  expect(hook.result.current.explorer.nodes).toHaveLength(1);
  expect(hook.result.current.explorer.isLoading).toBe(false);
  await act(async () => { next.resolve(listing([])); await refresh; });
  expect(hook.result.current.pane.hasLoaded).toBe(true);
  expect(hook.result.current.pane.nodes).toEqual([]);
});

it('shows directory failures as errors and can recover with an explicit retry', async () => {
  vi.mocked(listDir).mockRejectedValueOnce(new Error('offline'));
  const hook = renderHook(() => useTreeDir('p', ''), { wrapper: cache() });
  await waitFor(() => expect(hook.result.current.status).toBe('error'));
  expect(hook.result.current.hasLoaded).toBe(false);
  vi.mocked(listDir).mockResolvedValueOnce(listing([]));
  await act(async () => { await hook.result.current.refresh(); });
  expect(hook.result.current.status).toBe('ready');
  expect(hook.result.current.error).toBeUndefined();
});

it('switching a directory or project hides old nodes until the correct listing arrives', async () => {
  vi.mocked(listDir).mockResolvedValueOnce(listing(['a.md'])).mockImplementation(() => new Promise(() => {}));
  const hook = renderHook(({ project, path }) => useTreeDir(project, path), { initialProps: { project: 'p', path: '' }, wrapper: cache() });
  await waitFor(() => expect(hook.result.current.nodes).toHaveLength(1));
  hook.rerender({ project: 'p', path: 'docs' });
  expect(hook.result.current.isLoading).toBe(true);
  expect(hook.result.current.nodes).toEqual([]);
  hook.rerender({ project: 'q', path: '' });
  expect(hook.result.current.isLoading).toBe(true);
  expect(hook.result.current.nodes).toEqual([]);
});

it('starts seven decoration reads only after the active project root, with no other project scans', async () => {
  const root = deferred<Awaited<ReturnType<typeof listDir>>>();
  vi.mocked(listDir).mockReturnValueOnce(root.promise).mockImplementation(() => new Promise(() => {}));
  const hook = renderHook(({ project }) => useFileWorkspaceQueries(project), { initialProps: { project: 'p' }, wrapper: cache() });
  expect(listDir).toHaveBeenCalledExactlyOnceWith('p', '');
  for (const request of decorations) expect(request).not.toHaveBeenCalled();
  await act(async () => root.resolve(listing([])));
  await waitFor(() => { for (const request of decorations) expect(request).toHaveBeenCalledTimes(1); });
  expect(get).toHaveBeenCalledExactlyOnceWith('/api/v1/synchronize/status?project_id=p');
  hook.rerender({ project: 'q' });
  expect(hook.result.current.root.hasLoaded).toBe(false);
  for (const request of decorations) expect(request).toHaveBeenCalledTimes(1);
  expect(listDir).toHaveBeenLastCalledWith('q', '');
});

it('does not start decorations when the root failed to load', async () => {
  vi.mocked(listDir).mockRejectedValueOnce(new Error('no access'));
  const hook = renderHook(() => useFileWorkspaceQueries('p'), { wrapper: cache() });
  await waitFor(() => expect(hook.result.current.root.status).toBe('error'));
  for (const request of decorations) expect(request).not.toHaveBeenCalled();
});
