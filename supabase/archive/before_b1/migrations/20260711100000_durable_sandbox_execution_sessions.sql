-- ISSUE-018: ephemeral execution must remain controllable across API workers
-- and process restarts. Provider handles are reconstructed from resource_id.
CREATE TABLE IF NOT EXISTS public.sandbox_execution_sessions (
    session_id text PRIMARY KEY,
    provider text NOT NULL CHECK (provider IN ('docker', 'e2b')),
    resource_id text NOT NULL,
    readonly boolean NOT NULL DEFAULT false,
    temp_path text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    last_activity timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sandbox_execution_sessions_provider_activity
    ON public.sandbox_execution_sessions(provider, last_activity);

ALTER TABLE public.sandbox_execution_sessions ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.sandbox_execution_sessions FROM anon, authenticated;
GRANT ALL ON public.sandbox_execution_sessions TO service_role;

COMMENT ON TABLE public.sandbox_execution_sessions IS
    'Internal durable ownership records for request-oriented sandbox providers.';
