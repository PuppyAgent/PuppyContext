-- ============================================================================
-- Drop the unused tsvector channel from version_text_index
-- ============================================================================
-- Why
--   PUP-cloud-grep §3 specified a tsvector GIN for word-aware/default match
--   and a pg_trgm GIN for substring/regex. In practice query_indexed_grep
--   selects candidates with LIKE/ILIKE/~/~* (pg_trgm) for ALL cases and
--   recovers word-boundary (-w) semantics in Python; it never issues
--   `tsv @@ to_tsquery(...)`. So the generated ``tsv`` column and its GIN
--   index ``idx_vti_tsv`` were recomputed and maintained on every chunk
--   insert for ZERO read benefit — pure write-throughput + storage waste.
--
--   Dropping them is safe: no code reads ``tsv`` (the query selects
--   file_path/content_hash/chunk_idx/line_start/text only), and the
--   pg_trgm index ``idx_vti_trgm`` still backs every candidate query.
--
-- Idempotency: DROP ... IF EXISTS — safe to re-run.
-- ============================================================================

BEGIN;

DROP INDEX IF EXISTS public.idx_vti_tsv;

ALTER TABLE public.version_text_index
    DROP COLUMN IF EXISTS tsv;

NOTIFY pgrst, 'reload schema';

COMMIT;
