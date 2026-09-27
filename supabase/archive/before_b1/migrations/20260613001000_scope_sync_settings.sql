-- Per-scope sync settings (PUP-sync-trigger-architecture, M5).
--
-- Users don't configure individual triggers; they pick a coarse intent
-- (persona + auto-sync on/off) and the server resolves the managed SyncPolicy
-- preset from it. One row per (project, scope).

CREATE TABLE IF NOT EXISTS public.scope_sync_settings (
    project_id text NOT NULL,
    scope_id text NOT NULL,
    persona text NOT NULL DEFAULT 'dev',     -- non_dev | dev | reviewer
    auto_sync boolean NOT NULL DEFAULT true,  -- run the sidecar / checkpoint+publish at all
    updated_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT scope_sync_settings_pkey PRIMARY KEY (project_id, scope_id),
    CONSTRAINT scope_sync_settings_persona_check CHECK (persona = ANY (ARRAY['non_dev','dev','reviewer']))
);
