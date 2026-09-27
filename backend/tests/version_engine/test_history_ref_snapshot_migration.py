from pathlib import Path


MIGRATION = (
    Path(__file__).parents[3]
    / "supabase/archive/before_b1/migrations/20260712000000_project_history_ref_snapshot.sql"
)


def test_history_ref_snapshot_rpc_resolves_canonical_head_before_legacy_global_head():
    sql = MIGRATION.read_text()

    assert "get_version_project_history_refs" in sql
    assert "s.scope_hash = p.root_hash" in sql
    assert "v.project_root_hash = p.root_hash" in sql
    assert sql.index("s.scope_hash = p.root_hash") < sql.index("FROM public.version_commits AS c")
    assert "r.scope_path = ''" in sql
    assert "REVOKE ALL ON FUNCTION" in sql
    assert "TO service_role" in sql
    assert "SET search_path = ''" in sql
    assert "NOTIFY pgrst, 'reload schema'" in sql
