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
        "--hosting-s3", action="store_true", help="Run owned real S3-compatible service tests"
    )
    group.addoption(
        "--hosting-application",
        action="store_true",
        help="Run actual src.main application with Docker-owned dependencies",
    )
    group.addoption(
        "--hosting-migration",
        action="store_true",
        help="Run whole-inventory migration in a dedicated fresh Docker stack",
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
        "hosting_s3: actual owned S3-compatible object service, not moto or disk",
        "hosting_application: actual authenticated application process with owned Docker dependencies",
        "hosting_migration: whole-inventory migration requires its dedicated clean stack",
        "hosting_final_contract: commit final schema in a separately owned fresh stack",
        "hosting_gap(reason): executable unmet target contract; never counted as support",
    ):
        config.addinivalue_line("markers", marker)


def pytest_collection_modifyitems(config, items):
    # A whole-inventory migrator must not consume other tests' deliberately
    # corrupt/partial tenants. Its separate CI job owns a fresh stack.
    if not config.getoption("--hosting-migration", default=False):
        excluded = [item for item in items if item.get_closest_marker("hosting_migration")]
        items[:] = [item for item in items if item not in excluded]
        if excluded:
            config.hook.pytest_deselected(items=excluded)
    for item in items:
        if "repository_hosting" not in str(item.path):
            continue
        for name in (
            "hosting_native",
            "hosting_component",
            "hosting_live",
            "hosting_supabase",
            "hosting_s3",
            "hosting_application",
        ):
            if item.get_closest_marker(name):
                item.user_properties.append(("execution_layer", name))
        gap = item.get_closest_marker("hosting_gap")
        if gap:
            item.user_properties.append(("known_gap", gap.args[0]))
        if gap and not config.getoption("--hosting-target", default=False):
            item.add_marker(pytest.mark.xfail(strict=True, reason=gap.args[0]))
        if item.get_closest_marker("hosting_live") and not config.getoption(
            "--hosting-live", default=False
        ):
            item.add_marker(
                pytest.mark.skip(reason="real PG requires --hosting-live; not acceptance evidence")
            )
        if item.get_closest_marker("hosting_application") and not config.getoption(
            "--hosting-application", default=False
        ):
            item.add_marker(
                pytest.mark.skip(
                    reason="actual application acceptance requires --docker --live --s3"
                )
            )
        if item.get_closest_marker("hosting_s3") and not config.getoption(
            "--hosting-s3", default=False
        ):
            item.add_marker(pytest.mark.skip(reason="real object service requires --live --s3"))
        if item.get_closest_marker("hosting_supabase") and not config.getoption(
            "--hosting-supabase", default=False
        ):
            item.add_marker(
                pytest.mark.skip(
                    reason="real Auth/PostgREST requires --live; not acceptance evidence"
                )
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
