-- Internal runtime state is accessed by the backend service role only.
-- Align the legacy staging table with production without deleting any rows.
ALTER TABLE public.scope_sandbox_sessions ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.scope_sandbox_sessions FROM anon, authenticated;
