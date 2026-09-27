"""Operator CLI shared by GitHub Actions and self-hosted deployments."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .catalog import DataMigrationCatalog
from .database import PsqlClient
from .errors import DataMigrationError
from .policy import git_changed_paths, validate_repository_policy
from .runner import DataMigrationRunner


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="puppyone-db")
    parser.add_argument("--repository-root", type=Path)
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATA_MIGRATION_DATABASE_URL", ""),
        help="PostgreSQL connection URI; defaults to DATA_MIGRATION_DATABASE_URL",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="list repository data migration artifacts")
    subparsers.add_parser("lint", help="validate every repository artifact")
    policy = subparsers.add_parser("policy", help="enforce immutable database change policy")
    policy.add_argument("--base-ref", required=True)
    for name in ("plan", "run", "status", "verify", "verify-external-state"):
        command = subparsers.add_parser(name)
        command.add_argument("migration_id")
    return parser


def _runner(args: argparse.Namespace, catalog: DataMigrationCatalog) -> DataMigrationRunner:
    database = PsqlClient(args.database_url)
    project_ref = os.environ.get("SUPABASE_PROJECT_ID", "").strip()
    if project_ref:
        database.assert_supabase_target(
            project_ref=project_ref,
            api_url=os.environ.get("SUPABASE_URL", ""),
        )
    return DataMigrationRunner(catalog, database)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        catalog = DataMigrationCatalog(args.repository_root)
        if args.command == "list":
            print(
                json.dumps(
                    [
                        {
                            "id": artifact.manifest.id,
                            "kind": artifact.manifest.kind.value,
                            "legacy": artifact.manifest.legacy,
                            "checksum": artifact.checksum,
                        }
                        for artifact in catalog.load_all()
                    ],
                    indent=2,
                )
            )
            return 0
        if args.command == "lint":
            artifacts = catalog.load_all()
            validate_repository_policy(catalog, [])
            print(f"validated {len(artifacts)} data migration artifact(s)")
            return 0
        if args.command == "policy":
            changes = git_changed_paths(catalog.repository_root, args.base_ref)
            validate_repository_policy(catalog, changes)
            print(f"database change policy passed for {len(changes)} changed path(s)")
            return 0

        runner = _runner(args, catalog)
        if args.command in {"plan", "status"}:
            print(runner.plan(args.migration_id).model_dump_json(indent=2))
        elif args.command == "run":
            print(runner.run(args.migration_id).model_dump_json(indent=2))
        elif args.command == "verify":
            runner.verify(args.migration_id)
            print(json.dumps({"id": args.migration_id, "verified": True}, indent=2))
        elif args.command == "verify-external-state":
            print(json.dumps(runner.verify_external_state(args.migration_id), indent=2))
        return 0
    except DataMigrationError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
