-- Durable one-time import jobs.
--
-- These rows are the source of truth for user-triggered imports such as
-- "Import from GitHub". They are deliberately separate from connectors:
-- an import job is a one-shot task, not a persistent sync/access binding.

CREATE TABLE IF NOT EXISTS public.import_jobs (
    id text DEFAULT (gen_random_uuid())::text NOT NULL,
    org_id text REFERENCES public.organizations(id) ON DELETE SET NULL,
    project_id text NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,
    created_by uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    provider text NOT NULL,
    source_url text NOT NULL,
    name text,
    target_path text DEFAULT '' NOT NULL,
    config jsonb DEFAULT '{}'::jsonb NOT NULL,
    status text DEFAULT 'queued' NOT NULL,
    phase text DEFAULT 'queued' NOT NULL,
    progress integer DEFAULT 0 NOT NULL,
    message text,
    result_path text,
    result_commit_id text,
    error_message text,
    worker_job_id text,
    started_at timestamptz,
    completed_at timestamptz,
    created_at timestamptz DEFAULT now() NOT NULL,
    updated_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT import_jobs_pkey PRIMARY KEY (id),
    CONSTRAINT import_jobs_status_check CHECK (
        status = ANY (ARRAY['queued','running','completed','failed','cancelled'])
    ),
    CONSTRAINT import_jobs_progress_check CHECK (progress >= 0 AND progress <= 100),
    CONSTRAINT import_jobs_provider_check CHECK (provider <> ''),
    CONSTRAINT import_jobs_source_url_check CHECK (source_url <> '')
);

CREATE INDEX IF NOT EXISTS idx_import_jobs_project_created
    ON public.import_jobs (project_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_import_jobs_project_active
    ON public.import_jobs (project_id, status, created_at DESC)
    WHERE status IN ('queued', 'running');

CREATE INDEX IF NOT EXISTS idx_import_jobs_created_by
    ON public.import_jobs (created_by, created_at DESC);

CREATE OR REPLACE FUNCTION public._import_jobs_bump_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_import_jobs_updated_at ON public.import_jobs;
CREATE TRIGGER trg_import_jobs_updated_at
    BEFORE UPDATE ON public.import_jobs
    FOR EACH ROW
    EXECUTE FUNCTION public._import_jobs_bump_updated_at();

ALTER TABLE public.import_jobs ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'import_jobs'
          AND policyname = 'import_jobs_service_role_all'
    ) THEN
        CREATE POLICY "import_jobs_service_role_all"
            ON public.import_jobs
            FOR ALL
            TO service_role
            USING (true)
            WITH CHECK (true);
    END IF;
END $$;
