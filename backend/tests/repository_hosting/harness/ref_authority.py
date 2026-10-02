"""Owner-installed control-plane fixtures. Never proof of S3 object durability."""

import base64
import json
import uuid

from .postgres import literal

ABSENT = {"kind": "absent"}
A, B, C = (char * 40 for char in "abc")
TABLES = (
    "version_repositories", "version_repository_refs", "version_publication_receipts",
    "version_ref_transactions", "version_reflog_entries", "version_ref_events",
)


def b64(name):
    return base64.b64encode(name).decode("ascii")


def oid(value):
    return {"kind": "oid", "oid": value}


def symbolic(name):
    return {"kind": "symbolic", "target_b64": b64(name)}


def update(name=b"refs/heads/main", old=ABSENT, new=None):
    result = {"name_b64": b64(name), "expected": old}
    if new is not None:
        result["new"] = new
    return result


class Authority:
    def __init__(self, pg, project, *, object_format="sha1", roots=None):
        self.pg, self.project = pg, project
        self.receipt = str(uuid.uuid4())
        roots = roots or {A: "commit", B: "commit", C: "commit", "d" * 40: "blob", "e" * 40: "tag"}
        pg.sql(f"""
          INSERT INTO public.version_repositories(project_id, authority, object_format)
          VALUES ({literal(project)}, 'native', {literal(object_format)});
          INSERT INTO public.version_repository_refs(project_id, name, object_format, symbolic_target)
          VALUES ({literal(project)}, decode('48454144','hex'), {literal(object_format)}, decode({literal(b'refs/heads/main'.hex())},'hex'));
          INSERT INTO public.version_publication_receipts
            (id, project_id, object_format, generation, gc_epoch, roots, manifest_sha256, verified_at, expires_at)
          VALUES ({literal(self.receipt)}, {literal(project)}, {literal(object_format)}, 1, 1,
            {literal(roots)},
            {literal('f' * 64)}, now(), now() + interval '1 hour');
        """)

    def parameters(self, updates, *, key=None, actor="test:writer", generation=1, receipt=True):
        return {
            "p_project_id": self.project, "p_actor": actor,
            "p_request_key": key or str(uuid.uuid4()), "p_generation": generation,
            "p_updates": updates, "p_receipt_id": self.receipt if receipt else None,
            "p_message": "SQL fixture",
        }

    def query(self, updates, **kwargs):
        return (
            "SET ROLE service_role; SELECT public.apply_version_ref_transaction("
            + ",".join(literal(arg) for arg in self.parameters(updates, **kwargs).values()) + ");"
        )

    def apply(self, updates, **kwargs):
        return json.loads(self.pg.value(self.query(updates, **kwargs)))

    def state(self, name=b"refs/heads/main"):
        value = self.pg.value(f"""
          SELECT jsonb_build_object('oid', target_oid, 'target', encode(symbolic_target,'hex'))
          FROM public.version_repository_refs WHERE project_id={literal(self.project)}
          AND name=decode({literal(name.hex())},'hex');
        """)
        return json.loads(value) if value else None

    def count(self, table):
        assert table in (*TABLES, "audit_logs")
        return int(self.pg.value(
            f"SELECT count(*) FROM public.{table} WHERE project_id={literal(self.project)}"
        ))
