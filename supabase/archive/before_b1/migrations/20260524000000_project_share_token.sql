-- ============================================================
-- Project share-link MVP (per the proposal `docs/proposals/...` —
-- locked decision: deterministic per-project token).
--
-- Adds a single ``share_token`` column to ``projects``. Whoever has
-- the token can call ``POST /projects/share/{token}/join`` and
-- become a member (default role: viewer). Rotating the token
-- invalidates every link issued from the previous value — that's
-- the v1 "revoke" mechanism.
--
-- Token format: 32 URL-safe characters. We generate via Python's
-- ``secrets.token_urlsafe(24)`` for new rows; existing rows get a
-- one-off hex backfill in this migration so the NOT NULL constraint
-- can land without breaking deployed data.
-- ============================================================

-- 1. Add the column. Nullable for the duration of the backfill so
--    existing rows survive the ALTER. We tighten to NOT NULL at the
--    end.
ALTER TABLE "public"."projects"
  ADD COLUMN IF NOT EXISTS "share_token" text;

-- 2. Backfill any row that doesn't already have a token. ``gen_random_bytes``
--    is provided by ``pgcrypto`` (installed by Supabase by default).
--    ``encode(..., 'hex')`` gives URL-safe ASCII without ``+/=`` padding
--    we'd otherwise have to strip from base64. 24 bytes → 48 hex chars,
--    plenty of entropy.
UPDATE "public"."projects"
SET "share_token" = encode(gen_random_bytes(24), 'hex')
WHERE "share_token" IS NULL;

-- 3. Now that every row has a value, enforce NOT NULL + UNIQUE.
--    UNIQUE is required because the join endpoint looks up by token
--    alone — two projects with the same token would be ambiguous.
ALTER TABLE "public"."projects"
  ALTER COLUMN "share_token" SET NOT NULL;

ALTER TABLE "public"."projects"
  ADD CONSTRAINT "projects_share_token_unique" UNIQUE ("share_token");

-- 4. We intentionally do NOT set a column DEFAULT. The application
--    generates tokens via ``secrets.token_urlsafe(24)`` to keep the
--    format consistent across rotations and to avoid relying on
--    ``gen_random_bytes`` at insert time (Supabase's PostgREST does
--    accept it via DEFAULT, but the Python tier owning the format
--    is the simpler contract).

-- 5. Index on the lookup column. UNIQUE already creates one, but
--    spelling it out documents the access pattern.
COMMENT ON COLUMN "public"."projects"."share_token" IS
  'Per-project URL-safe token. Holder can join the project as viewer via POST /projects/share/{token}/join. Rotate (regenerate) to revoke outstanding links.';
