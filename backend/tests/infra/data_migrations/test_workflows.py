from __future__ import annotations

import fnmatch
import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from src.infra.data_migrations.schema_history import data_migration_directory

REPOSITORY = Path(__file__).resolve().parents[4]
WORKFLOWS = REPOSITORY / ".github" / "workflows"


def test_reusable_database_jobs_cannot_bypass_database_change_detection():
    workflow = yaml.safe_load((WORKFLOWS / "validate-migrations.yml").read_text())
    scope = next(
        step["run"]
        for step in workflow["jobs"]["database_change_scope"]["steps"]
        if step.get("id") == "detect"
    )
    patterns = re.findall(r"'([^']+)'", scope)
    for path in WORKFLOWS.glob("_*.yml"):
        source = path.read_text()
        if "group: database-${{ inputs.environment }}" in source:
            relative = path.relative_to(REPOSITORY).as_posix()
            assert any(fnmatch.fnmatchcase(relative, pattern) for pattern in patterns), relative


@pytest.mark.parametrize(
    "filename",
    [
        "supabase/data_migrations/20260927000000_example/run.py",
        "supabase/releases/staging-data-migration.json",
        "supabase/archive/before_b1/data_migrations/20260720_project_storage_inventory/verify.sql",
        "backend/src/infra/data_migrations/runner.py",
        ".github/workflows/_operator-data-verify.yml",
        "scripts/database_history.py",
    ],
)
def test_main_promotion_requires_staging_evidence_for_data_only_changes(filename):
    result = _run_database_gate([{"filename": filename, "status": "modified"}])
    assert result["failed"] and "exact head SHA head" in result["failed"][0]
    assert result["workflows"] == ["migrate-staging.yml"]


def _run_database_gate(files):
    workflow = yaml.safe_load((WORKFLOWS / "main-release-gate.yml").read_text())
    step = next(
        step
        for step in workflow["jobs"]["main-release-gate"]["steps"]
        if step.get("name") == "Require exact Qubits database attestation"
    )
    # Execute the real workflow script against an API fake: no hosted mutation.
    harness = r"""
      const files = JSON.parse(process.argv[1]);
      const result = {failed: [], workflows: []};
      const context = {repo: {owner: 'test', repo: 'test'}, payload: {
        pull_request: {number: 1, head: {ref: 'qubits', sha: 'head'}, base: {sha: 'base'}, labels: []}
      }};
      const core = {info() {}, warning() {}, setFailed(message) {result.failed.push(message)}};
      const github = {rest: {pulls: {listFiles: 'files'}, actions: {listWorkflowRuns: 'runs'}},
        async paginate(method, args) {
          if (method === 'files') return files;
          result.workflows.push(args.workflow_id); return [];
        }};
    """
    harness += (
        "(async () => {\n"
        + step["with"]["script"]
        + "\n})().then(() => console.log(JSON.stringify(result)));"
    )
    completed = subprocess.run(
        ["node", "-e", harness, json.dumps(files)], capture_output=True, text=True, check=True
    )
    return json.loads(completed.stdout)


def test_database_gate_covers_renames_and_leaves_unrelated_changes_alone():
    assert _run_database_gate([{"filename": "frontend/app/page.tsx"}])["failed"] == []
    result = _run_database_gate(
        [{"filename": "archive/removed.sql", "previous_filename": "supabase/migrations/old.sql"}]
    )
    assert result["failed"]


def test_entire_database_release_queues_without_cancelling_pending_releases():
    for name, group in [
        ("migrate-staging", "database-release-staging"),
        ("migrate-production", "database-release-production"),
        ("data-migration", "database-release-${{ inputs.environment }}"),
    ]:
        workflow = yaml.safe_load((WORKFLOWS / (name + ".yml")).read_text())
        assert workflow["concurrency"] == {
            "group": group,
            "cancel-in-progress": False,
            "queue": "max",
        }


def test_operator_attestation_uses_catalog_and_read_only_runner():
    workflow = yaml.safe_load((WORKFLOWS / "_operator-data-verify.yml").read_text())
    steps = workflow["jobs"]["verify"]["steps"]
    verify = next(step for step in steps if step.get("name") == "Verify Supabase completion state")
    assert 'puppyone-db verify-external-state "$MIGRATION_ID"' in verify["run"]
    publish = next(
        step for step in steps if step.get("name") == "Publish operator verification attestation"
    )
    assert publish.get("if", "success()") == "success()"


def test_database_workflow_yaml_is_parseable() -> None:
    names = (
        "_schema-deploy.yml",
        "migrate-staging.yml",
        "migrate-production.yml",
        "_data-migration.yml",
        "_operator-data-verify.yml",
        "data-migration.yml",
        "validate-migrations.yml",
        "main-release-gate.yml",
    )
    for name in names:
        parsed = yaml.safe_load((WORKFLOWS / name).read_text())
        assert isinstance(parsed, dict), name
        assert "jobs" in parsed, name


def test_schema_and_data_jobs_share_serialized_environment_boundary() -> None:
    schema = (WORKFLOWS / "_schema-deploy.yml").read_text()
    data = (WORKFLOWS / "_data-migration.yml").read_text()
    operator_verify = (WORKFLOWS / "_operator-data-verify.yml").read_text()
    for workflow in (schema, data, operator_verify):
        assert "environment: ${{ inputs.environment }}" in workflow
        assert "group: database-${{ inputs.environment }}" in workflow
        assert "cancel-in-progress: false" in workflow
        assert "permissions:\n  contents: read" in workflow


def test_hosted_workflows_bind_connection_to_protected_project() -> None:
    schema = (WORKFLOWS / "_schema-deploy.yml").read_text()
    data = (WORKFLOWS / "_data-migration.yml").read_text()
    assert "python3 supabase/releases/connection.py" in schema
    assert "python3 supabase/releases/connection.py" in data
    assert "secrets.SUPABASE_PROJECT_ID" in schema
    assert "secrets.SUPABASE_PROJECT_ID" in data
    assert "secrets.DATABASE_URL" in schema
    assert "secrets.DATABASE_URL" in data
    for name in (
        "S3_ENDPOINT_URL",
        "S3_BUCKET_NAME",
        "S3_REGION",
        "S3_ACCESS_KEY_ID",
        "S3_SECRET_ACCESS_KEY",
    ):
        assert name not in data


def test_psql_receives_discrete_connection_environment() -> None:
    schema = (WORKFLOWS / "_schema-deploy.yml").read_text()
    validation = (WORKFLOWS / "validate-migrations.yml").read_text()

    assert "PGDATABASE:" not in schema
    assert "PGDATABASE:" not in validation
    assert 'psql "$DATABASE_URL"' not in schema
    assert "python3 supabase/releases/connection.py" in schema
    for job in yaml.safe_load(validation)["jobs"].values():
        for step in job.get("steps", []):
            if 'psql "$DATABASE_URL"' in step.get("run", ""):
                assert step["env"]["DATABASE_URL"] == (
                    "postgresql://postgres:postgres@127.0.0.1:54322/postgres"
                )
    upgrade_harness = (REPOSITORY / "scripts" / "test-repository-target-migration.sh").read_text()
    explicit_calls = upgrade_harness.count('psql "$database_url"')
    assert explicit_calls == upgrade_harness.count("psql ")
    assert explicit_calls >= 1


def test_hosted_schema_smoke_has_no_pgtap_runtime_dependency() -> None:
    smoke_path = REPOSITORY / "supabase" / "tests" / "_support" / "schema_contracts.inc"
    smoke = smoke_path.read_text()
    adapter = (REPOSITORY / "supabase" / "tests" / "smoke_test_triggers.sql").read_text()
    deploy = (WORKFLOWS / "_schema-deploy.yml").read_text()

    # The hosted staging/production projects are not required to install the
    # pgTAP test extension. Deployment smoke checks fail through SQL exceptions
    # and therefore must remain executable by plain psql.
    assert "SELECT plan(" not in smoke
    assert "SELECT pass(" not in smoke
    assert "finish()" not in smoke
    assert r"\ir _support/schema_contracts.inc" in adapter
    assert "-f supabase/tests/_support/schema_contracts.inc" in deploy
    assert smoke_path not in (REPOSITORY / "supabase" / "tests").rglob("*.sql")


def test_ordered_data_migration_fixtures_are_not_auto_discovered_by_supabase() -> None:
    supabase_tests = REPOSITORY / "supabase" / "tests"
    supabase_test_fixtures = REPOSITORY / "supabase" / "test_fixtures"
    permission_migration = (
        REPOSITORY
        / "supabase"
        / "archive"
        / "before_b1"
        / "data_migrations"
        / "20260712_repo_user_permissions_to_project_members"
    )
    creator_migration = data_migration_directory(
        REPOSITORY, "20260713_reconcile_project_creator_admin"
    )
    validation = (WORKFLOWS / "validate-migrations.yml").read_text()
    upgrade_harness = (REPOSITORY / "scripts" / "test-repository-target-migration.sh").read_text()

    assert not list(supabase_tests.glob("*fixture*.sql"))
    assert not list(supabase_tests.glob("*assert*.sql"))
    assert (supabase_test_fixtures / "repository_target_upgrade_assert.sql").is_file()
    assert (supabase_test_fixtures / "project_creator_admin_repair.sql").is_file()
    for migration in (permission_migration, creator_migration):
        assert (migration / "test_fixture.sql").is_file()
        assert (migration / "test_assert.sql").is_file()
        assert migration.name in validation + upgrade_harness


def test_production_data_work_cannot_run_from_untrusted_ref() -> None:
    dispatcher = (WORKFLOWS / "data-migration.yml").read_text()
    assert '"refs/heads/qubits"' in dispatcher
    assert '"refs/heads/main"' in dispatcher
    assert "production_staging_evidence" in dispatcher
    assert "environment: staging" in dispatcher
    assert "operation: verify" in dispatcher
    assert "needs.production_staging_evidence.result == 'success'" in dispatcher


def test_reusable_database_workflows_receive_protected_secrets() -> None:
    staging = (WORKFLOWS / "migrate-staging.yml").read_text()
    production = (WORKFLOWS / "migrate-production.yml").read_text()
    dispatcher = (WORKFLOWS / "data-migration.yml").read_text()

    # GitHub does not pass secrets to reusable workflows automatically. Every
    # direct caller must cross that boundary explicitly; the called job then
    # selects the protected staging/production Environment.
    assert staging.count("secrets: inherit") == 7
    assert production.count("secrets: inherit") == 10
    assert dispatcher.count("secrets: inherit") == 3


def test_staging_release_is_automatic_auditable_and_serial() -> None:
    staging = (WORKFLOWS / "migrate-staging.yml").read_text()
    parsed = yaml.safe_load(staging)
    jobs = parsed["jobs"]
    release = json.loads(
        (REPOSITORY / "supabase" / "releases" / "staging-data-migration.json").read_text()
    )

    assert '"refs/heads/qubits"' in staging
    assert "github.event_name == 'workflow_dispatch'" not in staging
    assert "scripts/database_history.py release --environment staging" in staging
    assert jobs["prepare_schema"]["needs"] == "resolve_data_release"
    assert jobs["prepare_schema"]["with"]["allow_data_migration_pause"] is True
    assert jobs["validate_schema_pause"]["needs"] == [
        "prepare_schema",
        "resolve_data_release",
    ]
    assert jobs["repair_run"]["needs"] == [
        "validate_schema_pause",
        "resolve_data_release",
    ]
    assert "outputs.repair_migration_id != ''" in jobs["repair_run"]["if"]
    assert "outputs.execution_mode == 'ci'" in jobs["repair_run"]["if"]
    assert jobs["data_plan"]["needs"] == [
        "validate_schema_pause",
        "repair_run",
        "resolve_data_release",
    ]
    # A staged repair may legitimately be absent (skipped), but its failure
    # must stop the staged data migration from mutating the environment.
    assert "needs.repair_run.result == 'success'" in jobs["data_plan"]["if"]
    assert "needs.repair_run.result == 'skipped'" in jobs["data_plan"]["if"]
    assert "outputs.execution_mode == 'ci'" in jobs["data_plan"]["if"]
    assert jobs["data_run"]["needs"] == ["data_plan", "resolve_data_release"]
    assert "always()" in jobs["data_run"]["if"]
    assert "needs.data_plan.result == 'success'" in jobs["data_run"]["if"]
    assert jobs["data_verify"]["needs"] == ["data_run", "resolve_data_release"]
    assert "always()" in jobs["data_verify"]["if"]
    assert "needs.data_run.result == 'success'" in jobs["data_verify"]["if"]
    assert jobs["operator_data_verify"]["with"] == {
        "environment": "staging",
        "migration_id": "${{ needs.resolve_data_release.outputs.migration_id }}",
    }
    assert "needs.operator_data_verify.result == 'success'" in jobs["deploy_after_data"]["if"]
    assert jobs["deploy_after_data"]["needs"] == [
        "data_verify",
        "operator_data_verify",
        "resolve_data_release",
    ]
    assert staging.count("uses: ./.github/workflows/_schema-deploy.yml") == 2
    assert staging.count("uses: ./.github/workflows/_data-migration.yml") == 4
    assert staging.count("uses: ./.github/workflows/_operator-data-verify.yml") == 1
    for operation in ("plan", "run", "verify"):
        assert f"operation: {operation}" in staging
    assert re.fullmatch(r"[0-9A-Za-z_]+", release["migration_id"])
    assert release["execution_mode"] in {"ci", "operator_local"}
    assert data_migration_directory(REPOSITORY, release["migration_id"]).is_dir()
    repair_id = release.get("repair_migration_id")
    if repair_id:
        assert re.fullmatch(r"[0-9A-Za-z_]+", repair_id)
        assert data_migration_directory(REPOSITORY, repair_id).is_dir()


def test_production_release_is_automatic_and_requires_qubits_evidence() -> None:
    production = (WORKFLOWS / "migrate-production.yml").read_text()
    parsed = yaml.safe_load(production)
    jobs = parsed["jobs"]
    release = json.loads(
        (REPOSITORY / "supabase" / "releases" / "production-data-migration.json").read_text()
    )

    assert '"refs/heads/main"' in production
    assert "    paths:" not in production
    assert "scripts/database_history.py release --environment production" in production
    assert jobs["prepare_schema"]["with"]["allow_data_migration_pause"] is True
    assert jobs["staging_data_evidence"]["with"] == {
        "environment": "staging",
        "migration_id": "${{ needs.resolve_data_release.outputs.migration_id }}",
        "operation": "verify",
    }
    assert "needs.staging_data_evidence.result == 'success'" in jobs["data_plan"]["if"]
    assert "outputs.execution_mode == 'ci'" in jobs["data_plan"]["if"]
    assert jobs["data_plan"]["needs"] == [
        "validate_schema_pause",
        "repair_run",
        "staging_data_evidence",
        "resolve_data_release",
    ]
    assert "always()" in jobs["data_run"]["if"]
    assert "needs.data_plan.result == 'success'" in jobs["data_run"]["if"]
    assert "always()" in jobs["data_verify"]["if"]
    assert "needs.data_run.result == 'success'" in jobs["data_verify"]["if"]
    assert jobs["staging_operator_evidence"]["with"] == {
        "environment": "staging",
        "migration_id": "${{ needs.resolve_data_release.outputs.migration_id }}",
    }
    assert jobs["operator_data_verify"]["with"] == {
        "environment": "production",
        "migration_id": "${{ needs.resolve_data_release.outputs.migration_id }}",
    }
    assert "needs.operator_data_verify.result == 'success'" in jobs["deploy_after_data"]["if"]
    assert production.count("uses: ./.github/workflows/_schema-deploy.yml") == 2
    assert production.count("uses: ./.github/workflows/_data-migration.yml") == 6
    assert production.count("uses: ./.github/workflows/_operator-data-verify.yml") == 2
    assert re.fullmatch(r"[0-9A-Za-z_]+", release["migration_id"])
    assert release["execution_mode"] in {"ci", "operator_local"}
    assert data_migration_directory(REPOSITORY, release["migration_id"]).is_dir()


def test_schema_runner_only_pauses_for_an_explicit_data_migration_guard() -> None:
    schema = (WORKFLOWS / "_schema-deploy.yml").read_text()

    assert "allow_data_migration_pause:" in schema
    assert 'grep -q "DATA_MIGRATION_REQUIRED:"' in schema
    assert "schema_state=data_migration_required" in schema
    assert 'if [ "$ALLOW_DATA_MIGRATION_PAUSE" = "true" ]' in schema
    assert 'exit "$status"' in schema
    assert "if: steps.push.outputs.schema_state == 'deployed'" in schema


def test_schema_drift_is_scoped_to_puppyone_owned_public_schema() -> None:
    schema = (WORKFLOWS / "_schema-deploy.yml").read_text()

    assert 'supabase db diff --db-url "$SUPABASE_DATABASE_URL" --schema public' in schema
    assert "supabase db diff --linked 2>&1" not in schema
    assert "PuppyPay's `puppypay`" in schema


def test_legacy_credential_readiness_is_checked_before_schema_writes() -> None:
    workflow = yaml.safe_load((WORKFLOWS / "_schema-deploy.yml").read_text())
    steps = workflow["jobs"]["deploy_schema"]["steps"]
    guard_index = next(
        i
        for i, step in enumerate(steps)
        if "supabase/releases/legacy_credential_preflight.sql" in step.get("run", "")
    )
    push_index = next(i for i, step in enumerate(steps) if step.get("id") == "push")
    assert guard_index < push_index
    assert "if" not in steps[guard_index]
    assert "continue-on-error" not in steps[guard_index]
    assert "ON_ERROR_STOP=1" in steps[guard_index]["run"]


def test_only_historical_upgrade_harness_can_insert_missing_older_migrations() -> None:
    schema = (WORKFLOWS / "_schema-deploy.yml").read_text()
    upgrade_harness = (REPOSITORY / "scripts" / "test-repository-target-migration.sh").read_text()

    # This harness intentionally creates holes in local schema history so it can
    # prove an existing installation upgrades correctly. Hosted deploys must keep
    # Supabase's strict ordering guard and never normalize such history drift.
    assert upgrade_harness.count("supabase migration up --local --include-all") == 3
    assert "20260717000000_project_deletion_admission_fence.sql" in upgrade_harness
    assert upgrade_harness.count("save_fence") == 4
    assert upgrade_harness.count("restore_fence") == 5
    assert "--include-all" not in schema


def test_needs_expressions_use_identifier_safe_job_ids() -> None:
    for name in ("data-migration.yml", "migrate-staging.yml"):
        workflow = (WORKFLOWS / name).read_text()
        for line in workflow.splitlines():
            if "needs." in line:
                reference = line.split("needs.", 1)[1].split(".", 1)[0]
                assert "-" not in reference


def test_pull_request_validation_never_receives_remote_database_secrets() -> None:
    validation = (WORKFLOWS / "validate-migrations.yml").read_text()
    assert "SUPABASE_ACCESS_TOKEN" not in validation
    assert "STAGING_DB_PASSWORD" not in validation
    assert "PRODUCTION_DB_PASSWORD" not in validation
    assert "db push --dry-run" not in validation


def test_pull_request_database_gate_always_publishes_a_stable_result() -> None:
    workflow = (WORKFLOWS / "validate-migrations.yml").read_text()

    assert "pull_request:\n    paths:" not in workflow
    assert "database_change_scope:" in workflow
    assert "database_validation_result:" in workflow
    assert "if: always()" in workflow
    assert "No database release paths changed." in workflow
    assert "tests/security/test_unified_authorization_architecture.py" in workflow


def test_workflow_dispatch_values_are_not_interpolated_into_shell() -> None:
    reusable = (WORKFLOWS / "_data-migration.yml").read_text()
    dispatcher = (WORKFLOWS / "data-migration.yml").read_text()
    assert 'plan "${{ inputs.migration_id }}"' not in reusable
    assert 'run "${{ inputs.migration_id }}"' not in reusable
    assert 'verify "${{ inputs.migration_id }}"' not in reusable
    assert 'plan "$MIGRATION_ID"' in reusable
    assert '"${{ inputs.environment }}" = ' not in dispatcher


def test_main_gate_requires_exact_schema_and_contract_verification() -> None:
    gate = (WORKFLOWS / "main-release-gate.yml").read_text()
    assert "pr.head.sha" in gate
    assert "latestOwnerReview?.commit_id === pr.head.sha" in gate
    assert "labeled, unlabeled" in gate
    assert "migrate-staging.yml" in gate
    assert "migrate-production.yml" in gate
    assert "requires-data-migration" in gate
    assert "stagingVerified" in gate
    assert "productionVerified" in gate
    assert "pr.base.sha" in gate
    assert "['added', 'renamed'].includes(file.status)" in gate


def test_every_qubits_head_receives_an_exact_schema_attestation() -> None:
    staging = (WORKFLOWS / "migrate-staging.yml").read_text()
    assert "branches:\n      - qubits" in staging
    assert "    paths:" not in staging


def test_database_workflow_third_party_actions_are_sha_pinned() -> None:
    for name in (
        "_schema-deploy.yml",
        "_data-migration.yml",
        "_operator-data-verify.yml",
        "migrate-staging.yml",
        "validate-migrations.yml",
        "main-release-gate.yml",
    ):
        text = (WORKFLOWS / name).read_text()
        for line in text.splitlines():
            if "uses:" not in line or "./.github/workflows/" in line:
                continue
            reference = line.split("uses:", 1)[1].split("#", 1)[0].strip()
            assert "@" in reference, (name, line)
            revision = reference.rsplit("@", 1)[1]
            assert len(revision) == 40, (name, line)
            assert all(character in "0123456789abcdef" for character in revision)
