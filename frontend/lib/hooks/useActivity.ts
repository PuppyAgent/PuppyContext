import { useEffect } from 'react';
import useSWR from 'swr';
import { useAuth } from '@/contexts/SupabaseAuthProvider';
import {
  getProjectActivity,
  isActivityItemActive,
  type ActivityItem,
  type ActivityKind,
} from '@/lib/activityApi';

const DEFAULT_ACTIVITY_POLL_MS = 3000;

/**
 * Poll the unified activity feed for a project, optionally filtered to one
 * kind. Idle discovery remains slow but non-zero: another client can start a
 * job without a commit event. SWR suspends polling in hidden/offline tabs.
 */
export function useProjectActivity(
  projectId?: string | null,
  options?: { kind?: ActivityKind; activeOnly?: boolean; limit?: number },
) {
  const { userId, session, isAuthReady } = useAuth();
  const ready = Boolean(projectId && userId && isAuthReady);
  const kind = options?.kind;
  const activeOnly = options?.activeOnly ?? false;
  const limit = options?.limit ?? 20;

  const { data, error, isLoading, mutate } = useSWR(
    ready ? ['activity', userId, projectId, kind ?? 'all', activeOnly, limit] : null,
    () => getProjectActivity(projectId!, { kind, activeOnly, limit }),
    {
      revalidateOnFocus: true,
      revalidateOnReconnect: true,
      refreshInterval: (latest) => {
        const items = latest?.items ?? [];
        return items.some(isActivityItemActive) ? DEFAULT_ACTIVITY_POLL_MS : 30_000;
      },
    },
  );

  useEffect(() => { if (ready) void mutate(); }, [ready, session?.access_token, mutate]);

  const items: ActivityItem[] = data?.items ?? [];
  return {
    items,
    activeItems: items.filter(isActivityItemActive),
    isLoading,
    error,
    refresh: mutate,
  };
}
