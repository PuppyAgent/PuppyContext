-- ============================================================================
-- connectors — data-flow channels bound to a scope
-- ============================================================================
-- Why
--   Connectors used to share access_points with the repo's filesystem auth,
--   the agent identity, the MCP endpoint, and the sandbox. This forced one
--   table to do five different jobs. We split connectors into their own
--   table here, with strict scope binding (scope_id NOT NULL).
--
--   See docs/design/access-point-redesign-2026-05-02.md (sections 5.2, 5.3).
--
-- Built-in connectors (Q1)
--   Every scope ALWAYS has a 'cli' and an 'agent' connector. We enforce this
--   via an AFTER INSERT trigger on repo_scopes that auto-creates both rows.
--   This is cheaper than synthetic rendering (no special-case in every read
--   path), and makes status / pause / customize a uniform operation.
--
-- Idempotency
--   CREATE TABLE IF NOT EXISTS + DO blocks. Trigger function uses CREATE OR
--   REPLACE. Re-running yields the same final state.
-- ============================================================================

BEGIN;

-- ── Table ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.connectors (
    id                    TEXT        PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
    project_id            TEXT        NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,
    scope_id              TEXT        NOT NULL REFERENCES public.repo_scopes(id) ON DELETE CASCADE,

    -- Channel kind:
    --   'cli'    — user's local mut CLI. Auto-created per scope. Bidir.
    --   'agent'  — PuppyOne in-app chat agent. Auto-created per scope. Bidir.
    --   'notion' / 'gmail' / 'google_docs' / 'google_sheets' /
    --   'google_calendar' / 'google_drive' / 'github' / 'linear' /
    --   'airtable' / 'url' / 'rss' / 'rest_api' / 'supabase' / etc.
    provider              TEXT        NOT NULL,

    -- Display name. cli/agent rows are pre-named; user can edit third-party.
    name                  TEXT        NOT NULL,

    -- Direction:
    --   'bidirectional' — only for cli, agent.
    --   'inbound'       — third-party → repo (import).
    --   'outbound'      — repo → third-party (export).
    direction             TEXT        NOT NULL
        CHECK (direction IN ('bidirectional', 'inbound', 'outbound')),

    -- Provider-specific config (notion page_id, gmail label, github repo, ...).
    -- For agent: stores mcp_api_key so the MCP service can find the agent.
    -- For cli/agent (general): {} initially.
    config                JSONB       NOT NULL DEFAULT '{}'::JSONB,

    -- For OAuth-backed third-party: which oauth_connections row provides
    -- credentials. NULL for cli, agent, and self-auth providers (e.g. raw URL
    -- with API key in config).
    --
    -- Type is BIGINT to match oauth_connections.id, which is a BIGINT IDENTITY
    -- column inherited from the original qubits schema (see
    -- 20260306085814_qubits_schema.sql:944). Don't be misled by the TEXT-based
    -- ids elsewhere in this file — oauth_connections is the one big-int holdout.
    oauth_connection_id   BIGINT      REFERENCES public.oauth_connections(id)
                                        ON DELETE SET NULL,

    -- Sync trigger: {"type": "manual" | "scheduled" | "on_change", "config": {...}}.
    trigger               JSONB       NOT NULL DEFAULT '{"type": "manual"}'::JSONB,

    -- Lifecycle.
    status                TEXT        NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'paused', 'syncing', 'error')),
    last_run_at           TIMESTAMPTZ,
    last_run_id           TEXT,
    error_message         TEXT,

    -- Audit.
    created_by            UUID        REFERENCES auth.users(id) ON DELETE SET NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Built-in connectors are at most one per (scope, provider).
-- Third-party providers can have many connectors per scope (e.g. multiple
-- Notion pages bound to the same /docs scope), so the constraint is partial.
CREATE UNIQUE INDEX IF NOT EXISTS idx_connectors_builtin_one_per_scope
    ON public.connectors (scope_id, provider)
    WHERE provider IN ('cli', 'agent');

CREATE INDEX IF NOT EXISTS idx_connectors_project
    ON public.connectors (project_id);

CREATE INDEX IF NOT EXISTS idx_connectors_scope
    ON public.connectors (scope_id);

CREATE INDEX IF NOT EXISTS idx_connectors_oauth
    ON public.connectors (oauth_connection_id)
    WHERE oauth_connection_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_connectors_provider_pid
    ON public.connectors (project_id, provider);

-- ── updated_at auto-bump ──────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public._connectors_bump_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_connectors_updated_at ON public.connectors;
CREATE TRIGGER trg_connectors_updated_at
    BEFORE UPDATE ON public.connectors
    FOR EACH ROW
    EXECUTE FUNCTION public._connectors_bump_updated_at();

-- ── Built-in connector auto-creation trigger (Q1) ─────────────────────────
-- Every new scope gets a 'cli' and an 'agent' connector immediately.
-- Runs in the same transaction as the scope INSERT — no half-state visible.
--
-- Direction is 'bidirectional' for both. config is empty {}; agent's mcp_api_key
-- is filled in when the agent is first activated by the user (a separate
-- service-layer write, not part of this trigger).
CREATE OR REPLACE FUNCTION public.create_builtin_connectors_for_scope()
RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO public.connectors
        (project_id, scope_id, provider, name, direction, config, status)
    VALUES
        (NEW.project_id, NEW.id, 'cli',   'Local CLI', 'bidirectional', '{}'::JSONB, 'active'),
        (NEW.project_id, NEW.id, 'agent', 'AI Agent',  'bidirectional', '{}'::JSONB, 'active');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_create_builtin_connectors ON public.repo_scopes;
CREATE TRIGGER trg_create_builtin_connectors
    AFTER INSERT ON public.repo_scopes
    FOR EACH ROW
    EXECUTE FUNCTION public.create_builtin_connectors_for_scope();

-- ── RLS ───────────────────────────────────────────────────────────────────
ALTER TABLE public.connectors ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'connectors'
          AND policyname = 'connectors_service_role_all'
    ) THEN
        CREATE POLICY "connectors_service_role_all"
            ON public.connectors
            FOR ALL TO service_role
            USING (true) WITH CHECK (true);
    END IF;
END $$;

COMMIT;
