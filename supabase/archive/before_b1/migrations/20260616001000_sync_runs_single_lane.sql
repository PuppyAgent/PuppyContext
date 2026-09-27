-- Ensure each durable integration connection has only one active sync lane.
--
-- `sync_runs.id` is one execution attempt; `sync_runs.connection_id` is the
-- durable binding. Multiple queued/running runs for the same connection cause
-- duplicate provider fetches and stale sync-point updates. Keep one active run
-- per connection and let different connections converge through Version Engine
-- root CAS.

WITH ranked_active_runs AS (
    SELECT
        id,
        row_number() OVER (
            PARTITION BY connection_id
            ORDER BY
                CASE status WHEN 'running' THEN 0 ELSE 1 END,
                created_at DESC,
                id DESC
        ) AS active_rank
    FROM public.sync_runs
    WHERE status IN ('queued', 'running')
)
UPDATE public.sync_runs sr
SET
    status = 'skipped',
    phase = 'skipped',
    progress = 100,
    message = COALESCE(sr.message, 'Superseded by another active sync run'),
    finished_at = COALESCE(sr.finished_at, now()),
    updated_at = now()
FROM ranked_active_runs ranked
WHERE sr.id = ranked.id
  AND ranked.active_rank > 1;

CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_runs_one_active_per_connection
    ON public.sync_runs(connection_id)
    WHERE status IN ('queued', 'running');
