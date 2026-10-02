"""Unit checks for false-positive-proof rehearsal assertions, NOT database proof.

Actual permissions, JWTs, repositories and migrations run separately in the
mandatory Docker/Supabase rehearsal; these doubles only test its failure logic.
"""
from __future__ import annotations

import importlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml


@pytest.fixture
def runner(monkeypatch):
    root = Path(__file__).resolve().parents[4]
    monkeypatch.syspath_prepend(str(root / "scripts"))
    return importlib.import_module("test_data_api_containment")


@pytest.mark.parametrize("returncode,stderr,accepted", [
    (1, "ERROR:  ISSUE-053: client can access audit_logs\n", True),
    (0, "", False),
    (1, 'ERROR: syntax error\nLINE 4: RAISE EXCEPTION \'ISSUE-053: ...\'\n', False),
    (1, "Cannot connect to Docker daemon\n", False),
])
def test_negative_guard_requires_its_own_database_error(runner, monkeypatch, returncode, stderr, accepted):
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess([], returncode, "", stderr))
    stack = SimpleNamespace(container="owned-test-only")
    if accepted:
        runner.expect_guard_failure(stack, "read-only contract")
    else:
        with pytest.raises(AssertionError, match="Expected security rejection"):
            runner.expect_guard_failure(stack, "read-only contract")


@pytest.mark.parametrize("unsafe_query_error", [False, True])
def test_http_probe_uses_filters_and_rejects_non_acl_denials(runner, monkeypatch, unsafe_query_error):
    requests = []

    def respond(request):
        requests.append(request)
        if request.headers["authorization"] == "Bearer unit-service":
            return httpx.Response(200, json=[{"synthetic": 1}, {"synthetic": 2}])
        if request.method in {"PATCH", "DELETE"}:
            assert request.url.query, "unfiltered write would hit pg_safeupdate before ACL"
            if unsafe_query_error:
                return httpx.Response(400, json={"code": "21000"})
        return httpx.Response(403, json={"code": "42501"})

    real_client = httpx.Client
    monkeypatch.setattr(runner.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw))
    status = {"API_URL": "http://127.0.0.1:56391", "ANON_KEY": "unit-anon",
              "SERVICE_ROLE_KEY": "unit-service", "JWT_SECRET": "unit-only-synthetic-signing-key-not-a-secret"}
    if unsafe_query_error:
        with pytest.raises(AssertionError, match="Write denial is not a PostgreSQL permission failure"):
            runner.rest_matrix(status, before=False)
    else:
        assert runner.rest_matrix(status, before=False) == 88
        assert sum(r.method in {"PATCH", "DELETE"} for r in requests) == 36


def test_http_probe_cannot_target_hosted_database(runner):
    with pytest.raises(AssertionError, match="Nonlocal API refused"):
        runner.rest_matrix({"API_URL": "https://hosted.example.test"}, before=False)


def test_real_rehearsal_is_part_of_stable_database_gate():
    root = Path(__file__).resolve().parents[4]
    jobs = yaml.safe_load((root / ".github/workflows/validate-migrations.yml").read_text())["jobs"]
    rehearsal = jobs["validate_data_api_containment"]
    assert rehearsal["if"] == "needs.database_change_scope.outputs.changed == 'true'"
    assert any("scripts/test_data_api_containment.py" in step.get("run", "") for step in rehearsal["steps"])
    gate = jobs["database_validation_result"]
    assert "validate_data_api_containment" in gate["needs"]
    assert gate["env"]["CONTAINMENT_RESULT"] == "${{ needs.validate_data_api_containment.result }}"
    assert '"$CONTAINMENT_RESULT"' in gate["steps"][0]["run"]
