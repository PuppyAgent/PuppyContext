import useSWR, { useSWRConfig } from 'swr';
import { useEffect, useMemo, useRef } from 'react';
import { importJobKeys } from '@/lib/queryKeys';
import { invalidateTaskCompletion } from '@/lib/tasks/invalidation';
import {
  getProjectImportJobs,
  isImportJobTerminal,
  type ImportJob,
  type ImportJobListResponse,
} from '@/lib/importApi';

const DEFAULT_IMPORT_JOB_POLL_MS = 3000;
const EMPTY_JOBS: ImportJob[] = [];
const EMPTY_RESPONSE: ImportJobListResponse = { jobs: EMPTY_JOBS, total: 0 };

// The auth-aware caller supplies the provider's captured account lifetime. It
// outlives a Files view, so ordinary navigation still preserves query errors.
export function useProjectImportJobs(projectId: string | null | undefined, userId: string | null, isAccountActive: () => boolean) {
  const { key, ...commands } = useImportScope(userId, projectId);
  const {
    data,
    error,
    isLoading,
  } = useSWR(
    key,
    () => readImportJobs(projectId!, isAccountActive),
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
    ...commands,
  };
}

async function readImportJobs(projectId: string, isAccountActive: () => boolean) {
  try {
    return await getProjectImportJobs(projectId, { limit: 20 });
  } catch (error) {
    // SWR discards obsolete successful responses after account invalidation, but
    // errors can still overwrite a new lifetime's error state. Let that same
    // discard path handle an obsolete rejection without hiding current errors.
    if (!isAccountActive()) return EMPTY_RESPONSE;
    throw error;
  }
}

function useImportScope(userId: string | null, projectId?: string | null) {
  const { mutate } = useSWRConfig();
  const generation = useMemo(() => ({ userId, projectId }), [userId, projectId]);
  const current = useRef(generation);
  current.current = generation;
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  const key = userId && projectId ? importJobKeys.list(userId, projectId) : null;
  const isActive = () => Boolean(key) && mounted.current && current.current === generation;

  // A bound SWR mutate follows the latest key. Async creation callbacks must
  // retain their original project/account lifetime, including A -> B -> A.
  return {
    key,
    refresh: () => isActive() ? mutate<ImportJobListResponse>(key) : Promise.resolve(undefined),
    upsertJob: (job: ImportJob) => {
      if (!isActive() || job.project_id !== projectId) return Promise.resolve(undefined);
      return mutate<ImportJobListResponse>(key,
        (current) => {
          const currentJobs = current?.jobs ?? [];
          const nextJobs = [
            job,
            ...currentJobs.filter(existing => existing.id !== job.id),
          ];
          return { jobs: nextJobs, total: nextJobs.length };
        },
        { revalidate: true },
      );
    },
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
