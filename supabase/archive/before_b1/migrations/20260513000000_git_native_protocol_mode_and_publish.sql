-- ============================================================================
-- Git-native protocol mode + atomic version publish scaffolding
-- ============================================================================
-- New projects default to Git at the adapter boundary. Existing projects are
-- backfilled to "both" so legacy MUT clients remain admitted until explicitly
-- switched. The version engine and storage model are shared regardless.
-- ============================================================================

BEGIN;

ALTER TABLE public.projects
    ADD COLUMN IF NOT EXISTS protocol_mode TEXT;

UPDATE public.projects
   SET protocol_mode = 'both'
 WHERE protocol_mode IS NULL;

ALTER TABLE public.projects
    ALTER COLUMN protocol_mode SET DEFAULT 'git',
    ALTER COLUMN protocol_mode SET NOT NULL;

ALTER TABLE public.projects
    DROP CONSTRAINT IF EXISTS projects_protocol_mode_valid;

ALTER TABLE public.projects
    ADD CONSTRAINT projects_protocol_mode_valid
        CHECK (protocol_mode IN ('git', 'mut', 'both'));

-- Persistent mapping from canonical scope commits to Git-visible project
-- history commits. This is the history-graft index: child scope commits can
-- appear in the parent/project Git history without making scope heads share
-- one global write lock.
CREATE TABLE IF NOT EXISTS public.mut_version_index (
    id                      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id              TEXT NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,
    scope_path              TEXT NOT NULL DEFAULT '',
    source_commit_id        TEXT NOT NULL,
    source_scope_hash       TEXT NOT NULL DEFAULT '',
    project_root_hash       TEXT NOT NULL DEFAULT '',
    project_view_commit_id  TEXT NOT NULL DEFAULT '',
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (project_id, source_commit_id)
);

CREATE INDEX IF NOT EXISTS idx_mut_version_index_project_created
    ON public.mut_version_index (project_id, created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_mut_version_index_project_view_commit
    ON public.mut_version_index (project_id, project_view_commit_id);

ALTER TABLE public.mut_version_index ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'mut_version_index'
          AND policyname = 'mut_version_index_service_role_all'
    ) THEN
        CREATE POLICY "mut_version_index_service_role_all"
            ON public.mut_version_index
            FOR ALL TO service_role
            USING (true) WITH CHECK (true);
    END IF;
END $$;

-- Durable outbox for projection/notification repair. The application still
-- attempts synchronous post-commit hooks for read-your-write latency, but this
-- row is inserted in the same transaction as the accepted write.
CREATE TABLE IF NOT EXISTS public.mut_version_outbox (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,
    commit_id       TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    payload         JSONB NOT NULL DEFAULT '{}'::JSONB,
    attempts        INT NOT NULL DEFAULT 0,
    locked_at       TIMESTAMPTZ,
    last_error      TEXT,
    processed_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE public.mut_version_outbox
    ADD COLUMN IF NOT EXISTS attempts INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS locked_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_error TEXT;

CREATE INDEX IF NOT EXISTS idx_mut_version_outbox_unprocessed
    ON public.mut_version_outbox (created_at ASC, id ASC)
    WHERE processed_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_mut_version_outbox_claimable
    ON public.mut_version_outbox (locked_at ASC, created_at ASC, id ASC)
    WHERE processed_at IS NULL;

ALTER TABLE public.mut_version_outbox ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'mut_version_outbox'
          AND policyname = 'mut_version_outbox_service_role_all'
    ) THEN
        CREATE POLICY "mut_version_outbox_service_role_all"
            ON public.mut_version_outbox
            FOR ALL TO service_role
            USING (true) WITH CHECK (true);
    END IF;
END $$;

DROP FUNCTION IF EXISTS public.publish_mut_scope_update(
    TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, TEXT, JSONB, JSONB, TEXT, TEXT, JSONB
);
DROP FUNCTION IF EXISTS public.claim_mut_version_outbox_batch(INT);
DROP FUNCTION IF EXISTS public.complete_mut_version_outbox(BIGINT);
DROP FUNCTION IF EXISTS public.fail_mut_version_outbox(BIGINT, TEXT);

CREATE OR REPLACE FUNCTION public.publish_mut_scope_update(
    p_project_id      TEXT,
    p_scope_path      TEXT,
    p_old_hash        TEXT,
    p_new_hash        TEXT,
    p_head_commit_id  TEXT,
    p_who             TEXT,
    p_message         TEXT,
    p_event_type      TEXT,
    p_changes         JSONB,
    p_conflicts       JSONB,
    p_created_at      TEXT,
    p_audit_agent_id  TEXT,
    p_audit_detail    JSONB
) RETURNS BOOLEAN
LANGUAGE plpgsql
AS $$
DECLARE
    rows_affected INT;
    v_created_at TIMESTAMPTZ;
BEGIN
    v_created_at := COALESCE(NULLIF(p_created_at, '')::TIMESTAMPTZ, NOW());

    IF p_old_hash = '' OR p_old_hash IS NULL THEN
        INSERT INTO public.mut_scope_state
            (project_id, scope_path, scope_hash, head_commit_id)
        VALUES
            (p_project_id, p_scope_path, p_new_hash, p_head_commit_id)
        ON CONFLICT (project_id, scope_path) DO NOTHING;

        GET DIAGNOSTICS rows_affected = ROW_COUNT;
        IF rows_affected = 0 THEN
            UPDATE public.mut_scope_state
               SET scope_hash = p_new_hash,
                   head_commit_id = p_head_commit_id,
                   updated_at = NOW()
             WHERE project_id = p_project_id
               AND scope_path = p_scope_path
               AND (scope_hash = p_old_hash OR (scope_hash IS NULL AND p_old_hash = ''));
            GET DIAGNOSTICS rows_affected = ROW_COUNT;
        END IF;
    ELSE
        UPDATE public.mut_scope_state
           SET scope_hash = p_new_hash,
               head_commit_id = p_head_commit_id,
               updated_at = NOW()
         WHERE project_id = p_project_id
           AND scope_path = p_scope_path
           AND scope_hash = p_old_hash;
        GET DIAGNOSTICS rows_affected = ROW_COUNT;
    END IF;

    IF rows_affected = 0 THEN
        RETURN FALSE;
    END IF;

    INSERT INTO public.mut_commits
        (project_id, commit_id, root_hash, scope_path, scope_hash, who, message, changes, conflicts, created_at)
    VALUES
        (
            p_project_id,
            p_head_commit_id,
            '',
            p_scope_path,
            p_new_hash,
            p_who,
            COALESCE(p_message, ''),
            COALESCE(p_changes, '[]'::JSONB),
            p_conflicts,
            v_created_at
        );

    INSERT INTO public.audit_logs
        (action, operator_type, operator_id, project_id, metadata)
    VALUES
        (
            p_event_type,
            CASE
                WHEN p_audit_agent_id LIKE 'agent:%' THEN 'agent'
                WHEN p_audit_agent_id LIKE 'sync:%' THEN 'sync'
                WHEN p_audit_agent_id LIKE 'user:%' THEN 'user'
                ELSE 'system'
            END,
            p_audit_agent_id,
            p_project_id,
            p_audit_detail
        );

    INSERT INTO public.mut_version_outbox
        (project_id, commit_id, event_type, payload)
    VALUES
        (
            p_project_id,
            p_head_commit_id,
            'version_committed',
            jsonb_build_object(
                'scope_path', p_scope_path,
                'scope_hash', p_new_hash,
                'event_type', p_event_type
            )
        );

    RETURN TRUE;
END;
$$;

CREATE OR REPLACE FUNCTION public.claim_mut_version_outbox_batch(
    p_limit INT DEFAULT 50
) RETURNS TABLE (
    id BIGINT,
    project_id TEXT,
    commit_id TEXT,
    event_type TEXT,
    payload JSONB,
    attempts INT
)
LANGUAGE plpgsql
AS $$
BEGIN
    RETURN QUERY
    WITH picked AS (
        SELECT o.id
          FROM public.mut_version_outbox o
         WHERE o.processed_at IS NULL
           AND (o.locked_at IS NULL OR o.locked_at < NOW() - INTERVAL '5 minutes')
           AND o.created_at < NOW() - INTERVAL '15 seconds'
           AND o.attempts < 25
         ORDER BY o.created_at ASC, o.id ASC
         LIMIT GREATEST(1, LEAST(COALESCE(p_limit, 50), 500))
         FOR UPDATE SKIP LOCKED
    )
    UPDATE public.mut_version_outbox o
       SET locked_at = NOW(),
           attempts = o.attempts + 1,
           last_error = NULL
      FROM picked
     WHERE o.id = picked.id
    RETURNING o.id, o.project_id, o.commit_id, o.event_type, o.payload, o.attempts;
END;
$$;

CREATE OR REPLACE FUNCTION public.complete_mut_version_outbox(
    p_id BIGINT
) RETURNS BOOLEAN
LANGUAGE plpgsql
AS $$
DECLARE
    rows_affected INT;
BEGIN
    UPDATE public.mut_version_outbox
       SET processed_at = NOW(),
           locked_at = NULL,
           last_error = NULL
     WHERE id = p_id
       AND processed_at IS NULL;
    GET DIAGNOSTICS rows_affected = ROW_COUNT;
    RETURN rows_affected > 0;
END;
$$;

CREATE OR REPLACE FUNCTION public.fail_mut_version_outbox(
    p_id BIGINT,
    p_error TEXT
) RETURNS BOOLEAN
LANGUAGE plpgsql
AS $$
DECLARE
    rows_affected INT;
BEGIN
    UPDATE public.mut_version_outbox
       SET locked_at = NULL,
           last_error = LEFT(COALESCE(p_error, ''), 2000)
     WHERE id = p_id
       AND processed_at IS NULL;
    GET DIAGNOSTICS rows_affected = ROW_COUNT;
    RETURN rows_affected > 0;
END;
$$;

COMMIT;
