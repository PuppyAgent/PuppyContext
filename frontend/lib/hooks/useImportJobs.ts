import useSWR, { useSWRConfig } from 'swr';
import { useEffect, useRef } from 'react';
import { useAuth } from '@/contexts/SupabaseAuthProvider';
import { invalidateTaskCompletion } from '@/features/tasks/invalidation';
import {
  getProjectImportJobs,
  isImportJobTerminal,
  type ImportJob,
} from '@/lib/importApi';

const DEFAULT_IMPORT_JOB_POLL_MS = 3000;
const EMPTY_JOBS: ImportJob[] = [];

export function useProjectImportJobs(projectId?: string | null) {
  const { userId } = useAuth();
  const {
    data,
    error,
    isLoading,
    mutate,
  } = useSWR(
    projectId && userId ? ['import-jobs', userId, projectId] : null,
    () => getProjectImportJobs(projectId!, { limit: 20 }),
    {
      revalidateOnFocus: false,
      revalidateOnReconnect: true,
      refreshInterval: (latest) => {
        const jobs = latest?.jobs ?? [];
        return jobs.some(job => !isImportJobTerminal(job.status))
          ? DEFAULT_IMPORT_JOB_POLL_MS
          : 0;
      },
    },
  );

  const jobs = data?.jobs ?? EMPTY_JOBS;
  useImportCompletion(jobs, userId, projectId);
  const activeJob = jobs.find(job => !isImportJobTerminal(job.status)) ?? null;
  const latestJob = jobs[0] ?? null;

  return {
    jobs,
    activeJob,
    latestJob,
    isLoading,
    error,
    refresh: mutate,
    upsertJob: (job: ImportJob) => mutate(
      (current) => {
        const currentJobs = current?.jobs ?? [];
        const nextJobs = [
          job,
          ...currentJobs.filter(existing => existing.id !== job.id),
        ];
        return { jobs: nextJobs, total: nextJobs.length };
      },
      { revalidate: true },
    ),
  };
}

/** Completion refresh belongs to the query lifetime, not a window event or a
 * Files render effect. Account/project identity also bounds remembered jobs. */
function useImportCompletion(jobs: ImportJob[], userId: string | null, projectId?: string | null) {
  const { mutate } = useSWRConfig();
  const seen = useRef({ identity: '', ids: new Set<string>() });
  useEffect(() => {
    const identity = JSON.stringify([userId, projectId]);
    if (seen.current.identity !== identity) seen.current = { identity, ids: new Set() };
    if (!userId || !projectId) return;
    for (const job of jobs) {
      if (job.project_id !== projectId || !isImportJobTerminal(job.status) || seen.current.ids.has(job.id)) continue;
      seen.current.ids.add(job.id);
      if (job.status === 'completed') void invalidateTaskCompletion(mutate, userId, { projectId, orgId: job.org_id ?? undefined });
    }
  }, [jobs, mutate, projectId, userId]);
}
