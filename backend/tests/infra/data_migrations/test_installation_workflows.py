from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[4]


def workflow(name):
    return yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)


def test_every_main_revision_rebuilds_database_and_full_installation():
    for name in ("prepare-baseline.yml", "validate-migrations.yml", "self-hosted-install.yml"):
        triggers = workflow(name)["on"]
        assert "main" in triggers["push"]["branches"]
        assert "paths" not in triggers["push"]
        assert "paths-ignore" not in triggers["push"]


def test_merge_validation_cannot_generate_or_replace_the_baseline():
    steps = workflow("prepare-baseline.yml")["jobs"]["baseline"]["steps"]
    commands = "\n".join(step.get("run", "") for step in steps)
    assert "verify --candidate supabase/baselines/b1" in commands
    assert "prepare --output" not in commands
    assert "git push" not in commands


def test_required_installation_summary_cannot_skip_a_failed_variant():
    jobs = workflow("self-hosted-install.yml")["jobs"]
    assert jobs["install"]["strategy"]["matrix"]["variant"] == ["default", "custom", "upgrade"]
    assert "continue-on-error" not in jobs["install"]
    assert jobs["result"]["if"] == "always()"
    assert jobs["result"]["needs"] == "install"
    assert jobs["result"]["steps"][0]["run"] == 'test "$RESULT" = success'


def test_native_migrations_and_storage_completion_gate_application_startup():
    services = yaml.safe_load((ROOT / "docker/docker-compose.yml").read_text())["services"]
    assert services["api"]["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert services["api"]["depends_on"]["storage-init"]["condition"] == "service_completed_successfully"
    assert services["web"]["depends_on"]["api"]["condition"] == "service_healthy"
    assert not any("supabase/" in mount for mount in services["db"]["volumes"])
    assert services["migrate"]["build"]["context"] == ".."
