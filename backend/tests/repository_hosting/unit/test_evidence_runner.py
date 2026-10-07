"""The gate must fail closed even if pytest exits zero without valid evidence."""

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.hosting_component
ROOT = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location(
    "hosting_runner", ROOT / "scripts/testing/run_repository_hosting.py",
)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


@pytest.mark.parametrize("xml", [None, "", "<not-xml", "<testsuites/>"])
def test_missing_or_empty_results_cannot_pass(tmp_path, xml):
    path = tmp_path / "junit.xml"
    if xml is not None:
        path.write_text(xml)
    result = {"pytest_exit": 0, "target": True}
    runner.record_tests(path, result)
    assert result["evidence_error"]
    assert runner.result_exit_code(result) != 0


@pytest.mark.parametrize("status", [
    '<skipped message="missing PG"/>',
    '<skipped type="pytest.xfail" message="not implemented"/>',
    '<failure message="broken"/>',
    '<error message="fixture unavailable"/>',
])
def test_target_fails_on_every_nonpassing_case(tmp_path, status):
    path = tmp_path / "junit.xml"
    path.write_text(f'<testsuite><testcase name="case">{status}</testcase></testsuite>')
    result = {"pytest_exit": 0, "target": True}
    runner.record_tests(path, result)
    assert runner.result_exit_code(result) == 1


def test_native_pass_does_not_relabel_component_or_database_results(tmp_path):
    path = tmp_path / "junit.xml"
    path.write_text('''<testsuite>
      <testcase name="native"><properties>
        <property name="execution_layer" value="hosting_native"/>
      </properties></testcase>
      <testcase name="gap"><properties>
        <property name="execution_layer" value="hosting_component"/>
        <property name="known_gap" value="ref transaction not implemented"/>
      </properties><skipped type="pytest.xfail"/></testcase>
      <testcase name="database"><properties>
        <property name="execution_layer" value="hosting_live"/>
      </properties><skipped/></testcase>
    </testsuite>''')
    result = {"pytest_exit": 0, "target": False}
    runner.record_tests(path, result)
    assert result["layers"] == {
        "hosting_native": {"passed": 1},
        "hosting_component": {"known_gap": 1},
        "hosting_live": {"skipped": 1},
    }
    assert result["gaps"][0]["status"] == "known_gap"
    assert runner.result_exit_code(result) == 0  # Baseline regression, not acceptance.
    result["target"] = True
    assert runner.result_exit_code(result) == 1


@pytest.mark.parametrize("field,value", [("sql_exit", 1), ("pytest_exit", 2),
                                        ("infrastructure_error", "not started")])
def test_external_failures_cannot_be_masked_by_passing_junit(tmp_path, field, value):
    path = tmp_path / "junit.xml"
    path.write_text('<testsuite><testcase name="ok"/></testsuite>')
    result = {"pytest_exit": 0, "target": True, field: value}
    runner.record_tests(path, result)
    assert runner.result_exit_code(result) != 0


@pytest.mark.parametrize("executed,sql_exit,expected", [
    (False, None, 1), (True, None, 1), (False, 0, 1), (True, 0, 0),
])
def test_live_requires_explicit_sql_execution_and_exit(executed, sql_exit, expected):
    result = {
        "live": True, "target": True, "pytest_exit": 0,
        "supabase_sql_suite_executed": executed,
        "supabase_sql_complete": True,
        "layers": {"hosting_live": {"passed": 1}},
    }
    if sql_exit is not None:
        result["sql_exit"] = sql_exit
    assert runner.result_exit_code(result) == expected


def test_cli_registry_matches_database_ci_without_inheriting_credentials():
    env = runner.local_supabase_environment({
        "PATH": "/fixture", "SUPABASE_ACCESS_TOKEN": "private",
        "SUPABASE_DB_PASSWORD": "private", "AWS_SECRET_ACCESS_KEY": "private",
        "S3_BUCKET": "production", "SUPABASE_INTERNAL_IMAGE_REGISTRY": "untrusted.test",
    })
    assert env == {"PATH": "/fixture", "SUPABASE_INTERNAL_IMAGE_REGISTRY": "docker.io"}


@pytest.mark.parametrize("sql_exit", [0, 1])
def test_live_sql_precedes_tenant_fixtures_and_cannot_be_masked(monkeypatch, tmp_path, sql_exit):
    # Use the real stack object's filesystem contract, without starting Docker.
    spec = importlib.util.spec_from_file_location("hosting_baseline", ROOT / "scripts/database_baseline.py")
    baseline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(baseline)
    stack = baseline.LocalStack(tmp_path, [])
    calls = []

    def run(args, **kwargs):
        from subprocess import CompletedProcess
        calls.append((args, kwargs))
        return CompletedProcess(args, sql_exit, "Files=9, Tests=329, 1 wallclock secs\nResult: PASS\n", "")

    def call(args, **kwargs):
        calls.append((args, kwargs))
        return 0

    monkeypatch.setattr(runner, "prepare_supabase_sql_fixture", lambda s, e: calls.append((s, e)))
    monkeypatch.setattr(runner.subprocess, "run", run)
    monkeypatch.setattr(runner.subprocess, "call", call)
    result = {"live": True, "target": True, "layers": {"hosting_component": {"passed": 1}}}
    runner.run_supabase_suites(stack, ["pytest"],
                               {"phase": "pytest"}, {"phase": "sql"}, result)
    assert calls == [
        (stack, {"phase": "sql"}),
        (["supabase", "test", "db", "--workdir", str(tmp_path)],
         {"cwd": runner.ROOT, "env": {"phase": "sql"}, "capture_output": True, "text": True, "timeout": 300, "check": False}),
        (["pytest"], {"cwd": runner.ROOT / "backend", "env": {"phase": "pytest"}}),
    ]
    assert result["supabase_sql_suite_executed"] is True
    assert result["sql_exit"] == sql_exit
    assert result["pytest_exit"] == 0
    assert runner.result_exit_code(result) == sql_exit


@pytest.mark.parametrize("output", [
    "", "Result: PASS", "Files=0, Tests=0,\nResult: PASS",
    "Files=9, Tests=329,\nResult: FAIL",
    "Files=9, Tests=329,\nResult: PASS\nSMOKE TEST SKIPPED: no organization",
    "Files=9, Tests=329,\nResult: PASS\nok 1 # SKIP missing fixture",
    "Files=9, Tests=329,\nResult: PASS\nnot ok 1 # TODO pending contract",
])
def test_incomplete_or_skipped_sql_cannot_pass_live_gate(output):
    result = {"live": True, "target": True, "pytest_exit": 0, "sql_exit": 0,
              "supabase_sql_suite_executed": True, "layers": {"hosting_live": {"passed": 1}}}
    runner.record_sql_tests(output, result)
    assert runner.result_exit_code(result) == 1


def test_live_gate_requires_sql_report_even_with_zero_exit():
    result = {"live": True, "target": True, "pytest_exit": 0, "sql_exit": 0,
              "supabase_sql_suite_executed": True, "layers": {"hosting_live": {"passed": 1}}}
    assert runner.result_exit_code(result) == 1
