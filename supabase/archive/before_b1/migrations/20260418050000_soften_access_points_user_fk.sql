-- ============================================================================
-- SOFTEN access_points.user_id -> auth.users FK (CASCADE -> SET NULL)
-- ============================================================================
-- Purpose
--   Change the ON DELETE rule on access_points.user_id from CASCADE to SET NULL.
--
-- Why
--   PuppyOne is a multi-tenant collaboration platform. An access_point
--   (agent / MCP endpoint / sync / sandbox) is owned by an organization,
--   not by the individual user who created it. When that user is deleted
--   from auth.users (account closure, tenant offboarding, etc.), the
--   shared resource should survive — only the per-row "creator" pointer
--   should clear. CASCADE was too aggressive: a single user deletion
--   could nuke every agent / MCP / sync the org depends on.
--
-- History
--   The original baseline (qubits_schema.sql) declared this FK as CASCADE.
--   At some point in early production, an operator manually changed it to
--   SET NULL via the SQL Editor. That change never made its way into a
--   migration, leaving the canonical/qubits state out of sync with prod.
--   This migration retroactively codifies the SET NULL decision.
--
-- Effect by environment
--   prod   : drops SET NULL FK and re-adds SET NULL  (effective no-op)
--   qubits : drops CASCADE FK and re-adds SET NULL  (real change)
--   fresh  : drops CASCADE FK (just declared by baseline) and re-adds SET NULL
--
-- Idempotent
--   Re-running yields the same final state. The DROP/ADD pair is wrapped
--   in IF EXISTS guards.
-- ============================================================================

BEGIN;

-- 1. Defensive: NULL out any orphan user_id values before re-adding the FK.
--    (Should be 0 on both qubits and prod; included so this migration cannot
--    fail on a database that has been hand-edited in unexpected ways.)
DO $$
DECLARE n INT;
BEGIN
    UPDATE public.access_points SET user_id = NULL
     WHERE user_id IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM auth.users u WHERE u.id = access_points.user_id);
    GET DIAGNOSTICS n = ROW_COUNT;
    IF n > 0 THEN
        RAISE NOTICE 'Cleared % orphan user_id values in access_points (referenced users no longer exist)', n;
    END IF;
END $$;

-- 2. Drop the existing FK under either name (canonical or pre-rename legacy).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'syncs_user_id_fkey') THEN
        ALTER TABLE public.access_points DROP CONSTRAINT syncs_user_id_fkey;
        RAISE NOTICE 'Dropped existing syncs_user_id_fkey';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'connections_user_id_fkey') THEN
        ALTER TABLE public.access_points DROP CONSTRAINT connections_user_id_fkey;
        RAISE NOTICE 'Dropped legacy connections_user_id_fkey';
    END IF;
END $$;

-- 3. Re-add with SET NULL semantics (canonical name).
ALTER TABLE public.access_points
  ADD CONSTRAINT syncs_user_id_fkey
  FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE SET NULL;

-- 4. Sanity check: confirm the new ON DELETE rule landed correctly.
DO $$
DECLARE delete_action CHAR;
BEGIN
    SELECT confdeltype INTO delete_action
      FROM pg_constraint
     WHERE conname = 'syncs_user_id_fkey';
    -- 'n' = SET NULL, 'c' = CASCADE, 'a' = NO ACTION, 'r' = RESTRICT, 'd' = SET DEFAULT
    IF delete_action != 'n' THEN
        RAISE EXCEPTION 'Post-check failed: syncs_user_id_fkey ON DELETE rule is %, expected n (SET NULL)', delete_action;
    END IF;
    RAISE NOTICE 'Verified syncs_user_id_fkey ON DELETE = SET NULL';
END $$;

COMMIT;
