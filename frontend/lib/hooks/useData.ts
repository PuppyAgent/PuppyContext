/**
 * SWR 数据请求 Hooks
 *
 * 提供缓存、去重、自动重新验证的数据请求能力
 */

import useSWR, { mutate } from 'swr';
import { useMemo } from 'react';
import { workspaceKeys } from '@/lib/queryKeys';
import { readState } from '@/lib/queryState';
import {
  getProjects,
  getProject,
  getTable,
  getOrphanTables,
  type ProjectInfo,
  type TableData,
  type TableInfo,
} from '../projectsApi';
import {
  getToolsByProjectId,
  getToolsByPath,
  type Tool,
} from '../mcpApi';
import {
  directChildrenOf,
  listDir,
  normalizeTreePath,
  sortNodes,
  type NodeInfo,
} from '../contentTreeApi';
import { listSynchronizeProviders as getConnectorSpecs, type SynchronizeProviderSpec as ConnectorSpec } from '../synchronizeApi';

// Shared immutable-by-convention fallback: effect dependencies must not
// change identity merely because a disabled/pending query has no payload.
const EMPTY_TOOLS: Tool[] = [];
const EMPTY_PROJECTS: ProjectInfo[] = [];
const EMPTY_NODES: NodeInfo[] = [];

// SWR 配置：关闭自动重新验证，依赖手动刷新
const defaultConfig = {
  revalidateOnFocus: false, // 窗口聚焦时不自动刷新
  revalidateOnReconnect: false, // 网络恢复时不自动刷新
  dedupingInterval: 30000, // 30秒内相同请求去重
  errorRetryCount: 2, // 错误重试次数
};

/**
 * 获取项目列表
 *
 * - 自动缓存，多个组件共享同一份数据
 * - 30秒内不重复请求
 */
export function useProjects(orgId?: string | null) {
  const isDisabled = orgId === null;
  const key = isDisabled ? null : orgId ? ['projects', orgId] : 'projects';
  const {
    data,
    error,
    mutate: revalidate,
  } = useSWR<ProjectInfo[]>(
    key,
    () => getProjects(orgId ?? undefined),
    { ...defaultConfig, keepPreviousData: false }
  );
  const status = readState(!isDisabled, data, error);

  return {
    projects: isDisabled ? EMPTY_PROJECTS : data ?? EMPTY_PROJECTS,
    status,
    hasLoaded: status === 'ready',
    isLoading: status === 'loading',
    error,
    refresh: revalidate,
  };
}

/**
 * 获取单个项目详情。
 *
 * Used by project routes as the URL-level source of truth. This keeps a
 * refreshed `/projects/:projectId/...` page stable even before the selected
 * organization's project list has finished hydrating.
 */
export function useProject(projectId?: string | null) {
  const {
    data,
    error,
    isLoading,
    mutate: revalidate,
  } = useSWR<ProjectInfo>(
    projectId ? ['project', projectId] : null,
    () => getProject(projectId!),
    defaultConfig,
  );

  return {
    project: data ?? null,
    isLoading,
    error,
    refresh: revalidate,
  };
}

/**
 * 获取单个表数据
 *
 * @param projectId 项目 ID
 * @param tableId 表 ID (可选，为空时不请求)
 *
 * - 按需加载：只有 tableId 存在时才请求
 * - 自动缓存：相同 tableId 共享数据
 */
export function useTable(projectId: string, tableId: string | undefined) {
  const {
    data,
    error,
    isLoading,
    mutate: revalidate,
  } = useSWR<TableData>(
    // key: 只有 tableId 存在时才请求
    tableId ? ['table', projectId, tableId] : null,
    () => getTable(projectId, tableId!),
    {
      ...defaultConfig,
      dedupingInterval: 10000, // 表数据 10 秒去重
      keepPreviousData: false, // Never expose another file's data under this identity.
    }
  );

  return {
    tableData: data,
    isLoading,
    error,
    refresh: revalidate,
  };
}

/**
 * 获取裸 Table 列表（不属于任何 Project）
 */
export function useOrphanTables() {
  const {
    data,
    error,
    isLoading,
    mutate: revalidate,
  } = useSWR<TableInfo[]>('orphan-tables', getOrphanTables, defaultConfig);

  return {
    orphanTables: data ?? [],
    isLoading,
    error,
    refresh: revalidate,
  };
}

/**
 * 手动刷新项目列表（用于创建/删除项目后）
 * 
 * @returns Promise that resolves when the data is actually fetched
 */
export async function refreshProjects(orgId?: string | null) {
  void mutate('orphan-tables').catch(() => {});
  if (orgId === null) {
    return undefined;
  }
  if (orgId) {
    return mutate(['projects', orgId]);
  }
  return mutate('projects');
}

/**
 * Atomically upsert a project in the cached project list after create/update.
 *
 * This keeps the Projects dashboard from rendering a synthetic pending card
 * and then waiting for a second list fetch before counts/previews settle.
 */
export function upsertProjectCache(orgId: string | null | undefined, project: ProjectInfo) {
  const key = orgId ? ['projects', orgId] : 'projects';
  return mutate<ProjectInfo[]>(
    key,
    (current = []) => {
      const without = current.filter((p) => p.id !== project.id);
      return [project, ...without];
    },
    { revalidate: false },
  );
}

/**
 * 手动刷新指定表数据（用于保存后）
 */
export function refreshTable(projectId: string, tableId: string) {
  return mutate(['table', projectId, tableId]);
}

/**
 * 更新表数据缓存（乐观更新，不发请求）
 */
export function updateTableCache(
  projectId: string,
  tableId: string,
  newData: TableData
) {
  return mutate(['table', projectId, tableId], newData, { revalidate: false });
}

/**
 * Fetch directory listing for a given path (SWR cached).
 *
 * - Global cache: ExplorerSidebar / GridView / ListView share same data
 * - Keep only the same identity's cached snapshot while revalidating.
 */
export function useTreeDir(projectId: string, dirPath: string | null | undefined) {
  const normalizedPath = normalizeTreePath(dirPath);
  const key = projectId ? ['tree', projectId, normalizedPath] : null;
  const {
    data,
    error,
    isValidating,
    mutate: revalidate,
  } = useSWR<NodeInfo[]>(
    key,
    // Normalize the order at the fetcher boundary so that *whatever* the
    // backend returns ends up in canonical UI order in the SWR cache. This
    // keeps folder order stable at the fetcher boundary; see `sortNodes` for
    // the full rationale.
    () => listDir(projectId, normalizedPath)
      .then(r => sortNodes(directChildrenOf(r.nodes, normalizedPath))),
    {
      ...defaultConfig,
      dedupingInterval: 30000,
      keepPreviousData: false,
    }
  );
  const status = readState(Boolean(projectId), data, error);

  return {
    nodes: data ?? EMPTY_NODES,
    status,
    hasLoaded: status === 'ready',
    isLoading: status === 'loading',
    isValidating,
    error,
    refresh: revalidate,
  };
}

/** Use the current directory-backed content listing. */
export function useContentNodes(projectId: string, parentPath: string | null | undefined) {
  return useTreeDir(projectId, parentPath);
}

/**
 * Project explorer and main folder content are two views over the same path
 * listing. Share the canonical per-path SWR cache so mounting both panes does
 * not issue duplicate `/ls` requests. Expansion and selection remain local UI
 * state in the explorer components; they do not belong in the data cache.
 */
export function useExplorerTreeDir(projectId: string, dirPath: string | null | undefined) {
  return useTreeDir(projectId, dirPath);
}

export function useExplorerRootNodes(projectId: string) {
  const { nodes, isLoading, error, refresh } = useExplorerTreeDir(projectId, '');
  return {
    rootNodes: nodes,
    isLoading,
    error,
    refresh,
  };
}

/**
 * Manually refresh directory listing for a given path.
 */
export function refreshContentNodes(projectId: string, dirPath: string | null) {
  const normalizedPath = normalizeTreePath(dirPath);
  return mutate(['tree', projectId, normalizedPath]);
}

/**
 * Refresh all directory caches for a project.
 *
 * Use ONLY when the set of changed folders is genuinely unknown
 * (external sync/MCP/bot pushes, supabase saves, connector writes).
 * User-initiated mutations whose target
 * folders are known should call ``refreshFolderNodes`` instead —
 * a single rename re-fetching every cached folder in the project
 * is what made saves feel slow.
 */
export function refreshAllContentNodes(projectId: string) {
  return mutate(
    key => Array.isArray(key) && key[0] === 'tree' && key[1] === projectId,
  );
}

/**
 * Refresh only the directory caches for the given folder paths.
 *
 * Use this whenever the caller knows exactly which folder listings
 * the mutation affected:
 *   - create: parent folder
 *   - rename / delete: parent folder
 *   - move: source parent + target parent
 *
 * Pass ``''`` (empty string) for the project root. ``null`` /
 * ``undefined`` are normalised to root for callers that pass
 * ``currentFolderPath`` directly.
 *
 * Compared to ``refreshAllContentNodes`` this avoids re-fetching
 * unrelated folders the user has open elsewhere — a project with
 * 20 cached folder listings now does 1 round-trip per mutation
 * instead of 20.
 */
export function refreshFolderNodes(
  projectId: string,
  ...folderPaths: (string | null | undefined)[]
) {
  if (!folderPaths.length) return Promise.resolve();
  const unique = Array.from(
    new Set(folderPaths.map((p) => normalizeTreePath(p))),
  );
  // Explorer and content panes share this canonical path cache. Do not pass
  // `undefined` as mutate data: that clears the tree for one render and makes
  // both panes flash empty while revalidation is in flight.
  return Promise.all(
    unique.map(folderPath => mutate(['tree', projectId, folderPath])),
  );
}

/**
 * Refresh project history wherever it is mounted. Mutating content
 * should make the History page feel live even if the websocket event
 * is delayed or the user navigates there immediately after the action.
 */
export function refreshProjectHistory(projectId: string) {
  return mutate(
    key => Array.isArray(key) && key[0] === 'project-history' && key[1] === projectId,
  );
}

/**
 * 获取指定路径的 Tools（使用后端直接过滤）
 *
 * @param path version path (可选，为空时不请求)
 *
 * - 按需加载：只有 path 存在时才请求
 * - 后端过滤：直接调用 /api/v1/tools/by-path/{path}
 * - 自动缓存：相同 path 共享数据
 */
export function useToolsByPath(path: string | undefined, projectId = '') {
  // Project-aware callers select from the authorized project resource, shared
  // with the Files layout. Changing a cache key alone cannot fix a path-only
  // backend query (the same path may exist in multiple projects).
  const projectQuery = useProjectTools(path && projectId ? projectId : undefined);
  const {
    data: tableTools,
    error,
    isLoading,
    mutate: revalidate,
  } = useSWR<Tool[]>(
    path && !projectId ? workspaceKeys.tools('', path) : null,
    () => getToolsByPath(path!),
    {
      ...defaultConfig,
      dedupingInterval: 10000,
      keepPreviousData: false,
    }
  );

  const scopedTools = useMemo(() => path ? projectQuery.tools.filter(tool => tool.path === path) : EMPTY_TOOLS,
    [path, projectQuery.tools]);
  if (projectId) return {
    tools: scopedTools,
    isLoading: projectQuery.isLoading,
    error: projectQuery.error,
    refresh: projectQuery.refresh,
  };

  return {
    tools: tableTools ?? EMPTY_TOOLS,
    isLoading,
    error,
    refresh: revalidate,
  };
}

/**
 * 获取指定项目下的所有 Tools（聚合所有节点）
 */
export function useProjectTools(projectId: string | undefined) {
  const {
    data,
    error,
    isLoading,
    mutate: revalidate,
  } = useSWR<Tool[]>(
    projectId ? ['tools-by-project', projectId] : null,
    () => getToolsByProjectId(projectId!),
    {
      ...defaultConfig,
      dedupingInterval: 30000,
      keepPreviousData: false,
    }
  );

  return {
    tools: data ?? EMPTY_TOOLS,
    isLoading,
    error,
    refresh: revalidate,
  };
}

/**
 * 手动刷新指定项目的 Tools（用于：用户在 editor 侧栏配置权限后，ChatSidebar 立刻可见）
 */
export function refreshProjectTools(projectId?: string | null) {
  if (projectId) {
    return mutate(['tools-by-project', projectId]);
  }
  return Promise.resolve(undefined);
}

/**
 * 手动刷新指定路径的 Tools
 */
export function refreshToolsByPath(path?: string, projectId = '') {
  if (projectId) return refreshProjectTools(projectId);
  if (path) {
    return mutate(workspaceKeys.tools(projectId, path));
  }
  return Promise.resolve(undefined);
}

/**
 * Connector specs from backend (source of truth for sync providers)
 */
export function useConnectorSpecs() {
  const { data, error, isLoading } = useSWR<ConnectorSpec[]>(
    'connector-specs',
    getConnectorSpecs,
    { ...defaultConfig, dedupingInterval: 300000 }
  );

  return { specs: data ?? [], isLoading, error };
}
