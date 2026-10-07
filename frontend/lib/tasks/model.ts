/**
 * 任务类型
 */
export type TaskType = 'file' | 'notion' | 'github' | 'airtable' | 'google_sheets' | 'google_docs' | 'linear' | 'gmail' | 'drive' | 'calendar';

/**
 * Task record, persisted in account-scoped sessionStorage across reloads.
 *
 * Lifecycle for a direct-to-S3 file upload:
 *   - ``uploading``   — client is PUTing parts to S3; ``progress``
 *     ticks 0..100. Polling skips this state because the client is
 *     authoritative.
 *   - ``finalizing``  — every part is up in S3, the client is
 *     waiting on ``/upload/complete`` to write the assembled bytes
 *     into the Version Engine. This is a CLIENT-driven temporary state; polling
 *     skips it so a transient backend ``pending`` snapshot can't
 *     regress the widget back to a worse-looking label. The
 *     transition out is always driven by ``onTaskCompleted`` /
 *     ``onTaskFailed`` from ``uploadApi``.
 *   - ``pending``     — legacy: enqueued for a worker. Direct-S3
 *     flow doesn't visit this state anymore (finalize is inline)
 *     but we keep it for SaaS connectors that still go through it.
 *   - ``processing``  — worker is doing OCR / LLM / writing versioned content
 *     (SaaS connectors only; direct-S3 finalize is inline).
 *   - ``completed`` / ``failed`` / ``cancelled`` — terminal; the
 *     widget stops animating and exposes a clear/cancel affordance.
 */
export interface PendingTask {
  taskId: string;
  projectId: string;
  orgId?: string;
  /** Known affected directories for uploads; absent for worker tasks with no path. */
  folderPaths?: string[];
  tableId?: string;
  tableName?: string;
  filename: string;
  timestamp: number;
  taskType?: TaskType;
  status?:
    | 'uploading'
    | 'finalizing'
    | 'pending'
    | 'processing'
    | 'completed'
    | 'failed'
    | 'cancelled';
  /**
   * Upload progress 0..100. Populated only during the ``uploading``
   * phase; once we hand off to the worker the backend's task
   * progress takes over (which is coarser — 80, 100 — because
   * "writing versioned content" doesn't have meaningful sub-progress).
   */
  progress?: number;
  /** Last-seen error string for failed tasks. */
  error?: string;
}


export const EMPTY_TASKS: PendingTask[] = [];
export const EMPTY_PROGRESS: Record<string, number> = {};
export const STALE_UPLOADING_MS = 30 * 60 * 1000;

export function isPlaceholderTaskId(taskId: string): boolean {
  return taskId.startsWith('tmp-') || taskId.startsWith('placeholder-') || Number(taskId) < 0;
}

export function isTaskTerminal(status: PendingTask['status']): boolean {
  return status === 'completed' || status === 'failed' || status === 'cancelled';
}

export function isPollable(task: PendingTask): boolean {
  return !isPlaceholderTaskId(task.taskId) && (!task.taskType || task.taskType === 'file') &&
    task.status !== 'uploading' && task.status !== 'finalizing' && !isTaskTerminal(task.status);
}

export function taskStorageKey(accountId: string) {
  return `etl_pending_tasks:${accountId}`;
}

export function readStoredTasks(accountId: string): PendingTask[] {
  if (typeof sessionStorage === 'undefined') return EMPTY_TASKS;
  try {
    const tasks: unknown = JSON.parse(sessionStorage.getItem(taskStorageKey(accountId)) || '[]');
    return Array.isArray(tasks) ? tasks : EMPTY_TASKS;
  } catch {
    return EMPTY_TASKS;
  }
}

export function persistTasks(accountId: string, tasks: PendingTask[]) {
  try {
    sessionStorage.setItem(taskStorageKey(accountId), JSON.stringify(tasks));
  } catch (error) {
    // Storage can be unavailable or full; live SWR updates must still work.
    console.warn('Unable to persist background tasks:', error);
  }
}
