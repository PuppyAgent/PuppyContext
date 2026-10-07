export const workspaceKeys = {
  history: (projectId: string) => ['project-history', projectId] as const,
  pendingConflicts: (projectId: string) => ['pending-conflicts', projectId] as const,
  tree: (projectId: string, path: string) => ['tree', projectId, path] as const,
  tools: (projectId: string, path: string) => ['tools-by-path', projectId, path] as const,
};

export function changedFolders(paths: readonly string[]) {
  const folders = new Set(['']);
  for (const path of paths) {
    // The version-notification contract uses project-root-relative paths.
    const parts = path.replace(/^\/+|\/+$/g, '').split('/');
    for (let i = 1; i < parts.length; i++) folders.add(parts.slice(0, i).join('/'));
  }
  return folders;
}

export function isCommitInvalidationKey(key: unknown, projectId: string, folders: ReadonlySet<string>) {
  if (!Array.isArray(key) || key[1] !== projectId) return false;
  if (key[0] === 'tree') return folders.has(key[2]);
  return ['project-history', 'pending-conflicts', 'activity', 'table'].includes(key[0]);
}

export const etlTaskKeys = {
  list: (accountId: string) => ['etl-tasks', accountId] as const,
  progress: (accountId: string) => ['etl-progress', accountId] as const,
};

export const importJobKeys = {
  list: (accountId: string, projectId: string) => ['import-jobs', accountId, projectId] as const,
};
