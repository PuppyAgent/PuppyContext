-- Fix: MAIN database missing access_key and revoked_at columns on access_points
-- These columns exist in the qubits schema (created in qubits_schema.sql line 560)
-- but were lost during migration to the main project.

ALTER TABLE "public"."access_points"
    ADD COLUMN IF NOT EXISTS "access_key" TEXT;

ALTER TABLE "public"."access_points"
    ADD COLUMN IF NOT EXISTS "revoked_at" TIMESTAMPTZ;

CREATE UNIQUE INDEX IF NOT EXISTS "idx_access_points_access_key"
    ON "public"."access_points" ("access_key")
    WHERE "access_key" IS NOT NULL;
