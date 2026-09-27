-- Durable session state for scope-keyed Access sandboxes (V2 "sandbox as
-- access point"; see docs/proposals/PUP-sandbox-access-point.md).
--
-- One row per scope: the ScopeSandboxManager's source of truth so sessions
-- survive process restarts and coordinate across workers, replacing the
-- in-memory store. Time columns are epoch seconds (double precision) to match
-- the manager's float clock.
--
-- NOTE: this row IS the per-scope record; the manager serializes per scope with
-- an in-process lock. Cross-instance same-scope races (two API processes
-- acquiring the same scope at once) are a known follow-up — to be hardened with
-- a row lock / optimistic version before multi-writer production.

CREATE TABLE IF NOT EXISTS public.scope_sandbox_sessions (
    scope_id text NOT NULL,
    project_id text NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,
    provider text NOT NULL,
    sandbox_id text NOT NULL,
    state text NOT NULL,
    connected_users jsonb DEFAULT '[]'::jsonb NOT NULL,
    activity_events jsonb DEFAULT '[]'::jsonb NOT NULL,
    recent_user_events jsonb DEFAULT '{}'::jsonb NOT NULL,
    connection jsonb,
    last_full_pull_seconds double precision DEFAULT 0 NOT NULL,
    repo_size_bytes bigint DEFAULT 0 NOT NULL,
    created_at double precision NOT NULL,
    last_active_at double precision NOT NULL,
    last_state_change_at double precision NOT NULL,
    updated_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT scope_sandbox_sessions_pkey PRIMARY KEY (scope_id),
    CONSTRAINT scope_sandbox_sessions_state_check CHECK (
        state = ANY (ARRAY['pending','running','stopped','destroyed','unknown'])
    ),
    CONSTRAINT scope_sandbox_sessions_provider_check CHECK (provider <> ''),
    CONSTRAINT scope_sandbox_sessions_sandbox_id_check CHECK (sandbox_id <> '')
);

-- Reaper sweeps + per-project lookups.
CREATE INDEX IF NOT EXISTS idx_scope_sandbox_sessions_state_active
    ON public.scope_sandbox_sessions (state, last_active_at);
CREATE INDEX IF NOT EXISTS idx_scope_sandbox_sessions_project
    ON public.scope_sandbox_sessions (project_id);

CREATE OR REPLACE FUNCTION public._scope_sandbox_sessions_bump_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_scope_sandbox_sessions_updated_at ON public.scope_sandbox_sessions;
CREATE TRIGGER trg_scope_sandbox_sessions_updated_at
    BEFORE UPDATE ON public.scope_sandbox_sessions
    FOR EACH ROW EXECUTE FUNCTION public._scope_sandbox_sessions_bump_updated_at();
