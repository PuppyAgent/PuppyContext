-- Release coordinator metadata is not application schema or migration history.
-- This schema is never exposed through PostgREST or included in public drift.
BEGIN;
CREATE SCHEMA IF NOT EXISTS puppyone_release AUTHORIZATION postgres;
REVOKE ALL ON SCHEMA puppyone_release FROM PUBLIC, anon, authenticated, service_role;
CREATE TABLE IF NOT EXISTS puppyone_release.runs (
  source_sha text PRIMARY KEY CHECK (source_sha ~ '^[a-f0-9]{40}$'),
  plan_checksum text NOT NULL,
  state text NOT NULL CHECK (state IN ('running','failed','accepted','superseded')),
  phase text NOT NULL DEFAULT 'admission',
  evidence jsonb NOT NULL DEFAULT '{}',
  started_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE puppyone_release.runs ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON ALL TABLES IN SCHEMA puppyone_release FROM PUBLIC, anon, authenticated, service_role;
COMMIT;
