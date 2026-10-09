"""Validate the code-owned release contract; platform state is checked at release."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REQUIRED_ROLES = {
    "api",
    "agent_worker",
    "upload_worker",
    "import_worker",
    "synchronize_worker",
    "mcp_server",
}


def validate_contract(repo_root: Path) -> list[str]:
    errors = []
    try:
        manifest = json.loads(
            (repo_root / "backend/deploy/railway-release-services.json").read_text()
        )
    except (OSError, ValueError) as error:
        return [f"Cannot read release inventory: {type(error).__name__}"]
    if (
        manifest.get("schema_version") != 2
        or manifest.get("repository") != "puppyone-ai/puppyone-cloud"
    ):
        errors.append("Invalid release inventory identity")
    if set(manifest.get("services", {})) != REQUIRED_ROLES | {"frontend"}:
        errors.append("Release inventory must include every worker, API, MCP and frontend")
    if (
        manifest.get("deployment_owner") != "environment-release"
        or manifest.get("independent_autodeploy") is not False
    ):
        errors.append("Only the environment coordinator may deploy applications")
    for environment, branch, workflow in [
        ("staging", "qubits", "migrate-staging.yml"),
        ("production", "main", "migrate-production.yml"),
    ]:
        if manifest.get("environments", {}).get(environment) != {
            "branch": branch,
            "workflow": workflow,
        }:
            errors.append("Release environment mapping differs")
        source = (repo_root / ".github/workflows" / workflow).read_text()
        if (
            f"branches:\n      - {branch}" not in source
            or "uses: ./.github/workflows/_environment-release.yml" not in source
        ):
            errors.append(f"{environment} must invoke the shared coordinator on every push")
    for name in ("railway.toml", "nixpacks.toml"):
        source = (repo_root / "backend" / name).read_text()
        if "supabase db push" in source.lower():
            errors.append("Application build/start must not apply migrations")
    railway = (repo_root / "backend/railway.toml").read_text()
    for role in REQUIRED_ROLES - {"api"}:
        if role not in railway:
            errors.append(f"Missing worker start dispatch: {role}")
    return errors


def main() -> int:
    errors = validate_contract(Path(__file__).resolve().parents[2])
    for error in errors:
        print("ERROR: " + error, file=sys.stderr)
    if not errors:
        print("Shared release inventory is consistent. Hosted activation is verified separately.")
    return bool(errors)


if __name__ == "__main__":
    raise SystemExit(main())
