import type { ScopedMutator } from 'swr';
import type { PendingTask } from './model';

/** Use known upload directories when available; worker responses without paths
 * refresh the project's directory/table snapshots, including unmounted entries. */
export function isTaskCompletionKey(key: unknown, accountId: string, task: Pick<PendingTask, 'projectId' | 'orgId' | 'folderPaths'>) {
  if (!Array.isArray(key)) return false;
  if (key[0] === 'projects') return Boolean(task.orgId) && key[1] === task.orgId;
  if (key[0] === 'resource-dashboard') return key[1] === accountId && key[2] === task.projectId;
  if (key[0] === 'tree' && task.folderPaths && !task.folderPaths.includes(key[2])) return false;
  return key[1] === task.projectId && [
    'tree', 'table', 'project', 'project-history', 'project-history-overview',
    'home-tree', 'repo-identity', 'synchronize-status',
  ].includes(key[0]);
}

export function invalidateTaskCompletion(mutate: ScopedMutator, accountId: string, task: Pick<PendingTask, 'projectId' | 'orgId' | 'folderPaths'>) {
  // With one argument SWR retains the successful snapshot while revalidating.
  return mutate(key => isTaskCompletionKey(key, accountId, task));
}
