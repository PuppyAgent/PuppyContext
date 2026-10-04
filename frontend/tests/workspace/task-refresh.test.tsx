import React, { StrictMode, type ReactNode } from 'react';
import { act, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { SWRConfig, useSWRConfig } from 'swr';
import { BackgroundTaskNotifier } from '@/components/BackgroundTaskNotifier';
import { TaskStatusWidget } from '@/components/TaskStatusWidget';
import { ValueRenderer } from '@/components/editors/table/components/ValueRenderer';
import { TaskProvider, usePendingTasks, useTaskActions } from '@/features/tasks/TaskProvider';
import { etlTaskKeys, taskStorageKey } from '@/features/tasks/model';
import { batchGetETLTaskStatus, type ETLTaskStatus } from '@/lib/etlApi';
import { listDir } from '@/lib/contentTreeApi';
import { getProjects, getTable } from '@/lib/projectsApi';
import { useProjects, useTable, useTreeDir } from '@/lib/hooks/useData';
import { getProjectImportJobs, type ImportJob } from '@/lib/importApi';
import { useProjectImportJobs } from '@/lib/hooks/useImportJobs';
import { useFileImport } from '@/features/files/hooks/useFileImport';
import { uploadFiles } from '@/lib/uploadApi';
import { isTaskCompletionKey } from '@/features/tasks/invalidation';

const identity = vi.hoisted(() => ({ userId: 'account-a' as string | null, projectId: 'p' }));
vi.mock('@/contexts/SupabaseAuthProvider', () => ({ useAuth: () => ({ userId: identity.userId, session: identity.userId ? { access_token: `token-${identity.userId}` } : null }) }));
vi.mock('next/navigation', () => ({ useParams: () => ({ projectId: identity.projectId }) }));
vi.mock('@/lib/etlApi', async original => ({ ...await original<object>(), batchGetETLTaskStatus: vi.fn(), cancelETLTask: vi.fn() }));
vi.mock('@/lib/contentTreeApi', async original => ({ ...await original<object>(), listDir: vi.fn() }));
vi.mock('@/lib/uploadApi', () => ({ uploadFiles: vi.fn() }));
vi.mock('@/lib/projectsApi', () => ({ getProjects: vi.fn(), getTable: vi.fn() }));
vi.mock('@/lib/importApi', async original => ({ ...await original<object>(), getProjectImportJobs: vi.fn() }));

const task = { taskId: 'tmp-1', projectId: 'p', orgId: 'org-a', tableId: 'table.json', filename: 'report.pdf', status: 'uploading' as const };
const listing = (name: string, path = '') => ({ nodes: [{ path: path ? `${path}/${name}` : name, name, type: 'file' }] }) as Awaited<ReturnType<typeof listDir>>;
function status(patch: Partial<ETLTaskStatus> = {}): ETLTaskStatus {
  return { task_id: 'real-1', status: 'completed', progress: 100, ...patch } as ETLTaskStatus;
}
function wrapper() {
  const cache = new Map();
  return function Wrapper({ children }: { children: ReactNode }) {
    return <StrictMode><SWRConfig value={{ provider: () => cache, dedupingInterval: 0, errorRetryCount: 0 }}><TaskProvider>{children}</TaskProvider></SWRConfig></StrictMode>;
  };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}
let commands: ReturnType<typeof useTaskActions>;
function Controls() { commands = useTaskActions('p', 'org-a'); return null; }
function Cell({ tableId = 'table.json' }: { tableId?: string }) {
  return <ValueRenderer value={null} nodeKey="report.pdf" tableId={tableId} isExpanded={false} isExpandable={false} onChange={() => {}} onToggle={() => {}} onSelect={() => {}} />;
}
function Files() {
  const root = useTreeDir('p', '');
  const expanded = useTreeDir('p', 'docs');
  useTreeDir('q', '');
  useProjects('org-a');
  useProjects('org-b');
  useTable('p', 'table.json');
  return <><div data-testid="root">{root.nodes.map(node => node.name).join(',')}</div><div data-testid="expanded">{expanded.nodes.map(node => node.name).join(',')}</div></>;
}
function mount(extra?: ReactNode) {
  return render(<><Controls /><BackgroundTaskNotifier /><TaskStatusWidget /><Cell />{extra}</>, { wrapper: wrapper() });
}

beforeEach(() => {
  identity.userId = 'account-a'; identity.projectId = 'p'; sessionStorage.clear();
  vi.mocked(batchGetETLTaskStatus).mockReset().mockResolvedValue({ tasks: [], total: 0 });
  vi.mocked(listDir).mockReset().mockImplementation(async (_project, path) => listing('before.md', path));
  vi.mocked(getProjects).mockReset().mockResolvedValue([]);
  vi.mocked(getTable).mockReset().mockResolvedValue({} as never);
  vi.mocked(uploadFiles).mockReset();
  vi.mocked(getProjectImportJobs).mockReset().mockResolvedValue({ jobs: [], total: 0 });
});
afterEach(() => { vi.useRealTimers(); });

describe('task refresh behavior', () => {
  it('shows new uploads, live progress and terminal table placeholders', async () => {
    mount();
    await act(async () => commands.addPendingTasks([task]));
    expect(await screen.findByText('report.pdf')).toBeTruthy();
    expect(screen.getByText('Processing…')).toBeTruthy();
    await act(async () => commands.updateTaskProgress(task.taskId, 42));
    expect(screen.getByText('Uploading 42%')).toBeTruthy();
    await act(async () => commands.replaceTaskId(task.taskId, 'real-1'));
    expect(screen.getByText('Uploading 42%')).toBeTruthy();
    await act(async () => commands.updateTaskStatusById('real-1', 'finalizing'));
    expect(batchGetETLTaskStatus).not.toHaveBeenCalled();
    await act(async () => commands.updateTaskStatusById('real-1', 'completed'));
    expect(screen.queryByText('Processing…')).toBeNull();
    expect(screen.getByText('Completed')).toBeTruthy();
    fireEvent.click(screen.getByTitle('Clear all'));
    expect(screen.queryByText('report.pdf')).toBeNull();
  });

  it('keeps a collapsed widget collapsed on progress and does not fetch files', async () => {
    mount(<Files />);
    await waitFor(() => expect(screen.getByTestId('root').textContent).toBe('before.md'));
    await act(async () => commands.addPendingTasks([task]));
    fireEvent.click(screen.getByTitle('Collapse'));
    const reads = vi.mocked(listDir).mock.calls.length;
    await act(async () => { commands.updateTaskProgress(task.taskId, 15); commands.updateTaskProgress(task.taskId, 50); });
    expect(screen.queryByTitle('Collapse')).toBeNull();
    expect(listDir).toHaveBeenCalledTimes(reads);
    expect(batchGetETLTaskStatus).not.toHaveBeenCalled();
    expect(JSON.parse(sessionStorage.getItem(taskStorageKey('account-a'))!)[0].progress).toBe(50);
  });

  it.each(['polled', 'inline'] as const)('%s completion refreshes root, expanded folders, table and owning project list in the current provider', async mode => {
    const completion = deferred<{ tasks: ETLTaskStatus[]; total: number }>();
    vi.mocked(batchGetETLTaskStatus).mockReturnValue(completion.promise);
    mount(<Files />);
    await waitFor(() => expect(getTable).toHaveBeenCalledTimes(1));
    await act(async () => commands.addPendingTasks([{ ...task, taskId: 'real-1', status: mode === 'polled' ? 'pending' : 'finalizing' }]));
    const next = deferred<Awaited<ReturnType<typeof listDir>>>();
    vi.mocked(listDir).mockImplementation((_project, path) => path === '' ? next.promise : Promise.resolve(listing('after.md', path)));
    await act(async () => {
      if (mode === 'polled') completion.resolve({ tasks: [status()], total: 1 });
      else commands.updateTaskStatusById('real-1', 'completed');
    });
    // Revalidation must retain successful content while the server is slow.
    expect(screen.getByTestId('root').textContent).toBe('before.md');
    await act(async () => next.resolve(listing('after.md')));
    expect(screen.getByTestId('root').textContent).toBe('after.md');
    expect(screen.getByTestId('expanded').textContent).toBe('after.md');
    expect(vi.mocked(listDir).mock.calls.filter(([project]) => project === 'q')).toHaveLength(1);
    expect(vi.mocked(getProjects).mock.calls.filter(([org]) => org === 'org-a')).toHaveLength(2);
    expect(vi.mocked(getProjects).mock.calls.filter(([org]) => org === 'org-b')).toHaveLength(1);
    expect(getTable).toHaveBeenCalledTimes(2);
    expect(screen.getByText('Completed')).toBeTruthy();
    const reads = vi.mocked(listDir).mock.calls.length;
    await act(async () => commands.updateTaskStatusById('real-1', 'completed'));
    expect(listDir).toHaveBeenCalledTimes(reads);
  });

  it('keeps the three-second poll cadence, skips client/connector/terminal tasks and updates backend progress', async () => {
    vi.useFakeTimers();
    vi.mocked(batchGetETLTaskStatus).mockResolvedValue({ tasks: [status({ status: 'processing', progress: 25 })], total: 1 });
    mount();
    await act(async () => commands.addPendingTasks([
      task, { ...task, taskId: 'placeholder-1', status: 'pending' },
      { ...task, taskId: '-2', status: 'pending' }, { ...task, taskId: 'final', status: 'finalizing' },
      { ...task, taskId: 'saas', taskType: 'notion', status: 'processing' },
      { ...task, taskId: 'done', status: 'completed' }, { ...task, taskId: 'real-1', status: 'pending' },
    ]));
    expect(batchGetETLTaskStatus).toHaveBeenCalledExactlyOnceWith(['real-1'], 'token-account-a');
    await act(async () => { await vi.advanceTimersByTimeAsync(2999); });
    expect(batchGetETLTaskStatus).toHaveBeenCalledTimes(1);
    vi.mocked(batchGetETLTaskStatus).mockResolvedValue({ tasks: [status({ status: 'processing', progress: 75 })], total: 1 });
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(batchGetETLTaskStatus).toHaveBeenCalledTimes(2);
    expect(commands.snapshot().find(item => item.taskId === 'real-1')?.progress).toBe(75);
    vi.mocked(batchGetETLTaskStatus).mockResolvedValue({ tasks: [status({ status: 'failed', error: 'parse failed' })], total: 1 });
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(commands.snapshot().find(item => item.taskId === 'real-1')?.error).toBe('parse failed');
    await act(async () => { await vi.advanceTimersByTimeAsync(9000); });
    expect(batchGetETLTaskStatus).toHaveBeenCalledTimes(3);
  });

  it('merges a late poll against current tasks without resurrecting removals or losing new uploads', async () => {
    const response = deferred<{ tasks: ETLTaskStatus[]; total: number }>();
    vi.mocked(batchGetETLTaskStatus).mockReturnValue(response.promise);
    mount();
    await act(async () => commands.addPendingTasks([{ ...task, taskId: 'real-1', status: 'pending' }]));
    await act(async () => { commands.removeTaskById('p', 'real-1'); commands.addPendingTasks([{ ...task, taskId: 'new' }]); });
    await act(async () => response.resolve({ tasks: [status()], total: 1 }));
    expect(commands.snapshot().map(item => item.taskId)).toEqual(['new']);
    expect(screen.queryByText('Completed')).toBeNull();
  });

  it('isolates projects and tables even when filenames/task IDs match', async () => {
    const mounted = mount();
    await act(async () => commands.addPendingTasks([{ ...task, projectId: 'q' }, { ...task, tableId: 'other.json' }]));
    expect(screen.queryByText('Processing…')).toBeNull();
    await act(async () => commands.updateTaskStatusById(task.taskId, 'completed'));
    expect(commands.snapshot().find(item => item.projectId === 'q')?.status).toBe('uploading');
    identity.projectId = 'q';
    mounted.rerender(<><Controls /><Cell /></>);
    expect(screen.getByText('Processing…')).toBeTruthy();
  });

  it('isolates accounts, clears old snapshots and rejects callbacks captured before logout or switching back', async () => {
    const response = deferred<{ tasks: ETLTaskStatus[]; total: number }>();
    vi.mocked(batchGetETLTaskStatus).mockReturnValue(response.promise);
    const mounted = mount(<Files />);
    await act(async () => commands.addPendingTasks([{ ...task, taskId: 'real-1', status: 'pending' }]));
    const old = commands;
    identity.userId = 'account-b';
    mounted.rerender(<><Controls /><BackgroundTaskNotifier /><TaskStatusWidget /><Files /></>);
    expect(screen.queryByText('report.pdf')).toBeNull();
    expect(sessionStorage.getItem(taskStorageKey('account-a'))).toBeNull();
    const reads = vi.mocked(listDir).mock.calls.length;
    await act(async () => { old.addPendingTasks([task]); old.updateTaskStatusById('real-1', 'completed'); response.resolve({ tasks: [status()], total: 1 }); });
    expect(listDir).toHaveBeenCalledTimes(reads);
    expect(screen.queryByText('report.pdf')).toBeNull();
    identity.userId = 'account-a';
    mounted.rerender(<><Controls /><TaskStatusWidget /></>);
    await act(async () => old.addPendingTasks([task]));
    expect(screen.queryByText('report.pdf')).toBeNull();
    identity.userId = null;
    mounted.rerender(<><Controls /><TaskStatusWidget /></>);
    await act(async () => commands.addPendingTasks([task]));
    expect(screen.queryByText('report.pdf')).toBeNull();
  });

  it('does not broadcast task updates into another SWR provider', async () => {
    const first = renderHook(() => ({ actions: useTaskActions('p'), tasks: usePendingTasks() }), { wrapper: wrapper() });
    const second = renderHook(() => usePendingTasks(), { wrapper: wrapper() });
    await act(async () => {});
    await act(async () => first.result.current.actions.addPendingTasks([task]));
    expect(first.result.current.tasks).toHaveLength(1);
    expect(second.result.current).toEqual([]);
  });

  it('restores only owned storage and responds to explicit list-key invalidation without update loops', async () => {
    sessionStorage.setItem('etl_pending_tasks', JSON.stringify([{ ...task, filename: 'legacy-secret.pdf' }]));
    sessionStorage.setItem(taskStorageKey('account-a'), JSON.stringify([{ ...task, timestamp: Date.now() }]));
    const hook = renderHook(() => ({ tasks: usePendingTasks(), mutate: useSWRConfig().mutate }), { wrapper: wrapper() });
    await waitFor(() => expect(hook.result.current.tasks).toHaveLength(1));
    expect(hook.result.current.tasks[0].filename).toBe('report.pdf');
    sessionStorage.setItem(taskStorageKey('account-a'), '[]');
    await act(async () => { await hook.result.current.mutate(etlTaskKeys.list('account-a')); });
    expect(hook.result.current.tasks).toEqual([]);
    const empty = hook.result.current.tasks;
    hook.rerender();
    expect(hook.result.current.tasks).toBe(empty);
  });

  it('refreshes a completed upload after its initiating component unmounts', async () => {
    let importer!: ReturnType<typeof useFileImport>;
    function Upload() { importer = useFileImport('p', 'token', { orgId: 'org-a' }); return null; }
    const response = deferred<Awaited<ReturnType<typeof uploadFiles>>>();
    let callbacks!: NonNullable<Parameters<typeof uploadFiles>[2]>;
    vi.mocked(uploadFiles).mockImplementation((_params, _token, handlers) => {
      callbacks = handlers!;
      callbacks.onUploadStart?.([{ fileIndex: 0, filename: 'report.pdf', size: 1 }]);
      return response.promise;
    });
    const mounted = render(<><Upload /><TaskStatusWidget /><Files /></>, { wrapper: wrapper() });
    await waitFor(() => expect(screen.getByTestId('root').textContent).toBe('before.md'));
    let upload!: Promise<void>;
    act(() => { upload = importer.handleFileImportConfirm([new File(['x'], 'report.pdf')], 'raw'); });
    expect(await screen.findByText('report.pdf')).toBeTruthy();
    mounted.rerender(<><TaskStatusWidget /><Files /></>);
    await act(async () => callbacks.onTaskCreated?.({ fileIndex: 0, taskId: 'inline-1', filename: 'report.pdf', size: 1 }));
    await act(async () => callbacks.onProgress?.('inline-1', 1, 1, 100));
    expect(screen.getByText('Uploading 100%')).toBeTruthy();
    vi.mocked(listDir).mockImplementation(async (_project, path) => listing('report.pdf', path));
    await act(async () => {
      callbacks.onAllPartsUploaded?.('inline-1');
      callbacks.onTaskCompleted?.('inline-1');
      response.resolve([{ taskId: 'inline-1', status: 'completed' }] as Awaited<ReturnType<typeof uploadFiles>>);
      await upload;
    });
    expect(screen.getByText('Completed')).toBeTruthy();
    expect(screen.getByTestId('root').textContent).toBe('report.pdf');
    // Known upload targets keep unrelated expanded directories cached.
    expect(screen.getByTestId('expanded').textContent).toBe('before.md');
    expect(getProjects).toHaveBeenLastCalledWith('org-a');
  });

  it('refreshes an unmounted file query when it is reopened after completion', async () => {
    const mounted = mount(<Files />);
    await waitFor(() => expect(screen.getByTestId('root').textContent).toBe('before.md'));
    await act(async () => commands.addPendingTasks([task]));
    mounted.rerender(<Controls />);
    vi.mocked(listDir).mockImplementation(async (_project, path) => listing('fresh.md', path));
    await act(async () => commands.updateTaskStatusById(task.taskId, 'completed'));
    mounted.rerender(<><Controls /><Files /></>);
    await waitFor(() => expect(screen.getByTestId('root').textContent).toBe('fresh.md'));
  });

  it('retains file content on refresh failure and can retry the same key', async () => {
    const hook = renderHook(() => ({ actions: useTaskActions('p'), tree: useTreeDir('p', '') }), { wrapper: wrapper() });
    await waitFor(() => expect(hook.result.current.tree.nodes).toHaveLength(1));
    await act(async () => hook.result.current.actions.addPendingTasks([task]));
    vi.mocked(listDir).mockRejectedValueOnce(new Error('offline'));
    await act(async () => hook.result.current.actions.updateTaskStatusById(task.taskId, 'completed'));
    expect(hook.result.current.tree.nodes[0].name).toBe('before.md');
    expect(hook.result.current.tree.error?.message).toBe('offline');
    vi.mocked(listDir).mockResolvedValueOnce(listing('recovered.md'));
    await act(async () => { await hook.result.current.tree.refresh(); });
    expect(hook.result.current.tree.nodes[0].name).toBe('recovered.md');
  });

  it('bounds completion invalidation by account, project, organization and known directory paths', () => {
    const target = { ...task, folderPaths: ['', 'docs'] };
    expect(isTaskCompletionKey(['tree', 'p', 'docs'], 'account-a', target)).toBe(true);
    expect(isTaskCompletionKey(['tree', 'p', 'unrelated'], 'account-a', target)).toBe(false);
    expect(isTaskCompletionKey(['tree', 'q', 'docs'], 'account-a', target)).toBe(false);
    expect(isTaskCompletionKey(['projects', 'org-b'], 'account-a', target)).toBe(false);
    expect(isTaskCompletionKey(['resource-dashboard', 'account-b', 'p'], 'account-a', target)).toBe(false);
    expect(isTaskCompletionKey(['resource-dashboard', 'account-a', 'p'], 'account-a', target)).toBe(true);
    expect(isTaskCompletionKey(['repo-identity', 'p'], 'account-a', target)).toBe(true);
    expect(isTaskCompletionKey(['synchronize-status', 'p'], 'account-a', target)).toBe(true);
    expect(isTaskCompletionKey(['project-history', 'p'], 'account-a', target)).toBe(true);
    expect(isTaskCompletionKey('projects', 'account-a', target)).toBe(false);
  });

  it('keeps unload protection and the 30-minute stale sweep', async () => {
    vi.useFakeTimers();
    mount();
    await act(async () => commands.addPendingTasks([task]));
    const warning = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(warning);
    expect(warning.defaultPrevented).toBe(true);
    await act(async () => { await vi.advanceTimersByTimeAsync(31 * 60_000); });
    expect(commands.snapshot()[0].status).toBe('failed');
    expect(commands.snapshot()[0].error).toContain('30 minutes');
    const safe = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(safe);
    expect(safe.defaultPrevented).toBe(false);
  });
});

describe('Import completion', () => {
  it('refreshes Files through SWR, keeps the snapshot during reads and ignores repeated completion', async () => {
    const job = { id: 'import-1', project_id: 'p', org_id: 'org-a', status: 'running' } as ImportJob;
    vi.mocked(getProjectImportJobs).mockResolvedValue({ jobs: [job], total: 1 });
    const hook = renderHook(() => ({ imports: useProjectImportJobs('p'), root: useTreeDir('p', ''), expanded: useTreeDir('p', 'docs') }), { wrapper: wrapper() });
    await waitFor(() => expect(hook.result.current.imports.latestJob?.status).toBe('running'));
    vi.mocked(listDir).mockImplementation(async (_project, path) => listing('imported.md', path));
    vi.mocked(getProjectImportJobs).mockResolvedValue({ jobs: [{ ...job, status: 'completed' }], total: 1 });
    await act(async () => { await hook.result.current.imports.refresh(); });
    await waitFor(() => expect(hook.result.current.root.nodes[0].name).toBe('imported.md'));
    expect(hook.result.current.expanded.nodes[0].name).toBe('imported.md');
    const reads = vi.mocked(listDir).mock.calls.length;
    await act(async () => { await hook.result.current.imports.refresh(); });
    expect(listDir).toHaveBeenCalledTimes(reads);
    expect(getProjectImportJobs).toHaveBeenCalledWith('p', { limit: 20 });
  });

  it('keeps import polling at three seconds and stops on failure without invalidating files', async () => {
    vi.useFakeTimers();
    const job = { id: 'import-1', project_id: 'p', status: 'running' } as ImportJob;
    vi.mocked(getProjectImportJobs).mockResolvedValue({ jobs: [job], total: 1 });
    const hook = renderHook(() => ({ imports: useProjectImportJobs('p'), root: useTreeDir('p', '') }), { wrapper: wrapper() });
    await act(async () => {});
    expect(getProjectImportJobs).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(2999); });
    expect(getProjectImportJobs).toHaveBeenCalledTimes(1);
    vi.mocked(getProjectImportJobs).mockResolvedValue({ jobs: [{ ...job, status: 'failed' }], total: 1 });
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(getProjectImportJobs).toHaveBeenCalledTimes(2);
    expect(hook.result.current.imports.latestJob?.status).toBe('failed');
    expect(listDir).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(9000); });
    expect(getProjectImportJobs).toHaveBeenCalledTimes(2);
  });

  it('never adopts a late import list from another account or project', async () => {
    const response = deferred<{ jobs: ImportJob[]; total: number }>();
    vi.mocked(getProjectImportJobs).mockReturnValueOnce(response.promise).mockResolvedValue({ jobs: [], total: 0 });
    const hook = renderHook(({ project }) => useProjectImportJobs(project), { initialProps: { project: 'p' }, wrapper: wrapper() });
    identity.userId = 'account-b';
    hook.rerender({ project: 'q' });
    await act(async () => response.resolve({ jobs: [{ id: 'old', project_id: 'p', status: 'completed' } as ImportJob], total: 1 }));
    expect(hook.result.current.jobs).toEqual([]);
  });
});
