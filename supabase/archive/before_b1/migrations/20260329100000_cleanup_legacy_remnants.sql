-- Clean up three legacy remnants discovered during architecture audit.
--
-- 1. Rename context_publish → context_publishes (code already uses plural)
-- 2. Update openclaw → filesystem in connections provider column
-- 3. Drop orphan syncs table (replaced by connections long ago)

BEGIN;

-- 1. context_publish → context_publishes
ALTER TABLE IF EXISTS "public"."context_publish" RENAME TO "context_publishes";

-- 2. openclaw → filesystem
UPDATE connections SET provider = 'filesystem' WHERE provider = 'openclaw';

-- 3. Drop syncs (orphan from qubits_schema; connections is the active table)
DROP TABLE IF EXISTS "public"."syncs";

COMMIT;
