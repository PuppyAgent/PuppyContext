-- ============================================================================
-- repo_user_permissions — per-user-per-repo access control (team plans)
-- ============================================================================
-- Why
--   Today org_members is binary: in or out. Team plans need to say
--   "Alice can read repo A, write repo B, can't see repo C". This table is
--   the override layer; org_members remains the default.
--
--   See docs/design/access-point-redesign-2026-05-02.md (section 5.4).
--
-- Resolution rule (implemented in platform/project/access.py):
--   1. If repo_user_permissions row exists for (project_id, user_id):
--        role='denied' → deny
--        else allow with that role
--   2. Else fall back to org_members (any org member gets effective 'editor')
--   3. Else deny
--
--   This makes the table opt-in. Solo / personal-org projects unaffected
--   (no rows here = pure org_members behavior).
-- ============================================================================

BEGIN;

-- ── Table ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.repo_user_permissions (
    id           TEXT        PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
    project_id   TEXT        NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,
    user_id      UUID        NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,

    -- 'admin'   — full project control + can manage permissions
    -- 'editor'  — read + write through cli/agent/connectors
    -- 'reader'  — read only
    -- 'denied'  — explicitly blocked. Overrides any role granted by org membership.
    role         TEXT        NOT NULL
        CHECK (role IN ('admin', 'editor', 'reader', 'denied')),

    -- Optional fine-grained scope filter. NULL = "all scopes in project".
    -- Non-empty array of repo_scopes.id values restricts the user to those
    -- scopes only. Ignored when role='denied'.
    --
    -- We deliberately store as JSONB array (not a many-to-many table) because
    -- the typical case is "all scopes" (NULL) or "one or two scopes"; an
    -- explosion into a join table buys us nothing for this cardinality.
    allowed_scope_ids JSONB,

    granted_by   UUID        REFERENCES auth.users(id) ON DELETE SET NULL,
    granted_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (project_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_repo_user_perm_user
    ON public.repo_user_permissions (user_id);

CREATE INDEX IF NOT EXISTS idx_repo_user_perm_project
    ON public.repo_user_permissions (project_id);

-- ── RLS ───────────────────────────────────────────────────────────────────
ALTER TABLE public.repo_user_permissions ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'repo_user_permissions'
          AND policyname = 'repo_user_perm_service_role_all'
    ) THEN
        CREATE POLICY "repo_user_perm_service_role_all"
            ON public.repo_user_permissions
            FOR ALL TO service_role
            USING (true) WITH CHECK (true);
    END IF;
END $$;

COMMIT;
