'use client';

import { createContext, useContext, useEffect, useMemo, useRef, type ReactNode } from 'react';
import useSWR, { useSWRConfig } from 'swr';
import { useAuth } from '@/contexts/SupabaseAuthProvider';
import { createTaskActions } from './actions';
import { EMPTY_PROGRESS, EMPTY_TASKS, etlTaskKeys, readStoredTasks, taskStorageKey, type PendingTask } from './model';

const TaskContext = createContext<ReturnType<typeof createTaskActions> | null>(null);

/** Only command lifetime belongs here; task data is owned by the SWR provider. */
export function TaskProvider({ children }: { children: ReactNode }) {
  const { userId } = useAuth();
  const { cache, mutate } = useSWRConfig();
  const generation = useMemo(() => ({ userId }), [userId]);
  const identity = useRef(generation);
  identity.current = generation;
  const previous = useRef(userId);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  useEffect(() => {
    const old = previous.current;
    previous.current = userId;
    // Unowned legacy storage cannot safely be attributed to the current account.
    try { sessionStorage.removeItem('etl_pending_tasks'); } catch { /* Storage may be disabled. */ }
    if (!old || old === userId) return;
    try { sessionStorage.removeItem(taskStorageKey(old)); } catch { /* Live cache is still cleared. */ }
    void mutate(etlTaskKeys.list(old), undefined, { revalidate: false });
    void mutate(etlTaskKeys.progress(old), undefined, { revalidate: false });
  }, [mutate, userId]);
  const actions = useMemo(() => createTaskActions({
    accountId: userId ?? '', cache, mutate,
    isActive: () => Boolean(userId) && identity.current === generation && mounted.current,
  }), [cache, generation, mutate, userId]);
  return <TaskContext.Provider value={actions}>{children}</TaskContext.Provider>;
}

export function useTaskActions(projectId?: string | null, orgId?: string) {
  const actions = useContext(TaskContext);
  if (!actions) throw new Error('Task actions require TaskProvider');
  return useMemo(() => ({
    ...actions,
    addPendingTasks: (tasks: Omit<PendingTask, 'timestamp'>[]) => actions.addPendingTasks(tasks.map(task => ({ ...task, orgId: task.orgId ?? orgId }))),
    updateTaskStatusById: (taskId: string, status: PendingTask['status'], patch?: Partial<Pick<PendingTask, 'progress' | 'error'>>) => {
      if (projectId) actions.updateTaskStatusById(projectId, taskId, status, patch);
    },
    replaceTaskId: (oldId: string, newId: string) => { if (projectId) actions.replaceTaskId(projectId, oldId, newId); },
    updateTaskProgress: (taskId: string, progress: number) => { if (projectId) actions.updateTaskProgress(projectId, taskId, progress); },
  }), [actions, orgId, projectId]);
}

const localQueryConfig = { revalidateOnFocus: false, revalidateOnReconnect: false };

export function usePendingTasks(projectId?: string | null) {
  const { userId } = useAuth();
  const { data } = useSWR(userId ? etlTaskKeys.list(userId) : null,
    () => readStoredTasks(userId!), localQueryConfig);
  const tasks = userId ? data ?? EMPTY_TASKS : EMPTY_TASKS;
  return useMemo(() => projectId === undefined ? tasks : tasks.filter(task => task.projectId === projectId), [projectId, tasks]);
}

export function useTaskProgress() {
  const { userId } = useAuth();
  const { data } = useSWR<Record<string, number>>(userId ? etlTaskKeys.progress(userId) : null, null, localQueryConfig);
  return userId ? data ?? EMPTY_PROGRESS : EMPTY_PROGRESS;
}
