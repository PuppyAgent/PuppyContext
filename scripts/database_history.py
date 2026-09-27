#!/usr/bin/env python3
"""Stage archived upgrades or verify/adopt B1 using an explicit environment DSN."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from src.infra.data_migrations.baseline_adoption import adoption_sql  # noqa: E402
from src.infra.data_migrations.database import PsqlClient  # noqa: E402
from src.infra.data_migrations.errors import DataMigrationError  # noqa: E402
from src.infra.data_migrations.schema_history import (  # noqa: E402
    data_migration_directory,
    load_baseline,
)


def stage_history(destination: Path) -> None:
    baseline = load_baseline(ROOT)
    if not baseline:
        raise ValueError("No active baseline")
    # Never overwrite a checkout, linked project, or an operator's existing work.
    destination.mkdir(parents=True, exist_ok=False)
    target = destination / "supabase"
    (target / "migrations").mkdir(parents=True)
    shutil.copyfile(ROOT / "supabase/config.toml", target / "config.toml")
    for name in baseline["source_migrations"]:
        shutil.copyfile(ROOT / baseline["archive"] / name, target / "migrations" / name)
    shutil.copytree(ROOT / "supabase/tests", target / "tests")
    print(f"Archived upgrade workdir: {destination}")


def resolve_release(root: Path, environment: str) -> dict[str, str]:
    if environment not in {"staging", "production", "standalone"}:
        raise ValueError("invalid release environment")
    selection = json.loads(
        (root / f"supabase/releases/{environment}-data-migration.json").read_text()
    )
    if not isinstance(selection, dict):
        raise ValueError("data release must be an object")
    migration_id = selection.get("migration_id")
    mode = selection.get("execution_mode", "ci")
    repair = selection.get("repair_migration_id", "")
    if not isinstance(mode, str) or mode not in {"ci", "operator_local"}:
        raise ValueError("invalid data release mode")
    if not isinstance(repair, str) or (mode == "operator_local" and repair):
        raise ValueError("invalid repair migration")
    data_migration_directory(root, migration_id)
    if repair:
        data_migration_directory(root, repair)
    return {
        "migration_id": migration_id,
        "repair_migration_id": repair,
        "execution_mode": mode,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    stage = commands.add_parser(
        "stage", help="materialize the immutable pre-B1 migration chain"
    )
    stage.add_argument("--output", type=Path, required=True)
    data_path = commands.add_parser(
        "data-path", help="resolve an active or archived immutable ID"
    )
    data_path.add_argument("migration_id")
    release = commands.add_parser("release", help="validate the selected data release")
    release.add_argument(
        "--environment", choices=("staging", "production", "standalone"), required=True
    )
    commands.add_parser("check", help="verify admission without committing any changes")
    commands.add_parser(
        "adopt", help="verify and atomically replace covered history only"
    )
    args = parser.parse_args()
    if args.command == "data-path":
        print(data_migration_directory(ROOT, args.migration_id))
        return
    if args.command == "release":
        selection = resolve_release(ROOT, args.environment)
        output = "".join(f"{key}={value}\n" for key, value in selection.items())
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as destination:
                destination.write(output)
        print(output, end="")
        return
    if args.command == "stage":
        stage_history(args.output.resolve())
        return
    db = PsqlClient(
        os.environ.get("DATA_MIGRATION_DATABASE_URL")
        or os.environ.get("DATABASE_URL", "")
    )
    project = os.environ.get("SUPABASE_PROJECT_ID")
    if project:
        db.assert_supabase_target(
            project_ref=project, api_url=os.environ.get("SUPABASE_URL", "")
        )
    db.command(
        ["-q", "-v", "ON_ERROR_STOP=1"],
        timeout=180,
        input_text=adoption_sql(ROOT, apply=args.command == "adopt"),
    )
    print(
        "Baseline admission verified; "
        + ("history aligned." if args.command == "adopt" else "no changes committed.")
    )


if __name__ == "__main__":
    try:
        main()
    except (DataMigrationError, ValueError, OSError) as error:
        # Database errors can contain data. Only expose our fixed diagnostic code.
        import re

        code = re.search(r"BASELINE_[A-Z_]+", str(error))
        raise SystemExit(
            code.group(0)
            if code
            else f"Baseline admission failed ({type(error).__name__})"
        ) from None
