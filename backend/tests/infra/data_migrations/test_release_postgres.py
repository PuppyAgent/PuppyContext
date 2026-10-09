"""Real release journal, serialization and lock-loss tests on an owned PG cluster."""

import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

from src.infra.data_migrations.database import PsqlClient
from src.infra.data_migrations.errors import ExecutionError, MigrationBusyError
from src.infra.data_migrations.release.journal import Journal

ROOT = Path(__file__).resolve().parents[4]
pytestmark = pytest.mark.integration


def run(*args, **kwargs):
    return subprocess.run(
        list(map(str, args)), check=True, capture_output=True, text=True, timeout=60, **kwargs
    ).stdout.strip()


@pytest.fixture(scope="module")
def cluster(tmp_path_factory):
    if os.environ.get("PUPPYONE_RELEASE_PG_TEST") != "1":
        pytest.skip("explicit owned-cluster test; set PUPPYONE_RELEASE_PG_TEST=1")
    executable = shutil.which("initdb")
    assert executable, "PostgreSQL 17 binaries must be on PATH"
    binaries = Path(executable).parent
    root = tmp_path_factory.mktemp("release-pg")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    run(binaries / "initdb", "-D", root / "db", "-U", "postgres", "--auth=trust", "--no-locale")
    try:
        run(
            binaries / "pg_ctl",
            "-D",
            root / "db",
            "-l",
            root / "server.log",
            "-w",
            "-o",
            f"-p {port} -h 127.0.0.1 -k /tmp",
            "start",
        )
        url = f"postgresql://postgres@127.0.0.1:{port}/postgres?sslmode=disable&application_name=release-test"
        db = PsqlClient(url)
        db.scalar("CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role;")
        yield db
    finally:
        if (root / "db/postmaster.pid").exists():
            run(binaries / "pg_ctl", "-D", root / "db", "-m", "fast", "-w", "stop")


@pytest.fixture
def journal(cluster, tmp_path):
    cluster.scalar("DROP SCHEMA IF EXISTS puppyone_release CASCADE")
    control = tmp_path / "supabase/releases/control.sql"
    control.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / "supabase/releases/control.sql", control)
    run("git", "init", "-q", tmp_path)

    def commit(text):
        (tmp_path / "release.txt").write_text(text)
        run("git", "add", ".", cwd=tmp_path)
        run(
            "git",
            "-c",
            "user.name=Release Test",
            "-c",
            "user.email=release@example.invalid",
            "commit",
            "-qm",
            text,
            cwd=tmp_path,
        )
        return run("git", "rev-parse", "HEAD", cwd=tmp_path)

    old, new = commit("old"), commit("new")
    return Journal(tmp_path, cluster), old, new


def test_journal_records_verified_phases_and_resumes_after_process_loss(journal):
    value, old, _ = journal
    assert value.begin(old, "checksum")
    value.checkpoint("backup", {"restore_point_ref": "owned.dump", "restore_verified": True})
    resumed = Journal(value.root, value.db)
    assert resumed.begin(old, "checksum")
    assert resumed.evidence("backup")["restore_point_ref"] == "owned.dump"
    resumed.finish("accepted", "accepted")
    assert not Journal(value.root, value.db).begin(old, "checksum")


def test_old_accepted_source_cannot_roll_back_newer_failed_release(journal):
    value, old, new = journal
    value.begin(old, "old-plan")
    value.finish("accepted", "accepted")
    value.begin(new, "new-plan")
    value.finish("failed", "data")
    with pytest.raises(ValueError, match="descend"):
        value.begin(old, "old-plan")
    assert value.begin(new, "new-plan")


def test_forward_fix_supersedes_failure_but_preserves_audit(journal):
    value, old, new = journal
    value.begin(old, "old-plan")
    value.checkpoint("failure", {"phase": "data"})
    value.finish("failed", "data")
    assert value.begin(new, "new-plan")
    assert (
        value.db.scalar(
            "SELECT state FROM puppyone_release.runs WHERE source_sha=:'sha'",
            variables={"sha": old},
        )
        == "superseded"
    )
    with pytest.raises(ValueError, match="superseded"):
        value.begin(old, "old-plan")


def test_changed_plan_for_same_source_is_rejected(journal):
    value, old, _ = journal
    value.begin(old, "one")
    with pytest.raises(ValueError, match="plan changed"):
        value.begin(old, "two")


def test_database_not_ci_alone_serializes_release(cluster):
    with cluster.advisory_lock("puppyone-environment-release") as first:
        first.assert_held()
        with pytest.raises(MigrationBusyError), cluster.advisory_lock("puppyone-environment-release"):
            pytest.fail("second release acquired the same database")
    with cluster.advisory_lock("puppyone-environment-release") as next_release:
        next_release.assert_held()


def test_lost_lock_is_detected_before_next_release_operation(cluster):
    with pytest.raises(ExecutionError, match="lock connection was lost"), cluster.advisory_lock("puppyone-environment-release") as guard:
        cluster.scalar(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name='release-test' AND pid<>pg_backend_pid()"
        )
        guard.assert_held()


def test_release_receipts_are_not_accessible_to_product_roles(journal):
    value, old, _ = journal
    value.begin(old, "plan")
    for role in ("anon", "authenticated", "service_role"):
        assert (
            value.db.scalar(f"SELECT has_schema_privilege('{role}','puppyone_release','USAGE')")
            == "f"
        )
