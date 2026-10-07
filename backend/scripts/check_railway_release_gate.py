"""Validate the repository half of the Qubits Railway database release gate.

Railway's ``Wait for CI`` switch is external state and must be evidenced from
the dashboard/API. This check prevents repository drift around that setting:
the complete service-role inventory, source branch, database workflow, and the
absence of per-service schema mutation all remain reviewable and repeatable.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REQUIRED_ROLES = {"api", "agent_worker", "upload_worker", "import_worker", "synchronize_worker", "mcp_server"}


def validate_contract(repo_root: Path) -> list[str]:
    errors: list[str] = []
    manifest_path = repo_root / "backend/deploy/railway-qubits-services.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"cannot read Railway service manifest: {exc}"]

    if manifest.get("schema_version") != 1:
        errors.append("Railway service manifest schema_version must be 1")
    if manifest.get("repository") != "puppyone-ai/puppyone-cloud":
        errors.append("Railway service manifest must target puppyone-ai/puppyone-cloud")
    if manifest.get("branch") != "qubits":
        errors.append("Railway service manifest must target qubits")
    if manifest.get("root_directory") not in {"backend", "/backend"}:
        errors.append("Railway services must use backend as Root Directory")

    gate = manifest.get("database_gate") or {}
    if gate.get("workflow_file") != ".github/workflows/migrate-staging.yml":
        errors.append("database gate must use migrate-staging.yml")
    if gate.get("workflow_name") != "Deploy Database to Qubits":
        errors.append("database gate workflow name drifted")
    if gate.get("wait_for_ci_required") is not True:
        errors.append("Wait for CI must remain required")

    services = manifest.get("services")
    if not isinstance(services, list):
        errors.append("services must be a list")
        services = []
    roles = [item.get("service_role") for item in services if isinstance(item, dict)]
    if len(roles) != len(set(roles)):
        errors.append("Railway service roles must be unique")
    if set(roles) != REQUIRED_ROLES:
        errors.append(
            "Railway service inventory must contain exactly: "
            + ", ".join(sorted(REQUIRED_ROLES))
        )
    if any(item.get("requires_qubits_schema") is not True for item in services):
        errors.append("every listed Railway service must require the Qubits schema gate")

    workflow_path = repo_root / str(gate.get("workflow_file", ""))
    try:
        workflow = workflow_path.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(f"cannot read database workflow: {exc}")
        workflow = ""
    if "name: Deploy Database to Qubits" not in workflow:
        errors.append("migrate-staging.yml must keep its stable workflow name")
    if "branches:\n      - qubits" not in workflow:
        errors.append("migrate-staging.yml must run on every qubits push")

    railway_files = [repo_root / "backend/railway.toml", repo_root / "backend/nixpacks.toml"]
    deployment_text = "\n".join(path.read_text(encoding="utf-8") for path in railway_files)
    lowered = deployment_text.lower()
    if "supabase db push" in lowered:
        errors.append("Railway build/start config must not run supabase db push")
    railway_toml = railway_files[0].read_text(encoding="utf-8")
    for role in REQUIRED_ROLES - {"api"}:
        if role not in railway_toml:
            errors.append(f"backend/railway.toml does not route SERVICE_ROLE={role}")

    return errors


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    errors = validate_contract(repo_root)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("Railway Qubits database release contract is internally consistent.")
    print("Dashboard Wait for CI state still requires an external release receipt.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
