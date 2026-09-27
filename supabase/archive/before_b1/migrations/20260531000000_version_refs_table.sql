-- ============================================================================
-- version_refs — per-scope Git branch/tag ref store (GAP-3, multi-branch)
-- ============================================================================
-- Why
--   PuppyOne Git remotes historically accepted pushes only to
--   refs/heads/main and hard-rejected every feature branch / tag, because
--   there was nowhere to persist a named ref other than the scope head
--   (mut_scope_state). Silently publishing a feature-branch push to main
--   would be worse than rejecting it, so the transport stayed honest.
--
--   This table is the storage primitive that lets a scope hold additional
--   named refs WITHOUT touching the landed scope head. A ref is just a
--   named pointer to an already-promoted commit object; the scope's
--   authoritative head still lives in mut_scope_state and is only advanced
--   by a landing/merge, never by a branch/tag push.
--
--   Design: docs/proposals/PUP-multi-branch-design.md (Phase 1).
--
-- What it does
--   Creates version_refs (one row per (project, scope_path, ref_name)),
--   keyed by canonical scope_path to match mut_scope_state /
--   version_text_index_state, plus integrity checks (canonical scope_path,
--   40-hex commit id, ref_name shape, ref_type), an updated_at trigger,
--   and service_role RLS.
--
-- Idempotency
--   CREATE TABLE/INDEX IF NOT EXISTS + DO-guarded policy. Re-runnable.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.version_refs (
    id          TEXT        PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
    project_id  TEXT        NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,

    -- Canonical scope path this ref belongs to. '' = root scope. Same
    -- canonicalization as mut_scope_state.scope_path.
    scope_path  TEXT        NOT NULL DEFAULT '',

    -- Full Git ref name, e.g. 'refs/heads/feature-x' or 'refs/tags/v1.2.3'.
    ref_name    TEXT        NOT NULL,

    -- 'branch' (refs/heads/*) or 'tag' (refs/tags/*).
    ref_type    TEXT        NOT NULL CHECK (ref_type IN ('branch', 'tag')),

    -- 40-hex SHA-1 of the commit (or, for an annotated tag, the tag object)
    -- this ref points at. The object must already be promoted to the
    -- project's object store.
    commit_id   TEXT        NOT NULL,

    created_by  TEXT        NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- One row per (project, scope, ref).
    UNIQUE (project_id, scope_path, ref_name)
);

CREATE INDEX IF NOT EXISTS idx_version_refs_project_scope
    ON public.version_refs (project_id, scope_path);

CREATE INDEX IF NOT EXISTS idx_version_refs_commit
    ON public.version_refs (project_id, commit_id);

-- ── Integrity checks ───────────────────────────────────────────────────────

-- Canonical scope_path (matches repo_scopes_path_canonical).
ALTER TABLE public.version_refs
    DROP CONSTRAINT IF EXISTS version_refs_scope_path_canonical;
ALTER TABLE public.version_refs
    ADD CONSTRAINT version_refs_scope_path_canonical CHECK (
        scope_path = '' OR (
            scope_path NOT LIKE '/%' AND
            scope_path NOT LIKE '%/' AND
            scope_path NOT LIKE '%//%'
        )
    );

-- ref_name must be a heads/ or tags/ ref and never the scope head 'main'
-- (the landed head is mut_scope_state's job, not a version_refs row).
ALTER TABLE public.version_refs
    DROP CONSTRAINT IF EXISTS version_refs_ref_name_shape;
ALTER TABLE public.version_refs
    ADD CONSTRAINT version_refs_ref_name_shape CHECK (
        (ref_name LIKE 'refs/heads/%' OR ref_name LIKE 'refs/tags/%')
        AND ref_name <> 'refs/heads/main'
    );

-- 40-hex lowercase commit id.
ALTER TABLE public.version_refs
    DROP CONSTRAINT IF EXISTS version_refs_commit_id_hex;
ALTER TABLE public.version_refs
    ADD CONSTRAINT version_refs_commit_id_hex CHECK (
        commit_id ~ '^[0-9a-f]{40}$'
    );

-- ── updated_at auto-bump ────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public._version_refs_bump_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_version_refs_updated_at ON public.version_refs;
CREATE TRIGGER trg_version_refs_updated_at
    BEFORE UPDATE ON public.version_refs
    FOR EACH ROW
    EXECUTE FUNCTION public._version_refs_bump_updated_at();

-- ── RLS ─────────────────────────────────────────────────────────────────────
ALTER TABLE public.version_refs ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'version_refs'
          AND policyname = 'version_refs_service_role_all'
    ) THEN
        CREATE POLICY "version_refs_service_role_all"
            ON public.version_refs
            FOR ALL TO service_role
            USING (true) WITH CHECK (true);
    END IF;
END $$;

GRANT SELECT, INSERT, UPDATE, DELETE ON public.version_refs TO service_role;

NOTIFY pgrst, 'reload schema';

COMMIT;
