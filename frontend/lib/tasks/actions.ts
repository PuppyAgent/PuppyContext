import { unstable_serialize, type Cache, type ScopedMutator } from 'swr';
import type { ETLTaskStatus } from '@/lib/etlApi';
import { invalidateTaskCompletion } from './invalidation';
import { isPollable, persistTasks, readStoredTasks, STALE_UPLOADING_MS, type PendingTask } from './model';

import { etlTaskKeys } from '@/lib/queryKeys';

type NewTask = Omit<PendingTask, 'timestamp'>;
type TaskPatch = Partial<Pick<PendingTask, 'progress' | 'error'>>;
type TaskScope = { accountId: string; cache: Cache; mutate: ScopedMutator; isActive: () => boolean };

export function createTaskActions(scope: TaskScope) {
  const { accountId, cache, mutate, isActive } = scope;
  const key = etlTaskKeys.list(accountId);
  const progressKey = etlTaskKeys.progress(accountId);
  const snapshot = (): PendingTask[] => {
    const tasks: PendingTask[] = cache.get(unstable_serialize(key))?.data ?? readStoredTasks(accountId);
    const progress: Record<string, number> = cache.get(unstable_serialize(progressKey))?.data ?? {};
    return tasks.map(task => ({ ...task, progress: task.status === 'uploading' ? progress[JSON.stringify([task.projectId, task.taskId])] ?? task.progress : task.progress }));
  };
  const write = (update: (tasks: PendingTask[]) => PendingTask[]) => {
    if (!isActive()) return;
    const before = snapshot();
    const next = update(before);
    if (next === before) return;
    persistTasks(accountId, next);
    void mutate(key, next, { revalidate: false });
    const activeProgress = new Set(next.filter(task => task.status === 'uploading').map(task => JSON.stringify([task.projectId, task.taskId])));
    void mutate<Record<string, number>>(progressKey, current => Object.fromEntries(
      Object.entries(current ?? {}).filter(([id]) => activeProgress.has(id)),
    ), { revalidate: false });
    const completed = next.filter(task => task.status === 'completed' && before.some(
      previous => previous.taskId === task.taskId && previous.projectId === task.projectId && previous.status !== 'completed',
    ));
    for (const task of completed) void invalidateTaskCompletion(mutate, accountId, task);
  };
  const updateTask = (projectId: string, taskId: string, patch: Partial<PendingTask>) => write(tasks => tasks.map(
    task => task.projectId === projectId && task.taskId === taskId ? { ...task, ...patch } : task,
  ));

  return {
    snapshot,
    addPendingTasks: (tasks: NewTask[]) => write(current => [...current, ...tasks.map(task => ({ ...task, timestamp: Date.now() }))]),
    updateTaskStatusById: (projectId: string, taskId: string, status: PendingTask['status'], patch: TaskPatch = {}) => updateTask(projectId, taskId, { status, ...patch }),
    replaceTaskId: (projectId: string, oldId: string, newId: string) => updateTask(projectId, oldId, { taskId: newId }),
    removeTaskById: (projectId: string, taskId: string) => write(tasks => tasks.filter(task => task.projectId !== projectId || task.taskId !== taskId)),
    clearAllTasks: () => {
      if (!isActive()) return;
      write(() => []);
      void mutate(progressKey, {}, { revalidate: false });
    },
    updateTaskProgress: (projectId: string, taskId: string, progress: number) => {
      if (!isActive()) return;
      const tasks = snapshot();
      const task = tasks.find(item => item.projectId === projectId && item.taskId === taskId);
      if (!task || task.status !== 'uploading') return;
      // Progress has its own subscription: no list invalidation or backend read.
      const id = JSON.stringify([projectId, taskId]);
      void mutate<Record<string, number>>(progressKey, current => ({ ...current, [id]: progress }), { revalidate: false });
      persistTasks(accountId, tasks.map(item => item === task ? { ...item, progress } : item));
    },
    applyPoll: (tasks: ETLTaskStatus[]) => write(current => mergePolledTasks(current, tasks)),
    sweep: () => write(sweepStalledTasks),
  };
}

function mergePolledTasks(current: PendingTask[], statuses: ETLTaskStatus[]) {
  let changed = false;
  const next = current.map(task => {
    const status = statuses.find(item => item.task_id === task.taskId && (!item.project_id || item.project_id === task.projectId));
    if (!status || !isPollable(task)) return task;
    if (task.status === status.status && task.progress === status.progress && task.error === status.error) return task;
    changed = true;
    return { ...task, status: status.status, progress: status.progress, error: status.error };
  });
  return changed ? next : current;
}

function sweepStalledTasks(tasks: PendingTask[]) {
  let changed = false;
  const next = tasks.map(task => {
    if ((task.status !== 'uploading' && task.status !== 'finalizing') || Date.now() - task.timestamp <= STALE_UPLOADING_MS) return task;
    changed = true;
    return { ...task, status: 'failed' as const, error: task.error || 'Upload stalled — no progress in 30 minutes' };
  });
  return changed ? next : tasks;
}
