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
