from __future__ import annotations

from pathlib import Path

MIGRATION = (
    Path(__file__).parents[3]
    / "supabase/archive/before_b1/migrations/20260716020000_project_deletion_storage_and_org_guard.sql"
)
INVENTORY_REPAIR = (
    Path(__file__).parents[3]
    / "supabase/archive/before_b1/migrations/20260718000000_repair_project_storage_inventory_control_plane.sql"
)
INVENTORY_STATUS = (
    Path(__file__).parents[3]
    / "supabase/archive/before_b1/migrations/20260720000000_project_storage_inventory_status_rpc.sql"
)
REPOSITORY_TARGET_TEST = Path(__file__).parents[3] / "scripts/test-repository-target-migration.sh"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _function(sql: str, name: str, next_marker: str) -> str:
    return sql.split(f"CREATE FUNCTION public.{name}", 1)[1].split(next_marker, 1)[0]


def test_deletion_job_captures_all_physical_project_namespaces_before_cascade() -> None:
    sql = _sql()
    trigger = _function(
        sql,
        "_prepare_project_deletion_job_storage",
        "REVOKE ALL ON FUNCTION public._prepare_project_deletion_job_storage",
    )

    assert "BEFORE INSERT ON public.project_deletion_jobs" in sql
    assert "BEGIN;" in sql
    assert "SET LOCAL lock_timeout = '5s';" in sql
    assert "SET LOCAL statement_timeout = '15min';" in sql
    assert sql.rstrip().endswith("COMMIT;")
    assert "CREATE TABLE public.project_storage_principals" in sql
    assert "uploads_remember_storage_principal" in sql
    assert "AFTER INSERT OR UPDATE OF project_id, created_by ON public.uploads" in sql
    assert "FROM public.projects project" in sql
    assert "FROM public.project_storage_principals stored" in sql
    assert "FROM public.uploads upload" in sql
    assert "COALESCE(upload.created_by::text, p_project_id)" in sql
    assert "NEW.storage_principals :=" in trigger
    assert "NEW.object_prefixes :=" in trigger
    assert "NEW.search_namespace_prefixes :=" in trigger
    assert "NEW.sandbox_resources :=" in trigger
    for namespace in (
        "version/",
        "mut/",
        "projects/",
        "shadow-snapshots/",
        "etl_artifacts",
        "processed",
        "raw",
    ):
        assert namespace in sql


def test_deleted_upload_rows_cannot_erase_legacy_object_ownership_history() -> None:
    sql = _sql()

    table = sql.split("CREATE TABLE public.project_storage_principals", 1)[1].split(
        "CREATE FUNCTION public._remember_upload_storage_principal", 1
    )[0]
    trigger = _function(
        sql,
        "_remember_upload_storage_principal",
        "REVOKE ALL ON FUNCTION public._remember_upload_storage_principal",
    )
    assert "PRIMARY KEY (project_id, principal)" in table
    assert "REFERENCES public.projects(id) ON DELETE CASCADE" in table
    assert "INSERT INTO public.project_storage_principals" in trigger
    assert "COALESCE(NEW.created_by::text, NEW.project_id)" in trigger
    assert "ON CONFLICT (project_id, principal) DO NOTHING" in trigger
    assert "DELETE" not in trigger


def test_deletion_job_manifest_is_constrained_to_the_exact_allowlist() -> None:
    sql = _sql()

    assert "ADD COLUMN storage_principals jsonb" in sql
    assert "ALTER COLUMN storage_principals SET NOT NULL" in sql
    assert "project_deletion_jobs_principals_check" in sql
    assert "project_deletion_jobs_prefixes_check" in sql
    assert "object_prefixes = public._project_deletion_object_prefixes(" in sql
    assert "p_storage_principals ? p_requested_by::text" in sql
    assert "^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$" in sql
    assert "project_deletion_jobs_search_prefixes_check" in sql
    assert "project_deletion_jobs_sandbox_resources_check" in sql
    assert "FROM public.scope_sandbox_sessions session" in sql
    assert "FROM public.sandbox_execution_sessions session" in sql


def test_storage_inventory_is_two_pass_fail_closed_and_tracks_orphans() -> None:
    sql = _sql()

    assert "inventory_complete boolean NOT NULL DEFAULT false" in sql
    assert "CREATE TABLE public.project_storage_inventory_batches" in sql
    assert "CREATE TABLE public.project_storage_orphan_prefixes" in sql
    assert "finalize_project_storage_inventory_scan" in sql
    assert "verify_project_storage_inventory" in sql
    assert "verification_digest IS DISTINCT FROM state.inventory_digest" in sql
    assert "orphan_cleanup_required" in sql
    trigger = _function(
        sql,
        "_prepare_project_deletion_job_storage",
        "REVOKE ALL ON FUNCTION public._prepare_project_deletion_job_storage",
    )
    assert "Project storage inventory is incomplete" in trigger
    assert "ERRCODE = '55000'" in trigger


def test_inventory_control_plane_is_repaired_before_the_status_rpc() -> None:
    """A false-positive legacy migration history must not block later releases."""
    repair = INVENTORY_REPAIR.read_text(encoding="utf-8")
    status = INVENTORY_STATUS.read_text(encoding="utf-8")

    assert INVENTORY_REPAIR.name < INVENTORY_STATUS.name
    for relation in (
        "project_storage_principals",
        "project_storage_inventory_state",
        "project_storage_inventory_batches",
        "project_storage_orphan_prefixes",
    ):
        assert f"CREATE TABLE IF NOT EXISTS public.{relation}" in repair
    for function in (
        "record_project_storage_inventory_batch",
        "mark_project_storage_orphan_cleaned",
        "finalize_project_storage_inventory_scan",
        "verify_project_storage_inventory",
        "complete_project_storage_inventory",
    ):
        assert f"CREATE OR REPLACE FUNCTION public.{function}" in repair
    assert "INSERT INTO public.project_storage_inventory_state (singleton)" in repair
    assert "GRANT SELECT ON public.project_storage_inventory_state TO service_role;" in repair
    assert "FROM public.project_storage_inventory_state state" in status


def test_empty_organization_rpc_locks_before_final_owner_and_emptiness_proofs() -> None:
    function = _function(
        _sql(),
        "delete_empty_organization_control_plane",
        "-- Eliminate the old service-role escape hatch.",
    )

    actor_delete_lock = function.index("organization-delete:")
    organization_lock = function.index("FROM public.organizations organization")
    membership_lock = function.index("FROM public.org_members member")
    owner_check = function.index("actor_role IS DISTINCT FROM 'owner'")
    project_check = function.index("FROM public.projects project")
    destructive_delete = function.index("DELETE FROM public.organizations")
    assert actor_delete_lock < organization_lock < membership_lock < owner_check
    assert owner_check < project_check < destructive_delete
    assert function.count("FOR UPDATE") >= 2
    assert "actor_org_count <= 1" in function
    assert "'organization_not_empty'" in function
    assert "'organization_deletion_in_progress'" in function
    assert "FROM public.project_deletion_jobs job" in function
    assert "job.status <> 'completed'" in function


def test_direct_organization_delete_is_removed_from_the_application_role() -> None:
    sql = _sql()

    assert (
        "REVOKE DELETE ON TABLE public.organizations\n"
        "    FROM PUBLIC, anon, authenticated, service_role;"
    ) in sql
    assert (
        "REVOKE DELETE ON TABLE public.projects\n"
        "    FROM PUBLIC, anon, authenticated, service_role;"
    ) in sql
    assert (
        "GRANT EXECUTE ON FUNCTION "
        "public.delete_empty_organization_control_plane(text, uuid)\n"
        "    TO service_role;"
    ) in sql
    assert (
        "REVOKE ALL ON FUNCTION "
        "public.delete_empty_organization_control_plane(text, uuid)\n"
        "    FROM PUBLIC, anon, authenticated;"
    ) in sql


def test_legacy_migration_fixture_never_runs_the_closure_without_its_base_table() -> None:
    script = REPOSITORY_TARGET_TEST.read_text(encoding="utf-8")
    lines = [line.strip() for line in script.splitlines()]

    assert (
        'closure_rel="supabase/migrations/'
        '20260716020000_project_deletion_storage_and_org_guard.sql"'
    ) in script
    assert lines.count("save_closure") == lines.count("save_initialization")
    assert lines.count("restore_closure") == lines.count("restore_initialization")
    assert (
        'inventory_repair_rel="supabase/migrations/'
        '20260718000000_repair_project_storage_inventory_control_plane.sql"'
    ) in script
    assert (
        'inventory_status_rel="supabase/migrations/'
        '20260720000000_project_storage_inventory_status_rpc.sql"'
    ) in script
    assert lines.count("save_inventory_repair") == lines.count("save_closure")
    assert lines.count("restore_inventory_repair") == lines.count("restore_closure")
    assert lines.count("save_inventory_status") == lines.count("save_closure")
    assert lines.count("restore_inventory_status") == lines.count("restore_closure")
    cleanup = script.split("cleanup() {", 1)[1].split("}", 1)[0]
    assert "restore_closure" in cleanup
    assert "restore_inventory_repair" in cleanup
    assert "restore_inventory_status" in cleanup
    assert 'rm -f "$saved_contract"' in cleanup
    assert '"$saved_closure"' in cleanup
    assert '"$saved_inventory_repair"' in cleanup
    assert '"$saved_inventory_status"' in cleanup
