-- ============================================================
-- Federated grep / search — text index
-- ============================================================
--
-- Backs ``POST /ap-fs/grep-indexed`` and ``POST /ap-fs/search``.
-- Implementation contract is in
-- ``docs/proposals/PUP-cloud-grep.md``.
--
-- Why two GIN indexes instead of one:
--   * ``tsvector`` (``to_tsvector('simple', text)``) — word-aware
--     match used for the default ``grep`` and the literal portion
--     of search. Cheap, ranks-friendly, but tokenises so it can't
--     find substrings inside words.
--   * ``pg_trgm`` (``text gin_trgm_ops``) — three-character
--     n-grams that support arbitrary substring AND regex match
--     (PG's regex planner pushes trigram predicates down). This is
--     the load-bearing index for ``-E`` / ``-F`` grep at scale.
--
-- Why chunk rows instead of one-row-per-file:
--   * Trigram candidate sets stay bounded — a 5 MB file becomes
--     ~1200 rows of 4 KB rather than one giant row that bloats
--     the GIN index page.
--   * Line numbers are recoverable per chunk via ``line_start``
--     plus a single scan of the chunk text on the matched row.
--
-- Why no ``commit_id``:
--   * Index rows are keyed by ``content_hash`` — the same blob
--     bytes appearing under any commit are indexed exactly once.
--   * Freshness ("is this scope behind HEAD?") is tracked in
--     ``version_text_index_state`` so single-row index updates
--     don't have to rewrite per-row commit pointers.
--

BEGIN;

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ── Main index table ────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.version_text_index (
    id             BIGSERIAL PRIMARY KEY,
    project_id     TEXT  NOT NULL,
    scope_path     TEXT  NOT NULL DEFAULT '',
    file_path      TEXT  NOT NULL,
    content_hash   TEXT  NOT NULL,
    chunk_idx      INT   NOT NULL,
    line_start     INT   NOT NULL,
    text           TEXT  NOT NULL,
    tsv            tsvector GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED,
    indexed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT version_text_index_pk_natural
        UNIQUE (project_id, content_hash, chunk_idx),
    CONSTRAINT version_text_index_chunk_idx_nonneg
        CHECK (chunk_idx >= 0),
    CONSTRAINT version_text_index_line_start_pos
        CHECK (line_start >= 1)
);

-- Scope lookup. Trigram / tsvector hits are further narrowed by this.
CREATE INDEX IF NOT EXISTS idx_vti_project_scope
    ON public.version_text_index (project_id, scope_path);

-- File path lookup — used when re-indexing a specific path or when
-- the search response wants to group hits by file.
CREATE INDEX IF NOT EXISTS idx_vti_project_file_path
    ON public.version_text_index (project_id, file_path);

-- Full-text index (word-aware).
CREATE INDEX IF NOT EXISTS idx_vti_tsv
    ON public.version_text_index USING GIN (tsv);

-- Trigram index (substring + regex).
CREATE INDEX IF NOT EXISTS idx_vti_trgm
    ON public.version_text_index USING GIN (text gin_trgm_ops);

-- ── Freshness watermark per (project, scope) ────────────────
--
-- We tracked freshness at scope granularity, not per file, because
-- the outbox indexer naturally processes a commit's worth of
-- changes atomically. ``indexed_commit_id`` is the commit_id at
-- which this (project, scope) was last fully reconciled; the
-- query endpoint compares it to the project's HEAD to decide
-- whether to return ``indexed`` / ``stale`` / ``missing``.

CREATE TABLE IF NOT EXISTS public.version_text_index_state (
    project_id          TEXT NOT NULL,
    scope_path          TEXT NOT NULL DEFAULT '',
    indexed_commit_id   TEXT NOT NULL,
    indexed_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (project_id, scope_path)
);

CREATE INDEX IF NOT EXISTS idx_vti_state_project
    ON public.version_text_index_state (project_id);

-- ── RLS ─────────────────────────────────────────────────────
--
-- Read access is gated by application code (AP key → scope check
-- in ``access_point_fs``). RLS here is the second layer of defence:
-- everything goes through ``service_role`` because the indexer
-- worker writes under that role and the read API joins index rows
-- with the AP-resolved scope in the application layer.

ALTER TABLE public.version_text_index ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.version_text_index_state ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
        AND tablename = 'version_text_index'
        AND policyname = 'version_text_index_service_role_all'
    ) THEN
        CREATE POLICY version_text_index_service_role_all
            ON public.version_text_index
            FOR ALL
            TO service_role
            USING (true) WITH CHECK (true);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public'
        AND tablename = 'version_text_index_state'
        AND policyname = 'version_text_index_state_service_role_all'
    ) THEN
        CREATE POLICY version_text_index_state_service_role_all
            ON public.version_text_index_state
            FOR ALL
            TO service_role
            USING (true) WITH CHECK (true);
    END IF;
END $$;

GRANT SELECT, INSERT, UPDATE, DELETE ON public.version_text_index TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.version_text_index_state TO service_role;
GRANT USAGE, SELECT ON SEQUENCE public.version_text_index_id_seq TO service_role;

NOTIFY pgrst, 'reload schema';

COMMIT;
