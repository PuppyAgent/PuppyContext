#!/usr/bin/env python3
"""Owned Docker migration and final-schema acceptance, with no hosted target."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

if __name__ == "__main__":
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(
            (
                "SUPABASE_",
                "AWS_",
                "S3_",
                "PG",
                "DATA_MIGRATION_",
                "NATIVE_MIGRATION_",
                "HOSTING_TEST_",
            )
        )
    }
    # Failure fixtures deliberately retain blocked inventories. Contract must be
    # tested in its own fresh stack, never by deleting those proof failures.
    for phase, selection in (
        ("inventory", "hosting_migration and not hosting_final_contract"),
        ("contract", "hosting_final_contract"),
    ):
        result = subprocess.call(
            [
                sys.executable,
                str(ROOT / "scripts/testing/run_repository_hosting.py"),
                "--live",
                "--s3",
                "--docker",
                "--migration",
                "--target",
                "--output",
                str(ROOT / "backend/.native-migration-test-results" / phase),
                "--hosting-migration",
                "-m",
                selection,
                "-q",
            ],
            env=env,
        )
        if result:
            raise SystemExit(result)
