from __future__ import annotations

import pytest


def pytest_addoption(parser):
    group = parser.getgroup("repository hosting")
    group.addoption(
        "--hosting-target",
        action="store_true",
        help="Fail on known full-Git gaps instead of reporting strict xfails",
    )
    group.addoption(
        "--hosting-live",
        action="store_true",
        help="Run isolated PostgreSQL integration tests (native PG or Supabase)",
    )
    group.addoption(
        "--hosting-supabase",
        action="store_true",
        help="Run real local Supabase Auth/PostgREST tests; native PG is insufficient",
    )


def pytest_configure(config):
    for marker in (
        "hosting_native: stock Git workspace/oracle, not Cloud acceptance",
        "hosting_component: production Python code with explicitly substituted control plane",
        "hosting_live: real isolated PostgreSQL/Supabase SQL",
        "hosting_supabase: actual local Supabase Auth/PostgREST, no auth doubles",
        "hosting_gap(reason): executable unmet target contract; never counted as support",
    ):
        config.addinivalue_line("markers", marker)


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "repository_hosting" not in str(item.path):
            continue
        for name in ("hosting_native", "hosting_component", "hosting_live", "hosting_supabase"):
            if item.get_closest_marker(name):
                item.user_properties.append(("execution_layer", name))
        gap = item.get_closest_marker("hosting_gap")
        if gap:
            item.user_properties.append(("known_gap", gap.args[0]))
        if gap and not config.getoption("--hosting-target"):
            item.add_marker(pytest.mark.xfail(strict=True, reason=gap.args[0]))
        if item.get_closest_marker("hosting_live") and not config.getoption("--hosting-live"):
            item.add_marker(
                pytest.mark.skip(reason="real PG requires --hosting-live; not acceptance evidence")
            )
        if item.get_closest_marker("hosting_supabase") and not config.getoption("--hosting-supabase"):
            item.add_marker(
                pytest.mark.skip(reason="real Auth/PostgREST requires --live; not acceptance evidence")
            )


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    # A known capability gap must never conceal broken credentials, unavailable
    # databases, failed baseline pushes, or teardown failures in its fixture.
    if (
        item.get_closest_marker("hosting_gap")
        and report.when != "call"
        and hasattr(report, "wasxfail")
    ):
        report.outcome = "failed"
        del report.wasxfail


@pytest.fixture
def git_repo(tmp_path):
    from .harness.git import Git

    return Git.init(tmp_path / "native")


@pytest.fixture
def component_repo(tmp_path, monkeypatch):
    """Production engine and disk objects; explicit in-memory control-plane double."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from src.version_engine.adapters.product.operation_adapter import ProductOperationAdapter
    from src.version_engine.infrastructure.supabase.scope_manager import ScopeManager
    from src.version_engine.infrastructure.supabase.server_repo import PuppyOneServerRepo
    from src.version_engine.storage.object_store import ObjectStore
    from src.version_engine.write_engine.tree_objects import build_tree_from_files
    from tests.version_engine.test_server_repo import FakeAuditManager, FakeHistoryManager

    class Scopes:
        def __init__(self):
            self.rows = {}

        def get(self, sid):
            return self.rows.get(sid)

        def put(self, sid, scope):
            self.rows[sid] = scope

        def delete(self, sid):
            return self.rows.pop(sid, None)

        def list_all(self):
            return list(self.rows.values())

    store = ObjectStore(tmp_path / "objects")
    repo = PuppyOneServerRepo(
        project_id="test-proj",
        project_name="Hosting tests",
        store=store,
        history=FakeHistoryManager(),
        audit=FakeAuditManager(),
        scopes=ScopeManager(Scopes()),
    )
    manager = MagicMock()
    manager.get_server_repo.return_value = repo
    empty = build_tree_from_files(store, {})
    repo.history.set_root_hash(empty)
    repo.history.set_scope_hash("", empty)
    # These are optional external collaborators, not the code under test.
    monkeypatch.setattr(
        "src.version_engine.derived.hooks.schedule_post_push_hook", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "src.version_engine.derived.hooks.schedule_post_project_update_hook", lambda *a, **k: None
    )
    quota = SimpleNamespace(check_publish=AsyncMock(return_value=None))
    monkeypatch.setattr("src.platform.billing.storage.get_storage_quota_service", lambda: quota)
    return SimpleNamespace(repo=repo, manager=manager, adapter=ProductOperationAdapter(manager))
