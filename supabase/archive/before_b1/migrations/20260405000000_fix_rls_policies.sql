-- ============================================================
-- Migration: Fix RLS Policies for access_points and org_members
--
-- Problem: After the connections → access_points rename
-- (20260401000000), the service_role RLS policies may be
-- missing or stale on some environments. Additionally,
-- org_members policy may have been lost during schema changes.
--
-- This migration ensures all required service_role policies
-- exist, using idempotent DROP IF EXISTS + CREATE pattern.
-- ============================================================

BEGIN;

-- ── access_points ──────────────────────────────────────────
-- The original policy "service_role_all_syncs" was created on
-- the "connections" table and may or may not have survived the
-- rename to "access_points". Ensure a clean policy exists.
DROP POLICY IF EXISTS "service_role_all_syncs" ON "public"."access_points";
DROP POLICY IF EXISTS "service_role_all_access_points" ON "public"."access_points";
CREATE POLICY "service_role_all_access_points"
    ON "public"."access_points"
    FOR ALL TO service_role
    USING (true) WITH CHECK (true);

-- ── org_members ────────────────────────────────────────────
-- Policy was defined in qubits_schema.sql (line 2386) and
-- prod_alignment.sql (line 621), but may be missing on some
-- environments after schema migrations.
DROP POLICY IF EXISTS "service_role_all_org_members" ON "public"."org_members";
CREATE POLICY "service_role_all_org_members"
    ON "public"."org_members"
    FOR ALL TO service_role
    USING (true) WITH CHECK (true);

-- ── Verify ─────────────────────────────────────────────────
-- After running, confirm with:
--   SELECT tablename, policyname, roles
--   FROM pg_policies
--   WHERE tablename IN ('access_points', 'org_members');

COMMIT;
