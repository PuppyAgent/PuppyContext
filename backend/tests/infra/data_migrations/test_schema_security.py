from __future__ import annotations

from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[4]


def test_version_object_locations_is_confined_behind_rls() -> None:
    migration = (
        REPOSITORY
        / "supabase"
        / "archive"
        / "before_b1"
        / "migrations"
        / "20260713000000_enable_version_object_locations_rls.sql"
    ).read_text(encoding="utf-8")

    assert "ALTER TABLE public.version_object_locations ENABLE ROW LEVEL SECURITY;" in migration
    assert "FROM PUBLIC, anon, authenticated;" in migration
    assert "ON public.version_object_locations TO service_role;" in migration
