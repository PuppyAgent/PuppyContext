from pathlib import Path

from scripts.check_railway_release_gate import REQUIRED_ROLES, validate_contract

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_railway_release_gate_repository_contract_is_consistent() -> None:
    assert validate_contract(REPO_ROOT) == []


def test_railway_release_gate_covers_every_backend_service_role() -> None:
    railway = (REPO_ROOT / "backend/railway.toml").read_text(encoding="utf-8")
    assert {"api", "upload_worker", "import_worker", "synchronize_worker", "mcp_server"} == REQUIRED_ROLES
    assert all(role == "api" or role in railway for role in REQUIRED_ROLES)
