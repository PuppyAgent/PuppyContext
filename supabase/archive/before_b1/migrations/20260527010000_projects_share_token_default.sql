-- ============================================================================
-- projects.share_token — restore default for project creation
-- ============================================================================
-- Some environments have ``projects.share_token`` as a NOT NULL legacy column,
-- but the repo creation path no longer wrote it after the Access redesign. New
-- project creation then failed with:
--   null value in column "share_token" violates not-null constraint
--
-- Keep the column alive for compatibility, backfill any missing values, and set
-- a database default so direct inserts are safe even outside the Python service.
-- ============================================================================

BEGIN;

ALTER TABLE public.projects
    ADD COLUMN IF NOT EXISTS share_token TEXT;

UPDATE public.projects
   SET share_token = 'prj_' || replace(gen_random_uuid()::TEXT, '-', '')
 WHERE share_token IS NULL OR length(share_token) = 0;

ALTER TABLE public.projects
    ALTER COLUMN share_token SET DEFAULT ('prj_' || replace(gen_random_uuid()::TEXT, '-', '')),
    ALTER COLUMN share_token SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_share_token_unique
    ON public.projects (share_token);

COMMIT;
