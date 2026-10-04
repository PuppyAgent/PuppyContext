"""Local startup diagnostics never turn unavailable infrastructure into evidence."""

import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.hosting_component
ROOT = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location(
    "hosting_startup_runner", ROOT / "scripts/testing/run_repository_hosting.py",
)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
OWNED = "supabase_db_puppy-baseline-test1234"
COMMAND = ["supabase", "start", "--workdir", "/owned-test-directory"]


class Process:
    def __init__(self, clock, *, finish_at=None, code=0):
        self.clock, self.finish_at, self.code = clock, finish_at, code
        self.returncode = None
        self.killed = False
        self.drained = False

    def communicate(self, timeout):
        if self.killed:
            self.drained = True
            return "PRIVATE-LOCAL-KEY", "PRIVATE-CLI-OUTPUT"
        self.clock[0] += timeout
        if self.finish_at is not None and self.clock[0] >= self.finish_at:
            self.returncode = self.code
            return "PRIVATE-LOCAL-KEY", "PRIVATE-CLI-OUTPUT"
        raise subprocess.TimeoutExpired(COMMAND, timeout, output="PRIVATE-LOCAL-KEY")

    def kill(self):
        self.killed = True
        self.returncode = -9

    def poll(self):
        return self.returncode


@pytest.fixture
def startup(monkeypatch):
    clock = [0.0]
    child = Process(clock)
    launches = []
    monkeypatch.setattr(runner.time, "monotonic", lambda: clock[0])

    def launch(command, **kwargs):
        launches.append((command, kwargs))
        return child

    monkeypatch.setattr(runner.subprocess, "Popen", launch)
    monkeypatch.setattr(runner, "supabase_database_state", lambda *a, **k: {"status": "created"})

    def invoke(**kwargs):
        return runner.start_local_supabase(
            COMMAND, OWNED, env={"PATH": "fixture"}, timeout=30,
            created_timeout=10, poll_interval=5, **kwargs,
        )

    return SimpleNamespace(clock=clock, child=child, launches=launches, invoke=invoke)


def test_stalled_created_container_fails_before_full_startup_timeout(startup):
    with pytest.raises(runner.SupabaseStartupError, match="never started") as failure:
        startup.invoke()
    assert 10 <= startup.clock[0] < 30
    assert failure.value.diagnostics["database"]["status"] == "created"
    assert failure.value.diagnostics["reason"] == "docker_container_not_started"
    assert startup.child.killed and startup.child.drained
    assert "PRIVATE" not in str(failure.value)
    assert "PRIVATE" not in json.dumps(failure.value.diagnostics)
    assert startup.launches[0][1]["env"] == {"PATH": "fixture"}


def test_started_database_is_not_misclassified_as_docker_start_failure(startup, monkeypatch):
    monkeypatch.setattr(runner, "supabase_database_state", lambda *a, **k: {"status": "running"})
    with pytest.raises(runner.SupabaseStartupError) as failure:
        startup.invoke()
    assert failure.value.diagnostics["reason"] == "supabase_startup_timeout"
    assert startup.clock[0] >= 30
    assert startup.child.killed and startup.child.drained


def test_missing_container_during_image_pull_uses_full_budget(startup, monkeypatch):
    monkeypatch.setattr(runner, "supabase_database_state", lambda *a, **k: {"status": "unavailable"})
    with pytest.raises(runner.SupabaseStartupError) as failure:
        startup.invoke()
    assert failure.value.diagnostics["reason"] == "supabase_startup_timeout"
    assert startup.clock[0] >= 30


def test_created_timer_resets_when_container_enters_running(startup, monkeypatch):
    states = iter(["created", "running", "created", "running"])
    monkeypatch.setattr(runner, "supabase_database_state", lambda *a, **k: {"status": next(states)})
    startup.child.finish_at = 25
    output, diagnostic = startup.invoke()
    assert output == "PRIVATE-LOCAL-KEY"  # caller consumes keys; evidence does not
    assert diagnostic["reason"] == "started"
    assert "PRIVATE" not in json.dumps(diagnostic)
    assert not startup.child.killed


def test_nonzero_cli_exit_cannot_claim_success(startup):
    startup.child.finish_at, startup.child.code = 5, 7
    with pytest.raises(runner.SupabaseStartupError, match="exit 7") as failure:
        startup.invoke()
    assert failure.value.diagnostics["reason"] == "supabase_cli_failed"
    assert "PRIVATE" not in str(failure.value)
    assert not startup.child.killed


def test_interrupt_reaps_only_its_own_cli_child(startup, monkeypatch):
    original = startup.child.communicate

    def interrupted(timeout):
        if startup.child.killed:
            return original(timeout)
        raise KeyboardInterrupt

    monkeypatch.setattr(startup.child, "communicate", interrupted)
    with pytest.raises(KeyboardInterrupt):
        startup.invoke()
    assert startup.child.killed and startup.child.drained
    assert len(startup.launches) == 1


@pytest.mark.parametrize("container", ["postgres", "supabase_db_production", "supabase_db_puppy-baseline-"])
def test_diagnostics_refuse_unowned_container_names(container, monkeypatch):
    calls = []
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **k: calls.append(a))
    with pytest.raises(ValueError, match="owned"):
        runner.supabase_database_state(container, env={})
    assert calls == []


def test_diagnostics_only_record_allowlisted_state_fields(monkeypatch):
    calls = []

    def inspect(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "status": "created", "running": False, "exit_code": 0,
            "oom_killed": False, "health": "", "Error": "PRIVATE-SECRET",
            "Env": ["SERVICE_ROLE_KEY=PRIVATE-SECRET"],
        }))

    monkeypatch.setattr(runner.subprocess, "run", inspect)
    state = runner.supabase_database_state(OWNED, env={"PATH": "fixture"})
    assert state == {"status": "created", "running": False, "exit_code": 0,
                     "oom_killed": False, "health": ""}
    assert "PRIVATE" not in json.dumps(state)
    assert calls[0][0][:2] == ["docker", "inspect"]
    assert calls[0][0][-1] == OWNED
    assert calls[0][1]["timeout"] <= 5


@pytest.mark.parametrize("output", ["PRIVATE-NOT-JSON", '{"status":"PRIVATE-SECRET"}', '[]'])
def test_unreadable_inspection_cannot_leak_raw_output(output, monkeypatch):
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=output))
    assert runner.supabase_database_state(OWNED, env={}) == {"status": "unavailable"}


def test_unresponsive_docker_inspection_is_bounded(monkeypatch):
    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired("docker", 5, output="PRIVATE-SECRET")

    monkeypatch.setattr(runner.subprocess, "run", fail)
    assert runner.supabase_database_state(OWNED, env={}) == {"status": "unavailable"}
