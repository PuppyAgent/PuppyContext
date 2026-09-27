from __future__ import annotations

from pathlib import Path

MIGRATION = (
    Path(__file__).parents[3]
    / "supabase/archive/before_b1/migrations/20260716010000_project_initialization_control_plane.sql"
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _function(sql: str, name: str, next_name: str) -> str:
    return sql.split(f"CREATE FUNCTION public.{name}", 1)[1].split(
        f"CREATE FUNCTION public.{next_name}", 1
    )[0]


def test_project_replay_is_resolved_before_org_quota_admission():
    function = _function(
        _sql(),
        "create_project_idempotent",
        "get_project_create_operation_replay",
    )

    replay_lookup = function.index("FROM public.project_create_operations")
    replay_return = function.index("'initializing_replayed'")
    org_lock = function.index("FROM public.organizations")
    quota_count = function.index("SELECT count(*) INTO current_count")
    project_insert = function.index("INSERT INTO public.projects")
    admin_insert = function.index("INSERT INTO public.project_members")
    operation_insert = function.index("INSERT INTO public.project_create_operations")

    assert replay_lookup < replay_return < org_lock < quota_count
    assert quota_count < project_insert < admin_insert < operation_insert
    assert "version_root_hash" not in function
    assert "mut_root_hash" not in function
    assert "L5" in function
    assert "p_org_id text" in function
    assert "p_project_id !~ '^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$'" in function
    assert "request_hash" in function
    assert "result_metadata" in function


def test_completed_replay_is_available_without_mutable_workflow_source():
    sql = _sql()
    function = _function(
        sql,
        "get_project_create_operation_replay",
        "complete_project_initialization",
    )

    assert "operation.request_hash IS DISTINCT FROM p_request_hash" in function
    assert "operation.status <> 'ready'" in function
    assert "operation.project_snapshot" in function
    assert "operation.result_metadata" in function
    assert "lifecycle_status = 'ready'" in function
    assert "FROM public.org_members member" in function
    assert "Template" not in function
    assert "ticket" not in function


def test_l5_owns_initial_root_write_and_control_plane_only_verifies_completion():
    sql = _sql()
    create_function = _function(
        sql,
        "create_project_idempotent",
        "get_project_create_operation_replay",
    )
    completion = _function(
        sql,
        "complete_project_initialization",
        "claim_project_initialization_operations",
    )

    assert "'outcome', 'initializing_created'" in create_function
    assert "status IN ('initializing', 'ready', 'deleted', 'dead_lettered')" in sql
    assert "project_row.version_root_hash IS DISTINCT FROM project_row.mut_root_hash" in completion
    assert "operation.publication_mode = 'empty'" in completion
    assert "project_row.version_root_hash <> empty_tree" in completion
    assert "SET status = 'ready'" in completion
    assert "SET version_root_hash" not in completion
    assert "SET mut_root_hash" not in completion
    assert "claim_project_initialization_operations" in sql
    assert "fail_project_initialization_operation" in sql
    assert "dead_letter_project_initialization_operation" in sql


def test_project_publication_is_gated_until_l5_completion():
    sql = _sql()
    create_function = _function(
        sql,
        "create_project_idempotent",
        "get_project_create_operation_replay",
    )
    completion = _function(
        sql,
        "complete_project_initialization",
        "claim_project_initialization_operations",
    )
    credential = _function(
        sql,
        "issue_user_git_http_credential_idempotent",
        "abort_deferred_project_publication",
    )

    assert "ADD COLUMN lifecycle_status text;" in sql
    assert "UPDATE public.projects SET lifecycle_status = 'ready';" in sql
    assert "ALTER COLUMN lifecycle_status DROP DEFAULT" in sql
    assert "ALTER COLUMN lifecycle_status SET NOT NULL" in sql
    assert "p.lifecycle_status = 'ready'" in sql
    assert "p.lifecycle_status = 'ready'" in credential
    assert "p_share_token, 'initializing'" in create_function
    project_ready = completion.index("UPDATE public.projects")
    operation_ready = completion.index("UPDATE public.project_create_operations", project_ready)
    assert project_ready < operation_ready
    assert "SET lifecycle_status = 'ready'" in completion
    assert "JOIN public.projects p" in sql
    assert "AND p.lifecycle_status = 'ready'" in sql


def test_legacy_project_creation_rpc_is_removed_without_a_default_escape_hatch():
    sql = _sql()

    assert (
        "DROP FUNCTION IF EXISTS public.create_project_with_admin(\n"
        "    text, text, text, text, uuid, text\n"
        ");"
    ) in sql
    lifecycle_alter = sql.split("UPDATE public.projects SET lifecycle_status = 'ready';", 1)[1]
    lifecycle_alter = lifecycle_alter.split("CREATE INDEX projects_ready_org_idx", 1)[0]
    assert "DROP DEFAULT" in lifecycle_alter
    assert "SET DEFAULT" not in lifecycle_alter
    assert "SET NOT NULL" in lifecycle_alter


def test_legacy_projects_are_preserved_ready_without_empty_or_age_heuristics():
    sql = _sql()
    backfill = sql.split("ADD COLUMN lifecycle_status text;", 1)[1].split(
        "ALTER TABLE public.projects", 1
    )[0]
    claims = _function(
        sql,
        "claim_project_initialization_operations",
        "renew_project_initialization_claim",
    )

    assert backfill.count("UPDATE public.projects SET lifecycle_status = 'ready';") == 1
    assert "WHERE" not in backfill.upper()
    assert "FROM public.project_create_operations operation" in claims
    assert "version_root_hash IS NULL" not in claims
    assert "created_at <" not in claims
    assert "name =" not in claims


def test_service_role_cannot_bypass_project_creation_or_publication_gate():
    sql = _sql()
    grants = sql.split("-- The service role is an application transport principal", 1)[1].split(
        "COMMIT;", 1
    )[0]

    assert "REVOKE INSERT, UPDATE ON public.projects FROM service_role;" in grants
    assert "GRANT UPDATE (" in grants
    assert "lifecycle_status" not in grants.split(") ON public.projects", 1)[0]
    for allowed_column in (
        "name",
        "description",
        "visibility",
        "bound_git_branch",
        "prompt_template",
        "share_token",
        "mut_root_hash",
        "version_root_hash",
        "updated_at",
    ):
        assert allowed_column in grants


def test_service_role_cannot_forge_lifecycle_or_deletion_journals():
    sql = _sql()

    for table in (
        "project_create_operations",
        "git_credential_issue_operations",
        "project_deletion_jobs",
    ):
        assert (
            f"REVOKE ALL ON public.{table}\n    FROM PUBLIC, anon, authenticated, service_role;"
        ) in sql
        assert f"GRANT ALL ON public.{table}" not in sql
        assert f"{table}_service_role_all" not in sql


def test_credential_operation_persists_only_hashes_and_has_actor_key_uniqueness():
    sql = _sql()
    table = sql.split("CREATE TABLE public.git_credential_issue_operations", 1)[1].split(
        "CREATE TABLE public.project_deletion_jobs", 1
    )[0]
    function = _function(
        sql,
        "issue_user_git_http_credential_idempotent",
        "abort_deferred_project_publication",
    )

    assert "PRIMARY KEY (actor_user_id, operation_key)" in table
    assert "credential_hash text NOT NULL" in table
    assert "credential text" not in table
    assert "p_key_hash" in function
    assert "p_raw" not in function
    assert "p_credential text" not in function
    assert (
        "REVOKE ALL ON FUNCTION public.issue_user_git_http_credential(\n"
        "    text, text, text, text, text, uuid, text, text, text, text, text\n"
        ") FROM service_role;"
    ) in sql


def test_publication_modes_are_explicit_and_deferred_work_has_a_durable_deadline():
    sql = _sql()
    table = sql.split("CREATE TABLE public.project_create_operations", 1)[1].split(
        "CREATE INDEX project_create_operations_project_idx", 1
    )[0]
    create_function = _function(
        sql,
        "create_project_idempotent",
        "get_project_create_operation_replay",
    )

    assert "publication_mode text NOT NULL" in table
    assert "publication_mode IN ('empty', 'deferred')" in table
    assert "p_publication_mode text" in create_function
    assert "p_publication_mode NOT IN ('empty', 'deferred')" in create_function
    assert "existing_operation.publication_mode IS DISTINCT FROM p_publication_mode" in (
        create_function
    )
    assert "WHEN 'deferred' THEN now() + interval '6 hours'" in create_function
    assert "initialization_deadline_at" in table
    assert "ELSE now() + interval '24 hours'" in create_function


def test_terminal_empty_initialization_accepts_pre_root_state_and_has_dead_letter():
    sql = _sql()
    abandon = _function(
        sql,
        "abandon_project_initialization",
        "delete_project_control_plane",
    )
    dead_letter = _function(
        sql,
        "dead_letter_project_initialization_operation",
        "issue_user_git_http_credential_idempotent",
    )

    assert "COALESCE(project_row.version_root_hash, '') = ''" in abandon
    assert "COALESCE(project_row.mut_root_hash, '') = ''" in abandon
    assert "project_row.version_root_hash = empty_tree" in abandon
    assert "project_row.mut_root_hash = empty_tree" in abandon
    assert "create_operation.initialization_claimed_by IS DISTINCT FROM p_worker_id" in abandon
    assert "p_worker_id IS NULL\n               AND NOT EXISTS" in abandon
    assert "status = 'dead_lettered'" in dead_letter
    assert "initialization_claimed_by = p_worker_id" in dead_letter


def test_default_project_name_allocation_is_inside_the_serialized_create_transaction():
    sql = _sql()
    create_function = _function(
        sql,
        "create_project_idempotent",
        "get_project_create_operation_replay",
    )

    assert "CREATE FUNCTION public._untitled_project_slot(p_name text)" in sql
    org_lock = create_function.index(
        "PERFORM 1 FROM public.organizations WHERE id = p_org_id FOR UPDATE"
    )
    name_resolution = create_function.index(
        "requested_name_slot := public._untitled_project_slot(p_name)"
    )
    project_insert = create_function.index("INSERT INTO public.projects")
    assert org_lock < name_resolution < project_insert
    assert "generate_series(1::bigint, current_count + 1)" in create_function
    assert "p_project_id, resolved_project_name" in create_function


def test_deferred_abort_is_service_only_and_always_persists_exact_prefix_cleanup():
    sql = _sql()
    function = _function(
        sql,
        "abort_deferred_project_publication",
        "_project_initialization_has_cascade_dependents",
    )

    mode_guard = function.index("operation.publication_mode <> 'deferred'")
    project_lookup = function.index("FROM public.projects")
    job_insert = function.index("INSERT INTO public.project_deletion_jobs")
    admission_close = function.index("SET lifecycle_status = 'deleting'")
    assert mode_guard < project_lookup < job_insert < admission_close
    assert "DELETE FROM public.projects" not in function
    assert "operation.initialization_claimed_by IS DISTINCT FROM p_worker_id" in function
    assert "project_row.lifecycle_status <> 'initializing'" in function
    assert "project_row.org_id IS DISTINCT FROM operation.org_id" in function
    assert "'publication_abort'" in function
    for prefix in ("'version/'", "'mut/'", "'projects/'"):
        assert prefix in function
    assert (
        "REVOKE ALL ON FUNCTION public.abort_deferred_project_publication(\n"
        "    text, text, uuid, integer, text\n"
        ") FROM PUBLIC, anon, authenticated;"
    ) in sql
    assert (
        "GRANT EXECUTE ON FUNCTION public.abort_deferred_project_publication(\n"
        "    text, text, uuid, integer, text\n"
        ") TO service_role;"
    ) in sql


def test_deletion_tombstone_survives_project_and_has_quiescent_two_phase_cleanup():
    sql = _sql()
    table = sql.split("CREATE TABLE public.project_deletion_jobs", 1)[1].split(
        "CREATE TABLE public.project_write_leases", 1
    )[0]

    assert "REFERENCES public.projects" not in table
    assert "quiescence_seconds integer NOT NULL" in table
    assert "CHECK (quiescence_seconds >= 1800)" in table
    assert "phase IN ('drain', 'purge', 'verify')" in table
    assert "CREATE TABLE public.project_write_leases" in sql
    assert "CREATE FUNCTION public.drain_project_deletion_job" in sql
    drain = _function(
        sql,
        "drain_project_deletion_job",
        "claim_project_deletion_jobs",
    )
    lease_count = drain.index("FROM public.project_write_leases lease")
    project_delete = drain.index("DELETE FROM public.projects")
    assert lease_count < project_delete
    assert "available_at = now() + make_interval" in sql
    assert "schedule_project_deletion_verification" in sql
    assert "AND phase = 'verify'" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql
    for prefix in ("'version/'", "'mut/'", "'projects/'"):
        assert prefix in sql


def test_write_admission_lease_closes_before_deletion_and_drains_before_cascade():
    sql = _sql()
    acquire = _function(
        sql,
        "acquire_project_write_lease",
        "renew_project_write_lease",
    )
    delete = _function(
        sql,
        "delete_project_control_plane",
        "drain_project_deletion_job",
    )
    drain = _function(
        sql,
        "drain_project_deletion_job",
        "claim_project_deletion_jobs",
    )

    assert "lifecycle_status IN ('initializing', 'ready', 'deleting')" in sql
    assert "CREATE TABLE public.project_write_leases" in sql
    assert "FOR UPDATE" in acquire
    assert "operation.status = 'initializing'" in acquire
    assert "initialization_claimed_by = p_initialization_worker" in acquire
    assert "COALESCE(p_ttl_seconds, 120), 7200" in acquire
    assert "SET lifecycle_status = 'deleting'" in delete
    lease_count = drain.index("FROM public.project_write_leases lease")
    relational_delete = drain.index("DELETE FROM public.projects")
    assert lease_count < relational_delete
    assert "active_count > 0" in drain


def test_project_delete_locks_authorization_facts_before_final_role_resolution():
    function = _function(
        _sql(),
        "delete_project_control_plane",
        "claim_project_deletion_jobs",
    )

    project_lock = function.index("FROM public.projects project")
    org_membership_lock = function.index("FROM public.org_members member")
    project_membership_lock = function.index("FROM public.project_members member")
    final_role_resolution = function.index("FROM public.resolve_project_role")
    destructive_job = function.index("INSERT INTO public.project_deletion_jobs")

    assert project_lock < org_membership_lock < project_membership_lock
    assert project_membership_lock < final_role_resolution < destructive_job
    assert function.count("FOR UPDATE") >= 3


def test_abandon_proves_exact_empty_bootstrap_state_before_mutating_any_resource():
    sql = _sql()
    helper = sql.split("CREATE FUNCTION public._project_initialization_has_cascade_dependents", 1)[
        1
    ].split("CREATE FUNCTION public.abandon_project_initialization", 1)[0]
    function = _function(
        sql,
        "abandon_project_initialization",
        "delete_project_control_plane",
    )

    assert "constraint_row.confrelid = 'public.projects'::regclass" in helper
    assert "constraint_row.confdeltype IN ('c', 'n', 'd')" in helper
    assert "pg_catalog.generate_subscripts" in helper
    assert "version_root_hash" in function
    assert "mut_root_hash" in function
    assert "state.scope_path <> ''" in function
    assert "COALESCE(state.scope_hash, '') <> ''" in function
    assert "tx.status = 'committed'" in function
    assert "member.role = 'admin'" in function
    assert "member.granted_by = p_actor_user_id" in function
    assert "surface.scope_id IS NOT NULL" in function
    assert "public._project_initialization_has_cascade_dependents" in function
    assert "create_operation.publication_mode <> 'empty'" in function
    assert "to_jsonb(project_row)" in function
    assert "create_operation.project_snapshot" in function
    for allowed_change in (
        "'version_root_hash'",
        "'mut_root_hash'",
        "'updated_at'",
        "'lifecycle_status'",
    ):
        assert allowed_change in function

    first_mutation = function.index("UPDATE public.access_surface_credentials")
    assert function.index("create_operation.publication_mode <> 'empty'") < first_mutation
    assert (
        function.index("RETURN jsonb_build_object('outcome', 'not_abandonable')") < first_mutation
    )
    assert function.index("public._project_initialization_has_cascade_dependents") < first_mutation
    assert function.index("to_jsonb(project_row)") < first_mutation
