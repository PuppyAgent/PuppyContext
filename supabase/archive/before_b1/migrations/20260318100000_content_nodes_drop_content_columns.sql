-- ============================================================
-- Content-only-in-S3 migration
-- ============================================================
-- PuppyOne follows the GitHub model: content lives in MUT
-- (S3 ObjectStore), PostgreSQL is a metadata index only.
--
-- content_nodes.preview_json and content_nodes.preview_md
-- previously stored full file content as a read cache.
-- Content is now served via GET /nodes/{id}/content which
-- reads directly from S3 MUT ObjectStore using content_hash.
--
-- This migration:
--   1. Clears content columns (data is still in S3 objects)
--   2. Drops the columns to reclaim storage
--
-- Recovery: run IndexSync.rebuild_from_tree() to repopulate
-- if needed (not recommended — read from S3 instead).
-- ============================================================

-- Phase 1: Clear content data (reversible via rebuild)
UPDATE "public"."content_nodes"
SET preview_json = NULL,
    preview_md = NULL
WHERE preview_json IS NOT NULL
   OR preview_md IS NOT NULL;

-- Phase 2: Drop content columns
ALTER TABLE "public"."content_nodes" DROP COLUMN IF EXISTS "preview_json";
ALTER TABLE "public"."content_nodes" DROP COLUMN IF EXISTS "preview_md";
