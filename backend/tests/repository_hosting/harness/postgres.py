from __future__ import annotations

import json
import os
import subprocess
import uuid
from contextlib import contextmanager
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

    @contextmanager
    def empty_database(self):
        """Own one fresh database inside the already-validated disposable stack."""
        name = "hosting_upgrade_" + uuid.uuid4().hex
        self.sql(f"CREATE DATABASE {name} TEMPLATE template0 ENCODING 'UTF8';")
        try:
            child = Postgres()
            child.url = urlparse(self.url)._replace(path="/" + name).geturl()
            yield child
        finally:
            self.sql(f"DROP DATABASE {name} WITH (FORCE);")

    def create_project(self):
        key, user = uuid.uuid4().hex, str(uuid.uuid4())
        project, org = "hosting-" + key, "hosting-org-" + key
        self.sql(f"""
          BEGIN;
          INSERT INTO auth.users(id,aud,role,email,encrypted_password,email_confirmed_at,raw_app_meta_data,raw_user_meta_data,created_at,updated_at)
          VALUES ({literal(user)},'authenticated','authenticated',{literal(key + '@example.test')},'',now(),'{{}}','{{}}',now(),now());
          INSERT INTO public.organizations(id,name,slug,type,plan,seat_limit,created_by)
          VALUES ({literal(org)},'Hosting tests',{literal(org)},'team','enterprise',5,{literal(user)});
          INSERT INTO public.org_members(id,org_id,user_id,role) VALUES ({literal('member-' + key)},{literal(org)},{literal(user)},'owner');
          INSERT INTO public.projects(id,name,org_id,created_by,lifecycle_status,version_root_hash)
          VALUES ({literal(project)},'Hosting tests',{literal(org)},{literal(user)},'ready',{literal('1' * 40)});
          INSERT INTO public.project_members(id,org_id,project_id,user_id,role,granted_by)
          VALUES ({literal('pm-' + key)},{literal(org)},{literal(project)},{literal(user)},'admin',{literal(user)});
          COMMIT;
        """)
        return project

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
