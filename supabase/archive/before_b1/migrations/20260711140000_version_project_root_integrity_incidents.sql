-- Durable operator-visible state for projects whose current root tree cannot
-- be read and has no recoverable historical root.  Keep the project and its
-- pointers intact: this table records an incident; it never deletes or
-- rewrites user content.

CREATE TABLE IF NOT EXISTS public.version_project_root_integrity_incidents (
    project_id text PRIMARY KEY REFERENCES public.projects(id) ON DELETE CASCADE,
    root_hash text NOT NULL CHECK (root_hash ~ '^[0-9a-f]{40}$'),
    status text NOT NULL CHECK (status IN ('irrecoverable')),
    reason text NOT NULL,
    first_detected_at timestamptz NOT NULL DEFAULT now(),
    last_detected_at timestamptz NOT NULL DEFAULT now(),
    marked_by text NOT NULL DEFAULT 'system:root-integrity-repair'
);

CREATE INDEX IF NOT EXISTS idx_version_project_root_integrity_incidents_status
    ON public.version_project_root_integrity_incidents(status, last_detected_at DESC);

ALTER TABLE public.version_project_root_integrity_incidents ENABLE ROW LEVEL SECURITY;

CREATE POLICY version_project_root_integrity_incidents_service_role
    ON public.version_project_root_integrity_incidents
    FOR ALL TO service_role USING (true) WITH CHECK (true);

REVOKE ALL ON public.version_project_root_integrity_incidents
    FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON public.version_project_root_integrity_incidents TO service_role;

NOTIFY pgrst, 'reload schema';
