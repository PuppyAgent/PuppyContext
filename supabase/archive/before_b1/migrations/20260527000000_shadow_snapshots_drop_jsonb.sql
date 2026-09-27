-- ============================================================
-- Shadow snapshots — drop JSONB manifest, move to S3
-- ============================================================
--
-- The original ``local_shadow_snapshots`` table stored the manifest
-- (path / mode / blob_hash / size / preview text for every file),
-- the previews map, and the distinct blob_hashes array as JSONB
-- columns. This forced an 8 MiB cap "after JSON encoding" because
-- big manifests pressed against Postgres JSONB performance and the
-- TOAST page-size budget.
--
-- New design: those three blobs are written as ONE JSON object to
-- S3 at ``shadow-snapshots/{project_id}/{snapshot_id}/manifest.json``.
-- The Supabase row keeps only the lightweight identity + statistics
-- columns the listing endpoint actually needs. Implementation:
-- ``backend/src/version_engine/entrypoints/http/shadow_snapshot.py``.
--
-- Data impact: existing manifest / previews / blob_hashes content is
-- DROPPED. Shadow snapshots are user-side mirrors that the local
-- client can re-upload on next sync — they are not source of truth,
-- so a clean break is safer than a half-migrated state where some
-- rows live in S3 and some in JSONB.
--
-- Rollback: re-add the columns and restore from a Supabase backup
-- snapshot (the columns were JSONB with DEFAULT, so re-adding is
-- ``ALTER TABLE ... ADD COLUMN manifest JSONB NOT NULL DEFAULT '[]'``).

BEGIN;

-- Wipe rows so no client thinks it has a valid manifest after the
-- columns disappear. Clients next call to the upsert endpoint
-- repopulates everything from local working-tree state.
TRUNCATE TABLE public.local_shadow_snapshots;

ALTER TABLE public.local_shadow_snapshots
    DROP COLUMN IF EXISTS manifest,
    DROP COLUMN IF EXISTS previews,
    DROP COLUMN IF EXISTS blob_hashes;

NOTIFY pgrst, 'reload schema';

COMMIT;
