"""Protected release entrypoint. Configuration is private runner-local state."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
from pathlib import Path

from ..database import PsqlClient
from .engine import Release
from .hosted import Hosted
from .plan import ReleasePlan
from .target import DatabaseTarget


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", choices=["staging", "production"], required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--configuration", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-f0-9]{40}", args.source):
        raise ValueError("A complete source commit is required")
    root = Path(__file__).resolve().parents[5]
    metadata = args.configuration.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
        raise ValueError("Release configuration must be a private regular file")
    config = json.loads(args.configuration.read_text())
    if config.get("format_version") != 1 or config.get("environment") != args.environment:
        raise ValueError("Release configuration is for a different environment")
    environment = {
        **os.environ,
        **config["migration_environment"],
        "DATA_MIGRATION_DATABASE_URL": config["database_url"],
    }
    # An operator flag cannot substitute for the coordinator's measured drain.
    environment.pop("NATIVE_MIGRATION_WRITERS_DRAINED", None)
    environment["RELEASE_PRIVATE_ARTIFACTS"] = config["private_artifacts"]
    db = PsqlClient(config["database_url"], base_environment=environment)
    platform = Hosted(root, config, db)
    target = DatabaseTarget(root, db, platform, environment)
    private_logs = Path(config["private_artifacts"])
    private_logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    log_path = private_logs / (args.source + ".log")
    descriptor = os.open(log_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "a") as private_output:
        target.runner.output = private_output
        result = Release(ReleasePlan.load(root), target).run(args.source)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps({**result, "environment": args.environment}, indent=2) + "\n")
    # No credentials, IDs, customer rows, SQL diagnostics or internal URLs.
    print(
        json.dumps(
            {"environment": args.environment, "source_sha": args.source, "state": result["state"]}
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        raise SystemExit(
            f"Environment release failed ({type(error).__name__}); consult the private release journal"
        ) from None
