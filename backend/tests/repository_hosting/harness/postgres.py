from __future__ import annotations

import json
import os
import subprocess
from urllib.parse import urlparse


def literal(value):
    if value is None:
        return "NULL"
    if isinstance(value, (dict, list)):
        value = json.dumps(value)
    return "'" + str(value).replace("'", "''") + "'"


class Postgres:
    def __init__(self):
        self.url = os.environ.get("HOSTING_TEST_DB_URL", "")
        parsed = urlparse(self.url)
        if parsed.hostname not in {"127.0.0.1", "localhost"} or not os.environ.get(
            "HOSTING_TEST_STACK", ""
        ).startswith("puppy-baseline-"):
            raise RuntimeError(
                "Use scripts/testing/run_repository_hosting.py --live; a disposable loopback stack is required"
            )

    def sql(self, statement, *, check=True):
        result = subprocess.run(
            ["psql", self.url, "-X", "-qAt", "-v", "ON_ERROR_STOP=1"],
            input=statement,
            text=True,
            capture_output=True,
            timeout=40,
        )
        if check and result.returncode:
            raise AssertionError(result.stderr)
        return result

    def value(self, statement):
        return self.sql(statement).stdout.strip()

    def publish(self, project, old, new, commit):
        values = [
            project,
            old,
            new,
            commit,
            "test:writer",
            "test publish",
            "write",
            "[]",
            "[]",
            "",
            "test:writer",
            "{}",
        ]
        return (
            "SELECT row_to_json(result) FROM public.publish_version_project_update("
            + ",".join(literal(v) for v in values)
            + ") result;"
        )
