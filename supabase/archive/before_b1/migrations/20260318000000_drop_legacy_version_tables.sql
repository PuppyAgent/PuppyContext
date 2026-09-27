-- ============================================================
-- Drop legacy version management tables
-- ============================================================
-- The version management system has been fully migrated to the
-- Mut kernel (mut_commits + S3 ObjectStore).
--
-- This completes the cleanup deferred in the Mut migration:
--   "旧表 (file_versions, folder_snapshots) 在迁移过渡期结束后
--    通过单独的 DDL 删除。"
--
-- Dropped:
--   1. file_versions    — old per-node version snapshots (replaced by mut_commits)
--   2. folder_snapshots — old folder snapshots (replaced by Mut Merkle tree)
--   3. next_version()   — old atomic version increment function
--
-- Kept:
--   content_nodes.current_version  — still used by Mut IndexSync
--   content_nodes.content_hash     — still used by Mut IndexSync
-- ============================================================

-- Drop FK first to avoid dependency issues
ALTER TABLE "public"."file_versions"
    DROP CONSTRAINT IF EXISTS "fk_file_versions_snapshot";

DROP TABLE IF EXISTS "public"."file_versions";

DROP TABLE IF EXISTS "public"."folder_snapshots";

DROP FUNCTION IF EXISTS "public"."next_version"("text");
