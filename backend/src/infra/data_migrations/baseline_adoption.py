"""B1 history admission. Mutates tracking rows only after catalog verification."""

from __future__ import annotations

import re
from pathlib import Path

from .schema_history import baseline_version, covered_versions, load_baseline


def adoption_sql(root: Path, *, apply: bool, fingerprint: str | None = None) -> str:
    baseline = load_baseline(root)
    if not baseline:
        raise ValueError("No active baseline")
    fingerprint = fingerprint or baseline.get("schema_fingerprint")
    if not fingerprint or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise ValueError("Baseline schema fingerprint has not been verified")
    version = baseline_version(baseline)
    source_versions = sorted(covered_versions(baseline))
    query = (
        (root / "supabase/baselines/schema_fingerprint.sql").read_text().rstrip().removesuffix(";")
    )
    # All interpolated values are verified hashes, numeric versions or fixed SQL.
    covered = ",".join("'" + v + "'" for v in source_versions)
    checksum = baseline["files"]["baseline.sql"]
    return f"""
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';
SET LOCAL search_path = pg_catalog;
SELECT pg_advisory_xact_lock(hashtextextended('puppyone-baseline-b1', 0));
CREATE TEMP VIEW puppy_baseline_fingerprint AS {query};
DO $adopt$
DECLARE
    versions text[];
    original_history jsonb;
    actual text;
    relation record;
    receipt jsonb;
    has_business_objects boolean;
BEGIN
    SELECT EXISTS (
        SELECT FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','S','f')
        AND NOT EXISTS (SELECT FROM pg_depend d WHERE d.classid='pg_class'::regclass
            AND d.objid=c.oid AND d.deptype='e')
        UNION ALL
        SELECT FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='public'
        AND NOT EXISTS (SELECT FROM pg_depend d WHERE d.classid='pg_proc'::regclass
            AND d.objid=p.oid AND d.deptype='e')
    ) INTO has_business_objects;
    IF to_regclass('supabase_migrations.schema_migrations') IS NULL THEN
        IF has_business_objects THEN
            RAISE EXCEPTION 'BASELINE_HISTORY_MISSING';
        END IF;
        RETURN;
    END IF;
    LOCK TABLE supabase_migrations.schema_migrations IN SHARE ROW EXCLUSIVE MODE;
    SELECT coalesce(array_agg(version::text ORDER BY version),'{{}}') INTO versions
    FROM supabase_migrations.schema_migrations;
    IF '{version}' = ANY(versions) THEN
        IF versions && ARRAY[{covered}]::text[] THEN
            RAISE EXCEPTION 'BASELINE_MIXED_HISTORY';
        END IF;
        IF to_regclass('public.migration_log') IS NULL THEN
            RAISE EXCEPTION 'BASELINE_SCHEMA_MISSING';
        END IF;
        -- At B1 itself, still detect drift on repeat. Later revisions are
        -- checked by ordinary deployment drift validation instead.
        IF NOT EXISTS (SELECT FROM unnest(versions) v WHERE v > '{version}') THEN
            SELECT f.fingerprint INTO actual FROM pg_temp.puppy_baseline_fingerprint f;
            IF actual IS DISTINCT FROM '{fingerprint}' THEN
                RAISE EXCEPTION 'BASELINE_SCHEMA_DRIFT';
            END IF;
        END IF;
        RETURN;
    END IF;
    IF cardinality(versions) = 0 THEN
        IF has_business_objects THEN
            RAISE EXCEPTION 'BASELINE_HISTORY_MISSING';
        END IF;
        RETURN;
    END IF;
    IF versions IS DISTINCT FROM ARRAY[{covered}]::text[] THEN
        RAISE EXCEPTION 'BASELINE_UPGRADE_REQUIRED: complete the archived migration chain first';
    END IF;
    -- Prevent concurrent table DDL during inspection; ordinary customer writes
    -- remain allowed. Deployment jobs themselves are serialized by environment.
    FOR relation IN SELECT c.oid::regclass AS name FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind IN ('r','p') ORDER BY c.oid
    LOOP
        EXECUTE format('LOCK TABLE %s IN ACCESS SHARE MODE', relation.name);
    END LOOP;
    SELECT f.fingerprint INTO actual FROM pg_temp.puppy_baseline_fingerprint f;
    IF actual IS DISTINCT FROM '{fingerprint}' THEN
        RAISE EXCEPTION 'BASELINE_SCHEMA_DRIFT: history remains unchanged';
    END IF;
    SELECT coalesce(jsonb_agg(to_jsonb(h) ORDER BY version),'[]') INTO original_history
    FROM supabase_migrations.schema_migrations h;
    SELECT summary INTO receipt FROM public.migration_log WHERE name='schema_baseline_b1';
    IF receipt IS NOT NULL THEN
        RAISE EXCEPTION 'BASELINE_RECEIPT_CONFLICT';
    END IF;
    IF {str(apply).lower()} THEN
        INSERT INTO public.migration_log(name, applied_at, summary)
        VALUES ('schema_baseline_b1', now(), jsonb_build_object(
            'baseline_sha256','{checksum}', 'schema_fingerprint','{fingerprint}',
            'original_history',original_history, 'verified',true));
        DELETE FROM supabase_migrations.schema_migrations
        WHERE version = ANY(ARRAY[{covered}]::text[]);
        INSERT INTO supabase_migrations.schema_migrations(version,name,statements)
        VALUES ('{version}','baseline_b1',ARRAY['-- verified history adoption; no baseline DDL executed']);
    END IF;
END;
$adopt$;
{"COMMIT" if apply else "ROLLBACK"};
"""
