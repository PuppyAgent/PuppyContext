-- Scope-sync upstream event channel (PUP-sync-trigger-architecture, M3/M4).
--
-- When a version is published (or a parent/child projection advances), the
-- server appends a path-scoped "upstream advanced" event PER affected scope.
-- Connected sidecars poll events since a cursor and decide — per the managed
-- policy — to auto-integrate (paths disjoint from their dirty set) or hold +
-- notify (overlap). The bigserial id IS the cursor.

CREATE TABLE IF NOT EXISTS public.scope_sync_events (
    id bigserial PRIMARY KEY,
    project_id text NOT NULL,
    scope_id text NOT NULL,                 -- the scope whose subscribers see this
    head_version text NOT NULL,             -- new SoT commit/version id
    affected_paths jsonb DEFAULT '[]'::jsonb NOT NULL,  -- in THIS scope's coordinates
    source text NOT NULL DEFAULT 'publish', -- publish | scope-sync
    origin_user text,                       -- who caused it (so we can skip notifying them)
    created_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT scope_sync_events_source_check CHECK (source = ANY (ARRAY['publish','scope-sync']))
);

-- Sidecars poll "events for (project, scope) with id > cursor".
CREATE INDEX IF NOT EXISTS idx_scope_sync_events_scope_cursor
    ON public.scope_sync_events (project_id, scope_id, id);

-- Retention: events are transient (sidecars consume them quickly); a reaper can
-- trim old rows. Kept simple here — no trigger.
