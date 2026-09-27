-- ISSUE-003: hash repo_scopes access keys at rest.
--
-- repo_scopes.access_key is the primary credential for Git / CLI / AP-FS access
-- and was stored + queried in plaintext. This adds a nullable HMAC-hash column so
-- the app can resolve keys by hash instead of trusting the plaintext column.
--
-- Staged, non-breaking rollout:
--   1. Apply this migration (adds access_key_hash, nullable + indexed).
--   2. Run scripts/backfill_scope_access_key_hash.py to populate hashes for
--      existing rows.
--   3. Set SCOPE_ACCESS_KEY_HASH_LOOKUP=true so writes populate the hash and
--      reads resolve by hash (with plaintext fallback for any missed rows).
--   4. In a LATER migration, once all rows are backfilled and callers no longer
--      need the plaintext value, drop the access_key column.

ALTER TABLE public.repo_scopes
    ADD COLUMN IF NOT EXISTS access_key_hash text;

CREATE INDEX IF NOT EXISTS idx_repo_scopes_access_key_hash
    ON public.repo_scopes (access_key_hash)
    WHERE access_key_hash IS NOT NULL;

COMMENT ON COLUMN public.repo_scopes.access_key_hash IS
    'HMAC-SHA256 of access_key (ISSUE-003). Populated by backfill + on mint when '
    'SCOPE_ACCESS_KEY_HASH_LOOKUP is enabled. Replaces plaintext access_key lookups.';
