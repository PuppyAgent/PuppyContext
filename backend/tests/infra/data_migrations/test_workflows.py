from __future__ import annotations

import fnmatch
import json
import os
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
        "scripts/qubits_entrypoint_cutover.py",
        "scripts/entrypoint_source_decisions.py",
        ".github/workflows/entrypoint-cutover.yml",
    ],
)
def test_main_promotion_requires_staging_evidence_for_data_only_changes(filename):
    result = _run_database_gate([{"filename": filename, "status": "modified"}])
    assert result["failed"] and "exact head SHA head" in result["failed"][0]
    assert result["workflows"] == ["migrate-staging.yml"]


def _run_database_gate(files, success=False):
    workflow = yaml.safe_load((WORKFLOWS / "main-release-gate.yml").read_text())
    step = next(
        step
        for step in workflow["jobs"]["main-release-gate"]["steps"]
        if step.get("name") == "Require exact Qubits database attestation"
    )
    # Execute the real workflow script against an API fake: no hosted mutation.
    harness = r"""
      const files = JSON.parse(process.argv[1]);
      const success = process.argv[2] === "true";
      const result = {failed: [], workflows: []};
      const context = {repo: {owner: 'test', repo: 'test'}, payload: {
        pull_request: {number: 1, head: {ref: 'qubits', sha: 'head'}, base: {sha: 'base'}, labels: []}
      }};
      const core = {info() {}, warning() {}, setFailed(message) {result.failed.push(message)}};
      const github = {rest: {pulls: {listFiles: 'files'}, actions: {listWorkflowRuns: 'runs'}},
        async paginate(method, args) {
          if (method === 'files') return files;
          result.workflows.push(args.workflow_id); return success ? [{head_sha: "head", conclusion: "success", event: "push", head_branch: "qubits", status: "completed", html_url: "test"}] : [];
        }};
    """
    harness += (
        "(async () => {\n"
        + step["with"]["script"]
        + "\n})().then(() => console.log(JSON.stringify(result)));"
    )
    completed = subprocess.run(
        ["node", "-e", harness, json.dumps(files), json.dumps(success)], capture_output=True, text=True, check=True
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
    parsed = yaml.safe_load(dispatcher)
    trigger = parsed.get("on", parsed.get(True))
    assert trigger["workflow_dispatch"]["inputs"]["operation"]["options"] == ["plan", "verify"]
    assert "Use the environment release coordinator for writes" in dispatcher


def test_release_credentials_remain_on_restricted_private_runners():
    shared = yaml.safe_load((WORKFLOWS / "_environment-release.yml").read_text())
    release = shared["jobs"]["release"]
    assert release["environment"] == "${{ inputs.environment }}"
    assert "self-hosted" in str(release["runs-on"])
    command = next(s["run"] for s in release["steps"] if s.get("name") == "Run the shared upgrade and acceptance program")
    assert "src.infra.data_migrations.release" in command
    assert "/etc/puppyone/releases/" in command
    assert "secrets.S3" not in str(shared)


@pytest.mark.parametrize("file,branch,environment", [
    ("migrate-staging.yml", "qubits", "staging"),
    ("migrate-production.yml", "main", "production"),
])
def test_both_environments_use_one_automatic_release_program(file, branch, environment):
    workflow = yaml.safe_load((WORKFLOWS / file).read_text())
    trigger = workflow.get("on", workflow.get(True))
    assert trigger["push"] == {"branches": [branch]}
    job = workflow["jobs"]["release"]
    assert job["uses"] == "./.github/workflows/_environment-release.yml"
    assert job["with"] == {"environment": environment}
    assert "refs/heads/" + branch in job["if"]
    assert "puppyone-ai/puppyone-cloud" in job["if"]


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


def test_historical_upgrade_harness_exercises_missing_older_migrations() -> None:
    upgrade_harness = (REPOSITORY / "scripts" / "test-repository-target-migration.sh").read_text()

    # This harness intentionally creates holes in local schema history so it can
    # prove an existing installation upgrades correctly.
    assert upgrade_harness.count("supabase migration up --local --include-all") == 3
    assert "20260717000000_project_deletion_admission_fence.sql" in upgrade_harness
    assert upgrade_harness.count("save_fence") == 4
    assert upgrade_harness.count("restore_fence") == 5


@pytest.mark.parametrize(
    ("message", "pause", "expected_status", "expected_state"),
    [
        ("", "true", 0, "deployed"),
        ("DATA_MIGRATION_REQUIRED:20261003_final_entrypoint_storage", "true", 0, "data_migration_required"),
        ("DATA_MIGRATION_REQUIRED:20261003_final_entrypoint_storage", "false", 1, "data_migration_required"),
        ("ENTRYPOINT_FREEZE_REQUIRED", "true", 1, None),
        ("permission denied", "true", 1, None),
    ],
)
def test_schema_catchup_preserves_data_guards_and_database_failures(
    tmp_path, message, pause, expected_status, expected_state
) -> None:
    workflow = yaml.safe_load((WORKFLOWS / "_schema-deploy.yml").read_text())
    steps = workflow["jobs"]["deploy_schema"]["steps"]
    plan = next(step["run"] for step in steps if step.get("name") == "Show pending schema plan")
    push = next(step["run"] for step in steps if step.get("id") == "push")
    # Model a remote database containing a newer applied version: both plan and
    # execution must include missing older versions, while real SQL failures
    # still prevent a successful deployment attestation.
    cli = tmp_path / "supabase"
    cli.write_text('''#!/bin/bash
case " $* " in *" --include-all "*) ;; *) exit 42 ;; esac
case " $* " in *" --dry-run "*) exit 0 ;; esac
if [ -n "$TEST_DATABASE_ERROR" ]; then
  echo "$TEST_DATABASE_ERROR"
  exit 1
fi
''')
    cli.chmod(0o700)
    output = tmp_path / "outputs"
    env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}",
               SUPABASE_DATABASE_URL="postgresql://fixture.invalid/test",
               GITHUB_OUTPUT=str(output), GITHUB_STEP_SUMMARY=str(tmp_path / "summary"),
               TEST_DATABASE_ERROR=message, ALLOW_DATA_MIGRATION_PAUSE=pause)
    assert subprocess.run(["bash", "-c", plan], env=env, capture_output=True).returncode == 0
    # Isolate the workflow's diagnostic file from other local tests.
    push = push.replace("/tmp/schema-push.log", str(tmp_path / "schema-push.log"))
    result = subprocess.run(["bash", "-c", push], env=env, capture_output=True, text=True)
    assert result.returncode == expected_status, result.stdout + result.stderr
    recorded = output.read_text() if output.exists() else ""
    if expected_state:
        assert f"schema_state={expected_state}\n" in recorded
    else:
        assert "schema_state=deployed" not in recorded


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


def test_main_gate_does_not_require_production_to_be_migrated_before_merge():
    gate = (WORKFLOWS / "main-release-gate.yml").read_text()
    assert "pr.head.sha" in gate
    assert "latestOwnerReview?.commit_id === pr.head.sha" in gate
    assert "migrate-staging.yml" in gate
    assert "migrate-production.yml" not in gate
    assert "productionVerified" not in gate
    result = _run_database_gate([{"filename": "supabase/migrations/new_contract.sql", "status": "added"}], success=True)
    assert result["failed"] == []
    assert result["workflows"] == ["migrate-staging.yml"]


def test_every_qubits_head_receives_an_exact_schema_attestation() -> None:
    staging = (WORKFLOWS / "migrate-staging.yml").read_text()
    assert "branches:\n      - qubits" in staging
    assert "    paths:" not in staging


def test_every_waited_workflow_runs_on_each_protected_push_without_cancellation():
    shared = (WORKFLOWS / "_environment-release.yml").read_text()
    waited = re.findall(r"'([a-z-]+\.yml)'", shared)
    assert "secret-scanning-gitleaks.yml" in waited
    assert "release-upgrade-rehearsal.yml" in waited
    for name in waited:
        workflow = yaml.safe_load((WORKFLOWS / name).read_text())
        trigger = workflow.get("on", workflow.get(True))
        assert set(trigger["push"]["branches"]) == {"main", "qubits"}, name
        assert "paths" not in trigger["push"] and "paths-ignore" not in trigger["push"], name
        assert workflow.get("concurrency", {}).get("cancel-in-progress") is not True, name


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
