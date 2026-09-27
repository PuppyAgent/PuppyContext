-- Add active-run leases so crashed Integration workers cannot hold a sync lane forever.
--
-- `queued` and `running` rows are lane holders. Workers refresh heartbeat_at and
-- lease_expires_at while executing; stale rows are marked failed by the scheduler
-- reaper or by the next queue attempt before a replacement run is created.

ALTER TABLE public.sync_runs
    ADD COLUMN IF NOT EXISTS heartbeat_at timestamptz,
    ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz;

CREATE INDEX IF NOT EXISTS idx_sync_runs_active_lease
    ON public.sync_runs(status, lease_expires_at, created_at)
    WHERE status IN ('queued', 'running');

UPDATE public.sync_runs
SET
    heartbeat_at = COALESCE(heartbeat_at, started_at, updated_at, created_at),
    lease_expires_at = COALESCE(
        lease_expires_at,
        now() + interval '30 minutes'
    )
WHERE status IN ('queued', 'running');
