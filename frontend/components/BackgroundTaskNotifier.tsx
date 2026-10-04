'use client';

import { useEffect } from 'react';
import { useAuth } from '@/contexts/SupabaseAuthProvider';
import { batchGetETLTaskStatus } from '@/lib/etlApi';
import { usePendingTasks, useTaskActions } from '@/features/tasks/TaskProvider';
import { isPollable } from '@/features/tasks/model';

/** The single backend task poller: immediately, then every three seconds while
 * real file tasks are pending/processing. Upload/finalize remain client-owned. */
export function BackgroundTaskNotifier() {
  const { session } = useAuth();
  const tasks = usePendingTasks();
  const actions = useTaskActions();
  const token = session?.access_token;
  const pollKey = JSON.stringify(tasks.filter(isPollable).map(task => [task.projectId, task.taskId]));

  useEffect(() => {
    if (!token || pollKey === '[]') return;
    let disposed = false;
    let checking = false;
    const check = async () => {
      if (checking) return;
      const ids = actions.snapshot().filter(isPollable).map(task => task.taskId);
      if (!ids.length) return;
      checking = true;
      try {
        const response = await batchGetETLTaskStatus(ids, token);
        if (!disposed) actions.applyPoll(response.tasks);
      } catch (error) {
        console.error('Failed to check ETL task status:', error);
      } finally {
        checking = false;
      }
    };
    void check();
    const timer = setInterval(check, 3000);
    return () => { disposed = true; clearInterval(timer); };
  }, [actions, pollKey, token]);

  const hasInFlight = tasks.some(task => task.status === 'uploading' || task.status === 'finalizing');
  useEffect(() => {
    if (!hasInFlight) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [hasInFlight]);

  useEffect(() => {
    actions.sweep();
    const timer = setInterval(actions.sweep, 60_000);
    return () => clearInterval(timer);
  }, [actions]);

  useEffect(() => {
    const debugWindow = window as Window & { clearETLTasks?: () => void };
    debugWindow.clearETLTasks = actions.clearAllTasks;
    return () => { delete debugWindow.clearETLTasks; };
  }, [actions]);
  return null;
}
